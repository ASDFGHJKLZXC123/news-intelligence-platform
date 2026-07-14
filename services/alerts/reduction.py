"""`what_could_reduce_risk` conditions (ADR 0010).

Conditions are stored per-alert in the `alerts.what_could_reduce_risk` JSONB column and
checked on every run; when the machine-checkable ones are met they drive the downgrade.

Two kinds of condition, and the distinction is the point:

* :class:`ReductionPredicate` -- structured ``(signal_ref, comparator, threshold)``,
  evaluated numerically every run.
* :class:`ManualReviewCondition` -- free text ("the ECB opens a swap line"). It is *never*
  machine-evaluated; it is surfaced for weekly manual review.

Failure is asymmetric on purpose. A malformed *definition* raises: a typo in a comparator is
a config bug and must not sit silently in the database. A missing or non-numeric *signal
value* at evaluation time does not raise and does not count as met -- it is reported as
``unevaluable``, so absent data can never produce a false all-clear.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from services.alerts.numeric import Numeric, to_decimal


class Comparator(StrEnum):
    """Supported comparators.

    Only strict/inclusive inequalities are supported. Equality is deliberately absent:
    "signal equals 2.5" is not a deterministic test against floating-point pipeline output.
    """

    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"


class ReductionStatus(StrEnum):
    """Outcome of checking one condition."""

    MET = "met"
    NOT_MET = "not_met"
    UNEVALUABLE = "unevaluable"
    MANUAL_REVIEW = "manual_review"


_COMPARISONS = {
    Comparator.LT: lambda observed, threshold: observed < threshold,
    Comparator.LTE: lambda observed, threshold: observed <= threshold,
    Comparator.GT: lambda observed, threshold: observed > threshold,
    Comparator.GTE: lambda observed, threshold: observed >= threshold,
}


@dataclass(frozen=True)
class ReductionPredicate:
    """A machine-checkable reduction condition."""

    signal_ref: str
    comparator: Comparator
    threshold: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_ref", _require_text(self.signal_ref, field="signal_ref"))
        object.__setattr__(self, "comparator", _coerce_comparator(self.comparator))
        object.__setattr__(self, "threshold", to_decimal(self.threshold, field="threshold"))

    @property
    def machine_checkable(self) -> bool:
        return True

    def evaluate(self, signals: Mapping[str, Any]) -> ReductionOutcome:
        """Check this predicate against the run's signal values."""
        if self.signal_ref not in signals:
            return ReductionOutcome(self, ReductionStatus.UNEVALUABLE, None, "signal not reported")
        observed = _numeric_or_none(signals[self.signal_ref])
        if observed is None:
            return ReductionOutcome(
                self,
                ReductionStatus.UNEVALUABLE,
                None,
                f"signal value is not a finite number: {signals[self.signal_ref]!r}",
            )
        met = _COMPARISONS[self.comparator](observed, self.threshold)
        status = ReductionStatus.MET if met else ReductionStatus.NOT_MET
        return ReductionOutcome(
            self,
            status,
            observed,
            f"{self.signal_ref}={observed} {self.comparator.value} {self.threshold}",
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "signal_ref": self.signal_ref,
            "comparator": self.comparator.value,
            "threshold": str(self.threshold),
        }


@dataclass(frozen=True)
class ManualReviewCondition:
    """A free-text reduction condition. Surfaced for review, never machine-evaluated."""

    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _require_text(self.text, field="text"))

    @property
    def machine_checkable(self) -> bool:
        return False

    def evaluate(self, signals: Mapping[str, Any]) -> ReductionOutcome:
        """Never machine-evaluated; always reported as manual-review-only."""
        return ReductionOutcome(
            self, ReductionStatus.MANUAL_REVIEW, None, "free-text condition: manual review only"
        )

    def as_payload(self) -> dict[str, Any]:
        return {"text": self.text}


ReductionCondition = ReductionPredicate | ManualReviewCondition


