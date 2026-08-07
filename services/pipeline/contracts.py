"""Pure contracts for the manual daily intelligence pipeline.

This module deliberately contains no database, Celery, provider, or model imports.  It defines
the stable boundary between a coordinator, its injected stage implementations, and a future
durable lifecycle adapter.

The included graph stops at descriptive intelligence.  Gate G remains closed: crisis
probabilities, predictive rollups, and composite alerts are named as excluded stages so they
cannot be mistaken for work that this coordinator silently performs.
"""

from __future__ import annotations

import datetime
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TypeAlias


class PipelineStage(StrEnum):
    """The executable non-crisis stages, in daily processing order."""

    INGESTION = "ingestion"
    ARTICLE_EMBEDDINGS = "article_embeddings"
    CLUSTERING = "clustering"
    ENTITY_LINKING = "entity_linking"
    EVENT_EMBEDDINGS = "event_embeddings"
    ANALOGIES = "analogies"
    DAILY_BRIEF = "daily_brief"


class ExcludedGateGStage(StrEnum):
    """Predictive stages that must not be wired while Gate G is closed."""

    CRISIS_PREDICTION = "crisis_prediction"
    PREDICTIVE_ROLLUPS = "predictive_rollups"
    COMPOSITE_ALERTS = "composite_alerts"


EXCLUDED_GATE_G_STAGES: tuple[ExcludedGateGStage, ...] = tuple(ExcludedGateGStage)


@dataclass(frozen=True)
class StageDefinition:
    """One stage and the earlier stages whose usable output it requires."""

    stage: PipelineStage
    required_dependencies: tuple[PipelineStage, ...] = ()


# Entity linking and event embeddings are independent enrichments after clustering.  Analogies
# require event embeddings.  The brief only hard-requires clustered events: entity links and
# analogies are optional inputs whose absence is represented as data quality, so either enrichment
# branch may fail without suppressing the entire descriptive brief.
STAGE_DEFINITIONS: tuple[StageDefinition, ...] = (
    StageDefinition(PipelineStage.INGESTION),
    StageDefinition(PipelineStage.ARTICLE_EMBEDDINGS, (PipelineStage.INGESTION,)),
    StageDefinition(PipelineStage.CLUSTERING, (PipelineStage.ARTICLE_EMBEDDINGS,)),
    StageDefinition(PipelineStage.ENTITY_LINKING, (PipelineStage.CLUSTERING,)),
    StageDefinition(PipelineStage.EVENT_EMBEDDINGS, (PipelineStage.CLUSTERING,)),
    StageDefinition(PipelineStage.ANALOGIES, (PipelineStage.EVENT_EMBEDDINGS,)),
    StageDefinition(PipelineStage.DAILY_BRIEF, (PipelineStage.CLUSTERING,)),
)
ORDERED_STAGES: tuple[PipelineStage, ...] = tuple(
    definition.stage for definition in STAGE_DEFINITIONS
)


def _validate_graph() -> None:
    seen: set[PipelineStage] = set()
    for definition in STAGE_DEFINITIONS:
        if definition.stage in seen:
            raise RuntimeError(f"duplicate pipeline stage: {definition.stage.value}")
        missing = set(definition.required_dependencies) - seen
        if missing:
            names = ", ".join(sorted(stage.value for stage in missing))
            raise RuntimeError(
                f"pipeline stage {definition.stage.value} has non-prior dependencies: {names}"
            )
        seen.add(definition.stage)
    if seen != set(PipelineStage):
        missing = set(PipelineStage) - seen
        names = ", ".join(sorted(stage.value for stage in missing))
        raise RuntimeError(f"pipeline graph omits stages: {names}")


_validate_graph()


class StageStatus(StrEnum):
    """One stage's outcome.

    ``partially_failed`` means at least one fan-out item succeeded and at least one failed; its
    output is usable by dependent stages.  ``failed`` is unusable.  ``skipped`` is emitted by the
    coordinator only when a required dependency is unusable.
    """

    SUCCEEDED = "succeeded"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineState(StrEnum):
    """Terminal daily-pipeline states."""

    SUCCEEDED = "succeeded"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


def _json_value(value: object, *, path: str = "output") -> JSONValue:
    """Return a deterministic JSON-safe copy or raise for an unsupported value."""

    # Enum comes first because StrEnum and IntEnum are also instances of their primitive types.
    if isinstance(value, Enum):
        return _json_value(value.value, path=path)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key: {key!r}")
            result[key] = _json_value(nested, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, path=f"{path}[]") for item in value]
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")


def _nonnegative_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class StageItemFailure:
    """A serializable failure for one fan-out item (or ``__stage__`` itself)."""

    item_key: str
    code: str
    message: str
    retryable: bool = False
    error_type: str | None = None

    def __post_init__(self) -> None:
        for name in ("item_key", "code", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonblank string")
        if self.error_type is not None and (
            not isinstance(self.error_type, str) or not self.error_type.strip()
        ):
            raise ValueError("error_type must be None or a nonblank string")

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "item_key": self.item_key,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "error_type": self.error_type,
        }


