"""The null-model gate: a composite alert is experimental until it beats the baseline (ADR 0010).

ADR 0010: "composite-score alerts ship only after backtesting shows better precision/lead-time than
`services/crisis_model/baseline.py` on the labeled episode set. Until then, alerts are marked
`experimental` in the UI", over the windows **2007-2009, 2020, and 2023**.

This module is that rule, and only that rule: pure, typed, and free of the database, the clock, and
the backtest runner. The caller measures the candidate model and the baseline over the labeled
episodes and hands the numbers in; the gate decides whether the composite score has earned the
right to be believed. It never measures anything itself, so it can never mark its own homework.

**It fails closed, every way it can fail.** Release requires evidence from *every* required
window, each with at least one labeled episode, and in each the candidate must beat the baseline
on precision **and** on lead time. A missing window, a window with no labels, a tie on either
metric, or no evidence at all leaves the alert experimental -- silence about a window is not a
pass. Numbers that cannot be true of a real backtest (a precision outside 0-1, a NaN, a negative
lead time, a window this ADR does not name, the same window twice) are not a failing grade but a
malformed definition, and they raise :class:`GateEvidenceError` rather than being scored: a gate
that quietly reads a broken measurement as a pass is worse than no gate.

**Only composite scores are gated.** A single-signal condition -- a velocity alert, a threshold on
one observable series -- makes no claim the baseline is a null model *for*; it is what it says it
is, and gating it behind a composite backtest would suppress it forever. The basis is stated by the
caller (:class:`ScoreBasis`) and defaults, everywhere it can, to ``COMPOSITE``: an alert whose
provenance nobody stated is the one that most needs the gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

#: ADR 0010's backtest windows, exactly. The gate needs evidence from each of them.
REQUIRED_WINDOWS: tuple[str, ...] = ("2007-2009", "2020", "2023")


class GateEvidenceError(ValueError):
    """Backtest evidence that cannot be true of a real backtest. Never scored, never promoted."""


class ScoreBasis(StrEnum):
    """What produced the score behind an alert, and therefore whether the null model applies."""

    COMPOSITE = "composite"
    SINGLE_SIGNAL = "single_signal"


@dataclass(frozen=True)
class WindowEvidence:
    """One backtest window's measured comparison of the candidate against the baseline.

    ``labeled_episodes`` is the count of labeled episodes the window was scored on. Zero is not an
    error -- a window can genuinely have no labels yet -- but it is not evidence either, and it
    blocks release.
    """

    window: str
    labeled_episodes: int
    candidate_precision: float
    baseline_precision: float
    candidate_lead_time_days: float
    baseline_lead_time_days: float

    def __post_init__(self) -> None:
        if self.window not in REQUIRED_WINDOWS:
            raise GateEvidenceError(
                f"unknown backtest window {self.window!r}; ADR 0010 names {list(REQUIRED_WINDOWS)}"
            )
        if isinstance(self.labeled_episodes, bool) or not isinstance(self.labeled_episodes, int):
            raise GateEvidenceError(
                f"labeled_episodes must be an int, got {type(self.labeled_episodes).__name__}"
            )
        if self.labeled_episodes < 0:
            raise GateEvidenceError(f"labeled_episodes must be non-negative, got {self.labeled_episodes}")
        for name in ("candidate_precision", "baseline_precision"):
            object.__setattr__(self, name, _precision(getattr(self, name), field=name))
        for name in ("candidate_lead_time_days", "baseline_lead_time_days"):
            object.__setattr__(self, name, _lead_time(getattr(self, name), field=name))

    @property
    def has_labels(self) -> bool:
        """Whether the window was scored on anything at all."""
        return self.labeled_episodes > 0

    @property
    def beats_baseline(self) -> bool:
        """Strictly better precision **and** strictly longer lead time. A tie is not a win."""
        return (
            self.candidate_precision > self.baseline_precision
            and self.candidate_lead_time_days > self.baseline_lead_time_days
        )


@dataclass(frozen=True)
class GateDecision:
    """Whether an alert on this basis may ship as a real alert, and why."""

    released: bool
    basis: ScoreBasis
    reason: str
    blocking_windows: tuple[str, ...] = ()

    @property
    def experimental(self) -> bool:
        """What is persisted to `alerts.experimental`."""
        return not self.released


@dataclass(frozen=True)
class ExperimentalGate:
    """The platform's current backtest evidence, and the verdict it supports.

    Constructed with no evidence -- the state the platform is actually in until the gold-datasets
    backtest lands -- it releases single-signal conditions and holds every composite alert
    experimental. That is the default the service runs with, and it is the conservative one.
    """

    evidence: tuple[WindowEvidence, ...] = ()

    def __post_init__(self) -> None:
        evidence = tuple(self.evidence)
        seen: set[str] = set()
        for item in evidence:
            if not isinstance(item, WindowEvidence):
                raise GateEvidenceError(f"evidence must be WindowEvidence, got {type(item).__name__}")
            if item.window in seen:
                # Two verdicts for one window is not a window that passed and a window that failed;
                # it is a backtest whose output cannot be trusted to mean anything.
                raise GateEvidenceError(f"duplicate backtest evidence for window {item.window!r}")
            seen.add(item.window)
        object.__setattr__(self, "evidence", evidence)

    def decide(self, basis: ScoreBasis | str = ScoreBasis.COMPOSITE) -> GateDecision:
        """Decide whether an alert on ``basis`` is released or stays experimental."""
        basis = ScoreBasis(basis)
        if basis is ScoreBasis.SINGLE_SIGNAL:
            return GateDecision(
                released=True,
                basis=basis,
                reason=(
                    "the null-model gate covers composite-score alerts; a single-signal condition "
                    "makes no claim the baseline is a null model for"
                ),
            )

        by_window = {item.window: item for item in self.evidence}
        blocking: list[str] = []
        failures: list[str] = []
        for window in REQUIRED_WINDOWS:
            item = by_window.get(window)
            if item is None:
                failures.append(f"{window}: no backtest evidence")
            elif not item.has_labels:
                failures.append(f"{window}: no labeled episodes")
            elif not item.beats_baseline:
                failures.append(
                    f"{window}: precision {item.candidate_precision} vs baseline "
                    f"{item.baseline_precision}, lead time {item.candidate_lead_time_days}d vs "
                    f"baseline {item.baseline_lead_time_days}d -- the candidate must beat both"
                )
            else:
                continue
            blocking.append(window)

        if blocking:
            return GateDecision(
                released=False,
                basis=basis,
                reason="composite alerts stay experimental -- " + "; ".join(failures),
                blocking_windows=tuple(blocking),
            )
        return GateDecision(
            released=True,
            basis=basis,
            reason=(
                "the candidate beats the baseline on precision and lead time in every labeled "
                f"window ({', '.join(REQUIRED_WINDOWS)})"
            ),
        )


def _finite(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise GateEvidenceError(f"{field} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GateEvidenceError(f"{field} must be finite, got {value!r}")
    return number


def _precision(value: object, *, field: str) -> float:
    number = _finite(value, field=field)
    if not 0.0 <= number <= 1.0:
        raise GateEvidenceError(f"{field} must be a precision within 0-1, got {value!r}")
    return number


def _lead_time(value: object, *, field: str) -> float:
    number = _finite(value, field=field)
    if number < 0.0:
        raise GateEvidenceError(f"{field} must be a non-negative number of days, got {value!r}")
    return number
