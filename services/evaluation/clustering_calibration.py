"""Clustering (single-link cosine threshold) evaluator for ``stage9-validation.v1``.

``docs/evaluation/stage9-validation-protocol.md`` §4.1 fixes the *written* clustering procedure:
sweep the inclusive grid ``0.700..0.950`` step ``0.005`` over labeled same-event / different-event
article pairs and select the **feasible precision knee** (nonzero predicted positives and
precision ≥ 0.90; then maximize recall, then F1, then proximity to 0.80, then the higher
threshold). This module implements exactly that procedure as pure, offline functions built on the
shared primitives in :mod:`services.evaluation.calibration`.

It selects **nothing** in production: no labeled clustering pair dataset exists (ADR 0004's
precondition), so :func:`clustering_not_verifiable_report` records status ``not_verifiable`` and the
live **0.80** threshold stays unchanged. The sweep/selection functions run only against synthetic
observations supplied by a caller; they load no dataset and read no threshold from disk.

Determinism: observations are ordered by ``pair_id``, the grid is an exact ``Decimal`` sweep, and
selection is a total order (every tie-break resolves to a unique grid point). No clock, no
randomness, no I/O.
"""

from __future__ import annotations

import argparse
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from services.evaluation.calibration import (
    BinaryMetrics,
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    inclusive_decimal_grid,
    sha256_file,
)

#: Protocol this evaluator obeys.
PROTOCOL_ID = "stage9-validation.v1"
#: The clustering algorithm whose one threshold this evaluator would tune.
CLUSTERING_ALGORITHM_ID = "single-link-cosine.v1"
#: The embedding space cosine similarity is measured in (ADR 0004).
EMBEDDING_MODEL = "text-embedding-3-small"
#: The configured, still-unpinned embedding version string.
EMBEDDING_MODEL_VERSION = "current"

#: The §4.1 candidate grid bounds (decimal strings -- never floats -- for a reproducible sweep).
CLUSTERING_GRID_START = "0.700"
CLUSTERING_GRID_STOP = "0.950"
CLUSTERING_GRID_STEP = "0.005"

#: Feasibility floor for the precision knee.
PRECISION_KNEE_MIN = 0.90
#: The live threshold selection is pulled toward on a recall/F1 tie.
_KNEE_ANCHOR = Decimal("0.80")
#: The live default clustering threshold, reported unchanged.
LIVE_CLUSTERING_THRESHOLD = "0.80"
#: The unvalidated roadmap target (ADR 0004 / §8.4); informational only.
ROADMAP_CLUSTERING_THRESHOLD = "0.82"

_SHA256_HEX_CHARS = 64
_HEX_DIGITS = frozenset("0123456789abcdef")

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: The protocol document whose exact SHA-256 the canonical report records.
PROTOCOL_DOC_PATH = _REPO_ROOT / "docs" / "evaluation" / "stage9-validation-protocol.md"
#: The canonical development-report location this evaluator writes.
DEFAULT_OUTPUT_PATH = _REPO_ROOT / "evaluation" / "stage9" / "development" / "clustering.json"


class ClusteringCalibrationError(CalibrationError):
    """A clustering calibration input is invalid. Raised before any threshold is evaluated."""


# --- labeled pair observation --------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PairObservation:
    """One labeled article pair: its cosine ``similarity`` and whether it is truly the same event.

    Validated on construction: ``pair_id`` is a non-empty, non-whitespace string, ``similarity`` is a
    finite real number in ``[-1, 1]`` (the cosine range), and ``same_event`` is an actual ``bool`` --
    never an ``int`` or truthy stand-in. The id is validated, not rewritten: a legitimate id is stored
    verbatim. Collection-wide uniqueness of ``pair_id`` is checked when a set of observations is
    evaluated (:func:`evaluate_threshold` / :func:`sweep`).
    """

    pair_id: str
    similarity: float
    same_event: bool

    def __post_init__(self) -> None:
        if not isinstance(self.pair_id, str) or not self.pair_id.strip():
            raise ClusteringCalibrationError(
                f"pair_id must be a non-empty, non-whitespace string, got {self.pair_id!r}"
            )
        similarity = self.similarity
        if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
            raise ClusteringCalibrationError(
                f"similarity must be a real number, got {similarity!r}"
            )
        if not math.isfinite(similarity):
            raise ClusteringCalibrationError(
                f"similarity must be finite (not NaN/Infinity), got {similarity!r}"
            )
        if not -1.0 <= similarity <= 1.0:
            raise ClusteringCalibrationError(
                f"similarity must lie in [-1, 1] (cosine range), got {similarity!r}"
            )
        if not isinstance(self.same_event, bool):
            raise ClusteringCalibrationError(
                f"same_event must be a bool, got {type(self.same_event).__name__}"
            )

    def predicted_same_event(self, cutoff: float) -> bool:
        """Would this pair be placed in one cluster at ``cutoff``? Inclusive: ``similarity >= cutoff``."""
        return self.similarity >= cutoff