@dataclass(frozen=True)
class ReductionOutcome:
    """The result of checking one condition on one run."""

    condition: ReductionCondition
    status: ReductionStatus
    observed: Decimal | None
    detail: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "condition": self.condition.as_payload(),
            "status": self.status.value,
            "observed": None if self.observed is None else str(self.observed),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReductionAssessment:
    """The aggregate outcome lifecycle code consumes to drive a downgrade."""

    outcomes: tuple[ReductionOutcome, ...]

    @property
    def predicates_met(self) -> bool:
        """Whether every machine-checkable predicate is met, and there is at least one.

        False when the alert carries no machine-checkable predicate, and false when any
        predicate is unevaluable: a reduction that cannot be checked is never claimed as met.
        """
        checked = [o for o in self.outcomes if o.condition.machine_checkable]
        return bool(checked) and all(o.status is ReductionStatus.MET for o in checked)

    @property
    def requires_manual_review(self) -> bool:
        return any(o.status is ReductionStatus.MANUAL_REVIEW for o in self.outcomes)

    @property
    def has_unevaluable(self) -> bool:
        return any(o.status is ReductionStatus.UNEVALUABLE for o in self.outcomes)

    def as_payload(self) -> dict[str, Any]:
        return {
            "predicates_met": self.predicates_met,
            "requires_manual_review": self.requires_manual_review,
            "has_unevaluable": self.has_unevaluable,
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
        }


def parse_reduction_conditions(payload: Any) -> tuple[ReductionCondition, ...]:
    """Parse the `what_could_reduce_risk` JSONB payload into typed conditions.

    Accepts ``None`` (no conditions), a list of items, or a single item. An item is either a
    string (free text), a mapping with ``signal_ref``/``comparator``/``threshold``
    (a predicate), or a mapping with ``text`` (free text). Anything else raises.
    """
    if payload is None:
        return ()
    items: Sequence[Any]
    if isinstance(payload, str | Mapping):
        items = [payload]
    elif isinstance(payload, Iterable):
        items = list(payload)
    else:
        raise TypeError(f"what_could_reduce_risk must be a list, mapping, or string, got {payload!r}")
    return tuple(_parse_condition(item) for item in items)


def evaluate_reduction_conditions(
    conditions: Iterable[ReductionCondition],
    signals: Mapping[str, Any],
) -> ReductionAssessment:
    """Check every condition against this run's signals and aggregate the outcome."""
    return ReductionAssessment(tuple(condition.evaluate(signals) for condition in conditions))


def _parse_condition(item: Any) -> ReductionCondition:
    if isinstance(item, str):
        return ManualReviewCondition(item)
    if not isinstance(item, Mapping):
        raise TypeError(f"reduction condition must be a mapping or string, got {item!r}")
    if "signal_ref" in item or "comparator" in item or "threshold" in item:
        missing = [key for key in ("signal_ref", "comparator", "threshold") if key not in item]
        if missing:
            raise ValueError(f"structured reduction condition is missing {missing}: {dict(item)!r}")
        return ReductionPredicate(
            signal_ref=item["signal_ref"],
            comparator=item["comparator"],
            threshold=_threshold_from_payload(item["threshold"]),
        )
    if "text" in item:
        return ManualReviewCondition(item["text"])
    raise ValueError(f"unrecognised reduction condition: {dict(item)!r}")


def _threshold_from_payload(value: Any) -> Numeric:
    """Accept a JSON number, or the string form `as_payload` round-trips."""
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"threshold is not a valid number: {value!r}") from exc
    return value


def _coerce_comparator(value: Any) -> Comparator:
    if isinstance(value, Comparator):
        return value
    if not isinstance(value, str):
        raise TypeError(f"comparator must be a string, got {type(value).__name__}")
    try:
        return Comparator(value.strip().lower())
    except ValueError as exc:
        supported = ", ".join(sorted(c.value for c in Comparator))
        raise ValueError(f"unsupported comparator {value!r}; supported: {supported}") from exc


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be blank")
    return text


def _numeric_or_none(value: Any) -> Decimal | None:
    """Coerce a reported signal value, or ``None`` when it is missing/non-numeric/non-finite.

    Strings are non-numeric even when they look like numbers: a signal that arrives as text
    is a pipeline defect, and guessing would risk a false all-clear.
    """
    try:
        return to_decimal(value, field="signal value")
    except (TypeError, ValueError):
        return None