@dataclass(frozen=True)
class StageResult:
    """The immutable result returned by an injected stage callable."""

    status: StageStatus
    attempted_count: int
    succeeded_count: int
    failures: tuple[StageItemFailure, ...] = ()
    output: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            status = StageStatus(self.status)
        except ValueError as exc:
            raise ValueError(f"unsupported stage status: {self.status!r}") from exc
        object.__setattr__(self, "status", status)

        _nonnegative_int(self.attempted_count, name="attempted_count")
        _nonnegative_int(self.succeeded_count, name="succeeded_count")
        failures = tuple(self.failures)
        if not all(isinstance(failure, StageItemFailure) for failure in failures):
            raise TypeError("failures must contain StageItemFailure values")
        object.__setattr__(self, "failures", failures)

        normalized = _json_value(dict(self.output))
        assert isinstance(normalized, dict)
        object.__setattr__(self, "output", MappingProxyType(normalized))

        if status is StageStatus.SUCCEEDED:
            if failures or self.succeeded_count != self.attempted_count:
                raise ValueError(
                    "a succeeded stage has no failures and succeeds every attempted item"
                )
        elif status is StageStatus.PARTIALLY_FAILED:
            if (
                not failures
                or self.succeeded_count <= 0
                or self.attempted_count != self.succeeded_count + len(failures)
            ):
                raise ValueError(
                    "a partially failed stage needs successes and one failure per failed item"
                )
        elif status is StageStatus.FAILED:
            if not failures or self.succeeded_count != 0 or self.attempted_count != len(failures):
                raise ValueError(
                    "a failed stage needs zero successes and one failure per attempted item"
                )
        elif self.attempted_count != 0 or self.succeeded_count != 0 or failures or self.output:
            raise ValueError("a skipped stage cannot contain attempts, failures, or output")

    @classmethod
    def succeeded(
        cls,
        *,
        item_count: int = 0,
        output: Mapping[str, object] | None = None,
    ) -> StageResult:
        return cls(
            status=StageStatus.SUCCEEDED,
            attempted_count=item_count,
            succeeded_count=item_count,
            output={} if output is None else output,
        )

    @classmethod
    def partially_failed(
        cls,
        *,
        succeeded_count: int,
        failures: tuple[StageItemFailure, ...],
        output: Mapping[str, object] | None = None,
    ) -> StageResult:
        failures = tuple(failures)
        return cls(
            status=StageStatus.PARTIALLY_FAILED,
            attempted_count=succeeded_count + len(failures),
            succeeded_count=succeeded_count,
            failures=failures,
            output={} if output is None else output,
        )

    @classmethod
    def failed(
        cls,
        *failures: StageItemFailure,
        output: Mapping[str, object] | None = None,
    ) -> StageResult:
        failure_tuple = tuple(failures)
        return cls(
            status=StageStatus.FAILED,
            attempted_count=len(failure_tuple),
            succeeded_count=0,
            failures=failure_tuple,
            output={} if output is None else output,
        )

    @classmethod
    def skipped(cls) -> StageResult:
        return cls(
            status=StageStatus.SKIPPED,
            attempted_count=0,
            succeeded_count=0,
        )

    @property
    def usable(self) -> bool:
        """Whether dependent stages may safely consume this result."""

        return self.status in {StageStatus.SUCCEEDED, StageStatus.PARTIALLY_FAILED}

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "status": self.status.value,
            "attempted_count": self.attempted_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": len(self.failures),
            "failures": [failure.to_dict() for failure in self.failures],
            "output": dict(self.output),
        }


@dataclass(frozen=True)
class PipelineIdentity:
    """Stable identity for one calendar date, independent of retries or worker ids."""

    process_date: datetime.date
    process_key: str
    process_id: uuid.UUID

    def __post_init__(self) -> None:
        if isinstance(self.process_date, datetime.datetime) or not isinstance(
            self.process_date, datetime.date
        ):
            raise TypeError("process_date must be a calendar date")
        expected_key = f"daily-pipeline:{self.process_date.isoformat()}"
        expected_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"urn:news-intelligence:{expected_key}",
        )
        if self.process_key != expected_key or self.process_id != expected_id:
            raise ValueError("pipeline identity does not match its process date")

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "process_date": self.process_date.isoformat(),
            "process_key": self.process_key,
            "process_id": str(self.process_id),
        }