# --- one threshold's evaluation ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ThresholdEvaluation:
    """A candidate ``threshold`` (exact :class:`~decimal.Decimal`) and its :class:`BinaryMetrics`."""

    threshold: Decimal
    metrics: BinaryMetrics

    def __post_init__(self) -> None:
        if not isinstance(self.threshold, Decimal) or not self.threshold.is_finite():
            raise ClusteringCalibrationError(
                f"threshold must be a finite Decimal, got {self.threshold!r}"
            )
        if not isinstance(self.metrics, BinaryMetrics):
            raise ClusteringCalibrationError("metrics must be a BinaryMetrics")

    @property
    def is_feasible(self) -> bool:
        """Nonzero predicted positives and precision ≥ 0.90; an undefined precision is infeasible."""
        precision = self.metrics.precision
        return (
            self.metrics.predicted_positives > 0
            and precision is not None
            and precision >= PRECISION_KNEE_MIN
        )


def _to_threshold_decimal(value: Decimal | int | str) -> Decimal:
    """Coerce a candidate threshold to a finite :class:`~decimal.Decimal` in ``[0, 1]``.

    Floats and bools are refused; the parsed value must be finite and lie in the inclusive ``[0, 1]``
    range (production threshold semantics -- exactly ``0`` and ``1`` are accepted). The exact Decimal
    input is preserved and returned verbatim.
    """
    if isinstance(value, bool):
        raise ClusteringCalibrationError(f"threshold must not be a bool, got {value!r}")
    if isinstance(value, float):
        raise ClusteringCalibrationError(
            f"threshold must be a Decimal, int or decimal string, not a float ({value!r})"
        )
    if not isinstance(value, (Decimal, int, str)):
        raise ClusteringCalibrationError(
            f"threshold must be a Decimal, int or str, got {type(value).__name__}"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ClusteringCalibrationError(f"threshold is not a valid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ClusteringCalibrationError(f"threshold must be a finite decimal, got {value!r}")
    if not Decimal(0) <= result <= Decimal(1):
        raise ClusteringCalibrationError(
            f"threshold must lie in [0, 1] (inclusive), got {value!r}"
        )
    return result


def _ordered_unique(observations: Iterable[PairObservation]) -> tuple[PairObservation, ...]:
    """Validate a collection of observations and return it ordered by ``pair_id``.

    Rejects an empty collection, a non-:class:`PairObservation` element, and any duplicate
    ``pair_id`` -- so a sweep can never double-count a pair or depend on insertion order.
    """
    items = tuple(observations)
    if not items:
        raise ClusteringCalibrationError("at least one PairObservation is required")
    seen: set[str] = set()
    for observation in items:
        if not isinstance(observation, PairObservation):
            raise ClusteringCalibrationError(
                f"every observation must be a PairObservation, got {type(observation).__name__}"
            )
        if observation.pair_id in seen:
            raise ClusteringCalibrationError(f"duplicate pair_id: {observation.pair_id!r}")
        seen.add(observation.pair_id)
    return tuple(sorted(items, key=lambda observation: observation.pair_id))


def clustering_grid() -> tuple[Decimal, ...]:
    """The §4.1 candidate grid: ``0.700..0.950`` step ``0.005`` -- 51 inclusive Decimal points."""
    return inclusive_decimal_grid(CLUSTERING_GRID_START, CLUSTERING_GRID_STOP, CLUSTERING_GRID_STEP)


def _evaluate(observations: tuple[PairObservation, ...], threshold: Decimal) -> ThresholdEvaluation:
    cutoff = float(threshold)
    predicted = [observation.predicted_same_event(cutoff) for observation in observations]
    actual = [observation.same_event for observation in observations]
    return ThresholdEvaluation(threshold=threshold, metrics=BinaryMetrics.from_predictions(predicted, actual))


def evaluate_threshold(
    observations: Iterable[PairObservation], threshold: Decimal | int | str
) -> ThresholdEvaluation:
    """Score one ``threshold`` over the labeled pairs: predicted-positive iff ``similarity >= threshold``."""
    ordered = _ordered_unique(observations)
    return _evaluate(ordered, _to_threshold_decimal(threshold))


def sweep(observations: Iterable[PairObservation]) -> tuple[ThresholdEvaluation, ...]:
    """Evaluate every §4.1 grid threshold over the labeled pairs, ordered by ascending threshold."""
    ordered = _ordered_unique(observations)
    return tuple(_evaluate(ordered, threshold) for threshold in clustering_grid())


def _selection_key(evaluation: ThresholdEvaluation) -> tuple[float, float, Decimal, Decimal]:
    """Lexicographic knee key: max recall, then F1, then proximity to 0.80, then higher threshold.

    An undefined recall/F1 sorts to ``-inf`` -- it can never masquerade as a perfect ``1.0``. (For a
    feasible candidate both are always defined, since precision ≥ 0.90 with predicted positives
    forces ``tp > 0``; the guard is defensive.)
    """
    metrics = evaluation.metrics
    recall = metrics.recall if metrics.recall is not None else float("-inf")
    f1 = metrics.f1 if metrics.f1 is not None else float("-inf")
    proximity = abs(evaluation.threshold - _KNEE_ANCHOR)
    return (recall, f1, -proximity, evaluation.threshold)


def select_precision_knee(
    evaluations: Iterable[ThresholdEvaluation],
) -> ThresholdEvaluation | None:
    """Select the feasible precision knee, or ``None`` when no threshold is feasible.

    Feasible = nonzero predicted positives and precision ≥ 0.90. Among feasible thresholds, pick the
    one that lexicographically maximizes recall, then F1, then proximity to 0.80, then the higher
    threshold. Every candidate carries a distinct grid threshold, so the winner is unique.
    """
    feasible = [evaluation for evaluation in evaluations if evaluation.is_feasible]
    if not feasible:
        return None
    return max(feasible, key=_selection_key)


# --- the not-verifiable report -------------------------------------------------------------
def _validate_protocol_hash(protocol_hash: str) -> str:
    """Confirm ``protocol_hash`` is a 64-char SHA-256 hex string, without opening any file."""
    if isinstance(protocol_hash, bool) or not isinstance(protocol_hash, str):
        raise ClusteringCalibrationError(
            f"protocol_hash must be a 64-character SHA-256 hex string, got {protocol_hash!r}"
        )
    normalized = protocol_hash.strip().lower()
    if len(normalized) != _SHA256_HEX_CHARS or any(ch not in _HEX_DIGITS for ch in normalized):
        raise ClusteringCalibrationError(
            f"protocol_hash must be a 64-character SHA-256 hex string, got {protocol_hash!r}"
        )
    return normalized


def clustering_not_verifiable_report(protocol_hash: str) -> dict[str, Any]:
    """Build the canonical, JSON-safe §5 report recording clustering as ``not_verifiable``.

    Nothing is selected and nothing changes: no labeled same-event / different-event article-pair
    dataset exists, so §4.1's grid and precision knee are the *written* procedure only. The live
    **0.80** threshold is reported unchanged and the roadmap **0.82** is flagged informational. The
    supplied ``protocol_hash`` (the protocol document's SHA-256) is validated for shape only -- this
    function opens no file.
    """
    normalized_hash = _validate_protocol_hash(protocol_hash)
    grid = clustering_grid()
    return {
        "protocol": {"id": PROTOCOL_ID, "sha256": normalized_hash},
        "domain": "clustering",
        "status": CalibrationStatus.NOT_VERIFIABLE.value,
        "split": "development",
        "input_hashes": {},
        "input_hashes_reason": (
            "no clustering gold asset exists; no split or artifact was consumed"
        ),
        "model_versions": {
            "embedding_model": EMBEDDING_MODEL,
            "embedding_model_version": EMBEDDING_MODEL_VERSION,
            "clustering_algorithm": CLUSTERING_ALGORITHM_ID,
        },
        "evaluated_grid": {
            "start": CLUSTERING_GRID_START,
            "stop": CLUSTERING_GRID_STOP,
            "step": CLUSTERING_GRID_STEP,
            "points": len(grid),
            "metrics_run": False,
            "description": (
                "single-link cosine threshold sweep from §4.1; the written procedure only -- "
                "no labeled pairs exist to run it against, so no metrics were computed"
            ),
        },
        "selected_parameters": None,
        "effective_parameters": {
            "clustering_threshold": LIVE_CLUSTERING_THRESHOLD,
            "changed": False,
            "roadmap_threshold": ROADMAP_CLUSTERING_THRESHOLD,
            "roadmap_threshold_informational_only": True,
        },
        "metrics": None,
        "blockers": [
            "no genuine ~100-pair same-event / different-event article dataset exists "
            "(ADR 0004 precondition for validating the clustering threshold)",
            "the embedding model version is the unpinned string 'current'; cosine distributions "
            "are only comparable once it is pinned to a concrete version",
        ],
        "limitations": [
            "no labeled same-event / different-event article pairs exist, so §4.1's grid and "
            "precision-knee objective are written but not run and change nothing",
            "the live 0.80 threshold is retained unchanged; the roadmap 0.82 is informational only, "
            "never a validated tuning result",
        ],
    }


# --- the canonical development report on disk ----------------------------------------------
def build_clustering_report(*, protocol_path: Path = PROTOCOL_DOC_PATH) -> dict[str, Any]:
    """The §5 clustering report, with the protocol document's exact SHA-256 as its only file read.

    The protocol hash is the one and only real hash in a ``not_verifiable`` clustering report: no
    clustering gold asset exists, so ``input_hashes`` stays empty. This reads the protocol document
    to hash it and opens nothing else.
    """
    return clustering_not_verifiable_report(sha256_file(protocol_path))


def write_clustering_report(
    output_path: Path | str, *, protocol_path: Path = PROTOCOL_DOC_PATH
) -> bytes:
    """Serialize the clustering report to ``output_path`` as canonical bytes, refusing to overwrite.

    Builds the report first (so a bad protocol path raises before any file is touched), creates
    parent directories, writes atomically (temp file then ``os.replace``), and refuses if the path
    already exists -- a checked-in report is never silently overwritten. Returns the exact bytes.
    """
    path = Path(output_path)
    if path.exists():
        raise ClusteringCalibrationError(f"refusing to overwrite existing report at {path}")
    data = canonical_json_bytes(build_clustering_report(protocol_path=protocol_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def main(argv: Iterable[str] | None = None) -> int:
    """CLI: write the canonical not-verifiable report (no clock, no randomness, no git revision)."""
    parser = argparse.ArgumentParser(
        description="Stage 9 clustering (single-link cosine) not-verifiable report."
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args(list(argv) if argv is not None else None)
    data = write_clustering_report(args.output)
    print(f"wrote {len(data)} bytes to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CLUSTERING_ALGORITHM_ID",
    "CLUSTERING_GRID_START",
    "CLUSTERING_GRID_STEP",
    "CLUSTERING_GRID_STOP",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_VERSION",
    "DEFAULT_OUTPUT_PATH",
    "LIVE_CLUSTERING_THRESHOLD",
    "PRECISION_KNEE_MIN",
    "PROTOCOL_DOC_PATH",
    "PROTOCOL_ID",
    "ROADMAP_CLUSTERING_THRESHOLD",
    "ClusteringCalibrationError",
    "PairObservation",
    "ThresholdEvaluation",
    "build_clustering_report",
    "clustering_grid",
    "clustering_not_verifiable_report",
    "evaluate_threshold",
    "select_precision_knee",
    "sweep",
    "write_clustering_report",
]
