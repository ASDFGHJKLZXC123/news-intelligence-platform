"""Shared, domain-neutral primitives for the Stage 9 validation/tuning protocol.

``docs/evaluation/stage9-validation-protocol.md`` (protocol id ``stage9-validation.v1``) defines a
reproducible, hash-verified tuning procedure. This module is the small, deterministic toolkit that
procedure is built on -- and nothing more:

* :func:`inclusive_decimal_grid` -- an exact, endpoint-inclusive candidate grid built with
  :class:`~decimal.Decimal`, so a threshold sweep never drifts on binary floating point.
* :class:`BinaryMetrics` -- a confusion matrix whose precision is ``None`` (undefined, **never** 1)
  when nothing was predicted positive, and whose every rate is safe against a zero denominator.
* :func:`canonical_json_bytes` / :func:`canonical_json_hash` / :func:`sha256_file` /
  :func:`verify_artifact_hash` -- one canonical byte encoding and one hash, so a report or a frozen
  parameter artifact has exactly one SHA-256, and a frozen artifact can be checked byte-for-byte.
* :class:`CalibrationStatus` and :func:`validate_status` -- the report status vocabulary.

It deliberately implements **no domain evaluator**: it never loads a gold set, opens a holdout,
reads a threshold, or scores a model. It is pure stdlib, offline, and free of the clock and of
randomness. Domain evaluators (clustering, entity linking, alerts) are later Stage 9 items and live
next to their data, using these primitives.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Chunk size for streaming a file through the hash -- artifacts are small, but never read whole.
_HASH_CHUNK = 1 << 16
_SHA256_HEX_CHARS = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
#: A sane ceiling so a malformed ``(start, stop, step)`` cannot request an unbounded grid.
MAX_GRID_POINTS = 100_000
#: Metric rates are rounded to this many decimals in JSON output, for reproducible reports.
METRIC_PRECISION = 6


class CalibrationError(ValueError):
    """A calibration primitive was given input it cannot use. Raised before any tuning runs."""


class ArtifactVerificationError(CalibrationError):
    """A frozen artifact's bytes do not match the expected SHA-256 (or it cannot be read)."""


# --- deterministic candidate grids ---------------------------------------------------------
def _to_decimal(value: Decimal | int | str, name: str) -> Decimal:
    """Coerce a grid bound to :class:`~decimal.Decimal`. Floats are refused -- they would drift."""
    if isinstance(value, bool):
        raise CalibrationError(f"{name} must be a Decimal, int or decimal string, not bool")
    if isinstance(value, float):
        raise CalibrationError(
            f"{name} must be a Decimal, int or decimal string, not a float ({value!r}); "
            "a binary float would make the grid non-reproducible -- pass '0.005', not 0.005"
        )
    if not isinstance(value, Decimal | int | str):
        raise CalibrationError(f"{name} must be a Decimal, int or str, got {type(value).__name__}")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CalibrationError(f"{name} is not a valid decimal: {value!r}") from exc
    if not result.is_finite():
        raise CalibrationError(
            f"{name} must be a finite decimal, not {value!r}: "
            "NaN and Infinity are not valid grid bounds"
        )
    return result


def inclusive_decimal_grid(
    start: Decimal | int | str, stop: Decimal | int | str, step: Decimal | int | str
) -> tuple[Decimal, ...]:
    """An endpoint-inclusive grid of Decimals from ``start`` to ``stop`` in ``step`` increments.

    Built entirely with :class:`~decimal.Decimal`, so ``0.700..0.950`` step ``0.005`` yields exactly
    51 points and the last one is ``0.950`` exactly -- never ``0.9500000000000002``. Both endpoints
    are always present, which requires ``stop - start`` to be an exact multiple of ``step``; anything
    else is a malformed grid, not a silently truncated one, and raises.

    ``start``, ``stop`` and ``step`` must be Decimals, ints or decimal strings -- never floats, which
    would reintroduce the drift the Decimal build exists to avoid.
    """
    lo = _to_decimal(start, "start")
    hi = _to_decimal(stop, "stop")
    delta = _to_decimal(step, "step")
    if delta <= 0:
        raise CalibrationError(f"step must be positive, got {delta}")
    if hi < lo:
        raise CalibrationError(f"stop {hi} must be >= start {lo}")
    try:
        whole, remainder = divmod(hi - lo, delta)
    except DecimalException as exc:
        raise CalibrationError(
            f"grid {lo}..{hi} step {delta} exceeds the deterministic Decimal precision; "
            "use coarser bounds or a larger step"
        ) from exc
    if remainder != 0:
        raise CalibrationError(
            f"grid is not inclusive: stop {hi} is not start {lo} plus a whole number of "
            f"steps of {delta}"
        )
    count = int(whole)
    if count + 1 > MAX_GRID_POINTS:
        raise CalibrationError(f"grid would hold {count + 1} points, above the {MAX_GRID_POINTS} cap")
    return tuple(lo + delta * index for index in range(count + 1))