def _coerce_process_date(value: datetime.date | str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        raise TypeError("process_date must be a calendar date, not a datetime")
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise TypeError("process_date must be a datetime.date or YYYY-MM-DD string")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"process_date must be YYYY-MM-DD; got {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"process_date must be YYYY-MM-DD; got {value!r}")
    return parsed


def daily_pipeline_identity(process_date: datetime.date | str) -> PipelineIdentity:
    """Build the same key and UUID for every attempt of one process date."""

    resolved = _coerce_process_date(process_date)
    process_key = f"daily-pipeline:{resolved.isoformat()}"
    process_id = uuid.uuid5(uuid.NAMESPACE_URL, f"urn:news-intelligence:{process_key}")
    return PipelineIdentity(
        process_date=resolved,
        process_key=process_key,
        process_id=process_id,
    )


@dataclass(frozen=True)
class StageExecution:
    """A stage result annotated with its graph dependencies and any blockers."""

    stage: PipelineStage
    required_dependencies: tuple[PipelineStage, ...]
    result: StageResult
    blocked_by: tuple[PipelineStage, ...] = ()
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        try:
            stage = PipelineStage(self.stage)
            dependencies = tuple(PipelineStage(value) for value in self.required_dependencies)
            blockers = tuple(PipelineStage(value) for value in self.blocked_by)
        except ValueError as exc:
            raise ValueError("stage execution contains an unknown pipeline stage") from exc
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "required_dependencies", dependencies)
        object.__setattr__(self, "blocked_by", blockers)
        if not isinstance(self.result, StageResult):
            raise TypeError("stage execution result must be a StageResult")
        if self.result.status is StageStatus.SKIPPED:
            if not blockers:
                raise ValueError("a skipped stage must identify at least one blocking dependency")
        elif blockers:
            raise ValueError("only a skipped stage may identify blocking dependencies")
        if not set(blockers) <= set(dependencies):
            raise ValueError("a stage may be blocked only by one of its required dependencies")
        duration = self.duration_ms
        if duration is not None:
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int | float)
                or not math.isfinite(float(duration))
                or float(duration) < 0.0
            ):
                raise ValueError("stage execution duration_ms must be finite and nonnegative")
            object.__setattr__(self, "duration_ms", float(duration))

    def to_dict(self) -> dict[str, JSONValue]:
        value: dict[str, JSONValue] = {
            "stage": self.stage.value,
            "required_dependencies": [
                dependency.value for dependency in self.required_dependencies
            ],
            "blocked_by": [stage.value for stage in self.blocked_by],
            **self.result.to_dict(),
        }
        # Old durable results predate stage timing. Omitting rather than serializing null keeps
        # those snapshots byte-shape compatible when they are read and returned idempotently.
        if self.duration_ms is not None:
            value["duration_ms"] = self.duration_ms
        return value


@dataclass(frozen=True)
class StageContext:
    """Read-only context handed to one stage callable."""

    identity: PipelineIdentity
    stage: PipelineStage
    completed_stages: Mapping[PipelineStage, StageExecution]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "completed_stages",
            MappingProxyType(dict(self.completed_stages)),
        )

    def execution_for(self, stage: PipelineStage) -> StageExecution | None:
        return self.completed_stages.get(stage)


@dataclass(frozen=True)
class PipelineRunResult:
    """A complete, JSON-serializable terminal snapshot."""

    identity: PipelineIdentity
    state: PipelineState
    stages: tuple[StageExecution, ...]
    excluded_gate_g_stages: tuple[ExcludedGateGStage, ...] = EXCLUDED_GATE_G_STAGES
    idempotent_skip: bool = False

    def __post_init__(self) -> None:
        try:
            state = PipelineState(self.state)
        except ValueError as exc:
            raise ValueError(f"unsupported pipeline state: {self.state!r}") from exc
        object.__setattr__(self, "state", state)

        stages = tuple(self.stages)
        if tuple(execution.stage for execution in stages) != ORDERED_STAGES:
            raise ValueError("pipeline result stages must match ORDERED_STAGES exactly")
        for execution, definition in zip(stages, STAGE_DEFINITIONS, strict=True):
            if execution.required_dependencies != definition.required_dependencies:
                raise ValueError(
                    f"pipeline result dependencies drifted for {definition.stage.value}"
                )
        object.__setattr__(self, "stages", stages)

        excluded = tuple(self.excluded_gate_g_stages)
        if excluded != EXCLUDED_GATE_G_STAGES:
            raise ValueError("pipeline result must preserve the complete Gate G exclusion list")
        object.__setattr__(self, "excluded_gate_g_stages", excluded)

        brief = stages[-1].result
        if brief.status in {StageStatus.FAILED, StageStatus.SKIPPED}:
            expected_state = PipelineState.FAILED
        elif any(execution.result.status is not StageStatus.SUCCEEDED for execution in stages):
            expected_state = PipelineState.PARTIALLY_FAILED
        else:
            expected_state = PipelineState.SUCCEEDED
        if state is not expected_state:
            raise ValueError(
                f"pipeline state {state.value!r} disagrees with its stage results "
                f"({expected_state.value!r})"
            )

    @property
    def succeeded(self) -> bool:
        return self.state is PipelineState.SUCCEEDED

    def execution_for(self, stage: PipelineStage) -> StageExecution:
        return self.stages[ORDERED_STAGES.index(stage)]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "identity": self.identity.to_dict(),
            "state": self.state.value,
            "idempotent_skip": self.idempotent_skip,
            "stages": [execution.to_dict() for execution in self.stages],
            "excluded_gate_g_stages": [stage.value for stage in self.excluded_gate_g_stages],
        }