# --- binary confusion / metrics ------------------------------------------------------------
def _round(value: float | None) -> float | None:
    return None if value is None else round(value, METRIC_PRECISION)


@dataclass(frozen=True, slots=True)
class BinaryMetrics:
    """A binary confusion matrix and its rates, every one safe against a zero denominator.

    The one rule that matters for tuning: :attr:`precision` is ``None`` -- undefined, not ``1.0`` --
    when nothing was predicted positive. A policy that auto-accepts nothing must never look like a
    perfect one. Every other rate is ``None`` exactly where its denominator is zero, so a normal case
    and a degenerate one stay distinguishable rather than silently collapsing onto a real number.
    """

    tp: int
    fp: int
    tn: int
    fn: int

    def __post_init__(self) -> None:
        for name in ("tp", "fp", "tn", "fn"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CalibrationError(f"{name} must be a non-negative int, got {value!r}")

    @classmethod
    def from_predictions(cls, predicted: Sequence[bool], actual: Sequence[bool]) -> BinaryMetrics:
        """Tally a confusion matrix from equal-length predicted/actual boolean sequences."""
        if len(predicted) != len(actual):
            raise CalibrationError(
                f"predicted ({len(predicted)}) and actual ({len(actual)}) differ in length"
            )
        tp = fp = tn = fn = 0
        for index, (pred, act) in enumerate(zip(predicted, actual, strict=True)):
            if not isinstance(pred, bool) or not isinstance(act, bool):
                raise CalibrationError(f"predicted/actual[{index}] must both be booleans")
            if pred and act:
                tp += 1
            elif pred and not act:
                fp += 1
            elif not pred and act:
                fn += 1
            else:
                tn += 1
        return cls(tp=tp, fp=fp, tn=tn, fn=fn)

    @property
    def predicted_positives(self) -> int:
        return self.tp + self.fp

    @property
    def actual_positives(self) -> int:
        return self.tp + self.fn

    @property
    def actual_negatives(self) -> int:
        return self.tn + self.fp

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self) -> float | None:
        """``tp / (tp + fp)`` -- ``None`` (infeasible), never ``1.0``, when nothing was predicted."""
        predicted = self.predicted_positives
        return None if predicted == 0 else self.tp / predicted

    @property
    def recall(self) -> float | None:
        """``tp / (tp + fn)`` -- ``None`` when there are no actual positives."""
        actual = self.actual_positives
        return None if actual == 0 else self.tp / actual

    @property
    def specificity(self) -> float | None:
        """``tn / (tn + fp)`` -- ``None`` when there are no actual negatives."""
        actual = self.actual_negatives
        return None if actual == 0 else self.tn / actual

    @property
    def f1(self) -> float | None:
        """``2tp / (2tp + fp + fn)`` -- ``None`` only when there is no positive of any kind."""
        denom = 2 * self.tp + self.fp + self.fn
        return None if denom == 0 else (2 * self.tp) / denom

    @property
    def balanced_accuracy(self) -> float | None:
        """Mean of recall and specificity -- ``None`` when either is undefined."""
        recall, specificity = self.recall, self.specificity
        if recall is None or specificity is None:
            return None
        return (recall + specificity) / 2

    @property
    def accuracy(self) -> float | None:
        """``(tp + tn) / total`` -- ``None`` for an empty confusion matrix."""
        total = self.total
        return None if total == 0 else (self.tp + self.tn) / total

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view: integer counts, and rounded rates that are ``None`` where undefined."""
        return {
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "predicted_positives": self.predicted_positives,
            "actual_positives": self.actual_positives,
            "actual_negatives": self.actual_negatives,
            "total": self.total,
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "specificity": _round(self.specificity),
            "f1": _round(self.f1),
            "balanced_accuracy": _round(self.balanced_accuracy),
            "accuracy": _round(self.accuracy),
        }


# --- canonical serialization and hashing ---------------------------------------------------
def canonical_json_bytes(value: Any) -> bytes:
    """The one canonical byte encoding of ``value``: UTF-8, sorted keys, compact, trailing newline.

    A report or a frozen parameter set has exactly one serialization, so it has exactly one SHA-256.
    ``NaN``/``Infinity`` are refused (``allow_nan=False``): they are neither valid JSON nor
    reproducible, and a metric that produced one is a bug to surface, not a byte to hash.
    """
    try:
        text = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"value is not canonical-JSON serializable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


def canonical_json_hash(value: Any) -> str:
    """The SHA-256 hex digest of :func:`canonical_json_bytes` of ``value``."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: str | Path) -> str:
    """The SHA-256 hex digest of a file's exact bytes. Raises if the file cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CalibrationError(f"cannot read {path} to hash: {exc}") from exc
    return digest.hexdigest()


def _normalized_sha256(value: str) -> str:
    if not isinstance(value, str):
        raise CalibrationError(f"expected a SHA-256 hex string, got {type(value).__name__}")
    normalized = value.strip().lower()
    if len(normalized) != _SHA256_HEX_CHARS or any(ch not in _HEX_DIGITS for ch in normalized):
        raise CalibrationError(f"expected a 64-character SHA-256 hex string, got {value!r}")
    return normalized


def verify_artifact_hash(path: str | Path, expected_sha256: str) -> str:
    """Check that ``path``'s bytes hash to ``expected_sha256``; return the digest, or raise.

    The gate a frozen protocol/parameter artifact passes before the final holdout is unsealed: if
    the bytes have changed, the digest has changed, and this refuses loudly rather than letting a
    stale or edited artifact through. Comparison is case-insensitive on the expected hex.
    """
    expected = _normalized_sha256(expected_sha256)
    try:
        actual = sha256_file(path)
    except CalibrationError as exc:
        raise ArtifactVerificationError(
            f"cannot read {path} to verify its hash: {exc}"
        ) from exc
    if actual != expected:
        raise ArtifactVerificationError(
            f"{path} hash mismatch: expected {expected}, got {actual}"
        )
    return actual


# --- report status vocabulary --------------------------------------------------------------
class CalibrationStatus(StrEnum):
    """The Stage 9 report status vocabulary (protocol ``stage9-validation.v1``)."""

    #: A parameter was selected on development against the predeclared objective.
    CALIBRATED = "calibrated"
    #: The gold set exists but the eligibility gate was not met; the production value is kept.
    RETAINED_INSUFFICIENT_EVIDENCE = "retained_insufficient_evidence"
    #: Measured and reported only -- no threshold is selected (e.g. analogy).
    EVALUATION_ONLY = "evaluation_only"
    #: The evidence needed to verify anything does not yet exist (e.g. no genuine dataset).
    NOT_VERIFIABLE = "not_verifiable"


def validate_status(value: str | CalibrationStatus) -> CalibrationStatus:
    """Coerce ``value`` to a :class:`CalibrationStatus`, or raise with the allowed set."""
    try:
        return CalibrationStatus(value)
    except ValueError as exc:
        allowed = ", ".join(status.value for status in CalibrationStatus)
        raise CalibrationError(f"unknown status {value!r}; allowed: {allowed}") from exc


__all__ = [
    "MAX_GRID_POINTS",
    "METRIC_PRECISION",
    "ArtifactVerificationError",
    "BinaryMetrics",
    "CalibrationError",
    "CalibrationStatus",
    "canonical_json_bytes",
    "canonical_json_hash",
    "inclusive_decimal_grid",
    "sha256_file",
    "validate_status",
    "verify_artifact_hash",
]
