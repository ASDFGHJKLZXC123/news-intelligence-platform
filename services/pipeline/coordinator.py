"""Dependency-aware coordinator for a manually triggered daily pipeline.

The coordinator is deliberately an application service, not a scheduler and not a Celery task.
Every executable stage and the lifecycle store are injected.  Production wiring can therefore
bind existing workers and a SQLAlchemy lifecycle adapter, while unit/integration tests use
deterministic callables and stores without network or database side effects.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Protocol, runtime_checkable

from services.pipeline.contracts import (
    EXCLUDED_GATE_G_STAGES,
    ORDERED_STAGES,
    STAGE_DEFINITIONS,
    ExcludedGateGStage,
    PipelineIdentity,
    PipelineRunResult,
    PipelineStage,
    PipelineState,
    StageContext,
    StageExecution,
    StageItemFailure,
    StageResult,
    StageStatus,
    daily_pipeline_identity,
)

StageRunner = Callable[[StageContext], StageResult]


class PipelineConfigurationError(ValueError):
    """The injected runner map is missing, duplicates, or includes a forbidden stage."""


class PipelineLifecycleError(RuntimeError):
    """A lifecycle store returned a completed result for the wrong identity or state."""


@runtime_checkable
class PipelineLifecycleStore(Protocol):
    """Durability boundary for atomic idempotency and terminal persistence.

    ``begin_or_get_succeeded`` should atomically claim/start ``identity`` unless that identity
    already has a succeeded terminal result.  In the latter case it returns the stored snapshot,
    allowing the coordinator to skip every stage.  Partial and failed dates are intentionally
    eligible for another manual attempt.
    """

    def begin_or_get_succeeded(
        self,
        identity: PipelineIdentity,
    ) -> PipelineRunResult | None: ...

    def save_terminal(self, result: PipelineRunResult) -> None: ...


class NoopPipelineLifecycleStore:
    """Lifecycle implementation for callers that do not need durable idempotency."""

    def begin_or_get_succeeded(
        self,
        identity: PipelineIdentity,
    ) -> PipelineRunResult | None:
        del identity
        return None

    def save_terminal(self, result: PipelineRunResult) -> None:
        del result


def _normalize_runners(
    supplied: Mapping[PipelineStage | str, StageRunner],
) -> dict[PipelineStage, StageRunner]:
    normalized: dict[PipelineStage, StageRunner] = {}
    excluded_values = {stage.value for stage in EXCLUDED_GATE_G_STAGES}

    for raw_stage, runner in supplied.items():
        raw_value = raw_stage.value if isinstance(raw_stage, ExcludedGateGStage) else raw_stage
        if raw_value in excluded_values:
            raise PipelineConfigurationError(
                f"Gate G is closed; stage {raw_value!r} cannot be configured"
            )
        try:
            stage = PipelineStage(raw_stage)
        except (TypeError, ValueError) as exc:
            raise PipelineConfigurationError(f"unknown pipeline stage: {raw_stage!r}") from exc
        if stage in normalized:
            raise PipelineConfigurationError(f"duplicate runner for stage {stage.value!r}")
        if not callable(runner):
            raise PipelineConfigurationError(f"runner for stage {stage.value!r} is not callable")
        normalized[stage] = runner

    missing = set(ORDERED_STAGES) - set(normalized)
    if missing:
        names = ", ".join(sorted(stage.value for stage in missing))
        raise PipelineConfigurationError(f"missing stage runners: {names}")
    return normalized


def _stage_exception(exc: Exception) -> StageResult:
    message = str(exc).strip() or "stage callable raised without an error message"
    return StageResult.failed(
        StageItemFailure(
            item_key="__stage__",
            code="unhandled_stage_exception",
            message=message,
            retryable=False,
            error_type=type(exc).__name__,
        )
    )


def _invalid_result(message: str) -> StageResult:
    return StageResult.failed(
        StageItemFailure(
            item_key="__stage__",
            code="invalid_stage_result",
            message=message,
            retryable=False,
            error_type="PipelineConfigurationError",
        )
    )


def normalize_fatal_exceptions(
    supplied: tuple[type[BaseException], ...],
) -> tuple[type[BaseException], ...]:
    """Validate a caller-declared fatal-exception tuple, or raise ``PipelineConfigurationError``.

    Public because the production stage adapter (``workers.pipeline_stages``) has to re-raise the
    very same types one level below the coordinator -- the soft time limit almost always lands
    inside a task body, not between two stages -- and both layers should reject a misconfigured
    tuple identically at construction time. Left unvalidated, a bad entry would surface much later
    as a ``TypeError`` from the ``except`` clause itself, i.e. as a spurious stage/item failure.
    """

    try:
        normalized = tuple(supplied)
    except TypeError as exc:
        # A bare class instead of a tuple is the easy mistake; ``tuple()`` calls it not iterable.
        raise PipelineConfigurationError(
            f"fatal_exceptions must be a tuple of exception classes; got {supplied!r}"
        ) from exc
    for candidate in normalized:
        if not (isinstance(candidate, type) and issubclass(candidate, BaseException)):
            raise PipelineConfigurationError(
                f"fatal exception {candidate!r} is not an exception class"
            )
    return normalized


def contains_fatal_exception(
    cause: BaseException,
    fatal_exceptions: tuple[type[BaseException], ...],
) -> bool:
    """Return whether ``cause`` or any nested exception-group leaf is fatal.

    Cleanup code can preserve an original timeout alongside rollback/close faults in an
    ``ExceptionGroup``.  Matching only the outer group would then turn a worker-shutdown signal
    into an ordinary stage failure and continue launching work during the graceful-stop window.
    """

    if isinstance(cause, fatal_exceptions):
        return True
    if isinstance(cause, BaseExceptionGroup):
        return any(
            contains_fatal_exception(nested, fatal_exceptions) for nested in cause.exceptions
        )
    return False


def _invoke_stage(
    runner: StageRunner,
    context: StageContext,
    fatal_exceptions: tuple[type[BaseException], ...] = (),
) -> StageResult:
    try:
        result = runner(context)
    except fatal_exceptions:
        # Not a stage outcome: the caller declared these to mean "this process is being torn
        # down". Converting one into a StageResult would let the run continue into the next
        # stage and swallow the signal. An empty tuple matches nothing.
        raise
    except Exception as exc:
        if contains_fatal_exception(exc, fatal_exceptions):
            raise
        return _stage_exception(exc)
    if not isinstance(result, StageResult):
        return _invalid_result(
            f"stage callable returned {type(result).__name__}; expected StageResult"
        )
    if result.status is StageStatus.SKIPPED:
        return _invalid_result(
            "stage callable returned skipped; dependency skips are owned by the coordinator"
        )
    return result


def _terminal_state(executions: tuple[StageExecution, ...]) -> PipelineState:
    brief = executions[-1].result
    if brief.status in {StageStatus.FAILED, StageStatus.SKIPPED}:
        return PipelineState.FAILED
    if any(execution.result.status is not StageStatus.SUCCEEDED for execution in executions):
        return PipelineState.PARTIALLY_FAILED
    return PipelineState.SUCCEEDED


class DailyPipelineCoordinator:
    """Run all non-crisis stages once for an explicit calendar date."""

    def __init__(
        self,
        *,
        stage_runners: Mapping[PipelineStage | str, StageRunner],
        lifecycle_store: PipelineLifecycleStore | None = None,
        fatal_exceptions: tuple[type[BaseException], ...] = (),
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        """``fatal_exceptions`` are propagated verbatim instead of becoming stage failures.

        Everything a stage raises is normally recorded as that stage's failure so the remaining
        independent branches still run.  A runtime's shutdown signal is not a stage outcome,
        though: a Celery worker raises ``SoftTimeLimitExceeded`` inside whichever stage happens
        to be executing, and absorbing it would spend the graceful window before the hard kill
        starting the *next* stage.  The caller names those types; this module deliberately stays
        free of any framework import, so it never names one itself.

        This guard only covers what reaches the coordinator.  A runner that catches broadly on
        its own -- the production adapter's per-item loop, for one -- has to be told the same
        types (see :func:`normalize_fatal_exceptions`), or it absorbs the signal below this
        frame and the coordinator never sees it.
        """

        self._stage_runners = _normalize_runners(stage_runners)
        self._lifecycle_store = (
            lifecycle_store if lifecycle_store is not None else NoopPipelineLifecycleStore()
        )
        self._fatal_exceptions = normalize_fatal_exceptions(fatal_exceptions)
        self._monotonic_ns = monotonic_ns

    def run(self, process_date: datetime.date | str) -> PipelineRunResult:
        """Run the dependency graph, or return the stored succeeded result without rerunning."""

        identity = daily_pipeline_identity(process_date)
        existing = self._lifecycle_store.begin_or_get_succeeded(identity)
        if existing is not None:
            self._validate_existing(existing, identity)
            return replace(existing, idempotent_skip=True)

        completed: dict[PipelineStage, StageExecution] = {}
        for definition in STAGE_DEFINITIONS:
            blockers = tuple(
                dependency
                for dependency in definition.required_dependencies
                if not completed[dependency].result.usable
            )
            if blockers:
                result = StageResult.skipped()
                duration_ms = 0.0
            else:
                context = StageContext(
                    identity=identity,
                    stage=definition.stage,
                    completed_stages=completed,
                )
                started_ns = self._monotonic_ns()
                result = _invoke_stage(
                    self._stage_runners[definition.stage],
                    context,
                    self._fatal_exceptions,
                )
                duration_ms = round(max(0, self._monotonic_ns() - started_ns) / 1_000_000, 3)
            completed[definition.stage] = StageExecution(
                stage=definition.stage,
                required_dependencies=definition.required_dependencies,
                result=result,
                blocked_by=blockers,
                duration_ms=duration_ms,
            )

        executions = tuple(completed[stage] for stage in ORDERED_STAGES)
        terminal = PipelineRunResult(
            identity=identity,
            state=_terminal_state(executions),
            stages=executions,
        )
        self._lifecycle_store.save_terminal(terminal)
        return terminal

    @staticmethod
    def _validate_existing(
        existing: PipelineRunResult,
        identity: PipelineIdentity,
    ) -> None:
        if not isinstance(existing, PipelineRunResult):
            raise PipelineLifecycleError(
                "lifecycle store returned a non-PipelineRunResult succeeded snapshot"
            )
        if existing.identity != identity:
            raise PipelineLifecycleError(
                "lifecycle store returned a succeeded snapshot for a different process identity"
            )
        if existing.state is not PipelineState.SUCCEEDED:
            raise PipelineLifecycleError(
                "lifecycle store may return only a succeeded terminal snapshot"
            )


def run_daily_pipeline(
    process_date: datetime.date | str,
    *,
    stage_runners: Mapping[PipelineStage | str, StageRunner],
    lifecycle_store: PipelineLifecycleStore | None = None,
    fatal_exceptions: tuple[type[BaseException], ...] = (),
) -> PipelineRunResult:
    """Functional entry point for manual/API/worker wiring."""

    return DailyPipelineCoordinator(
        stage_runners=stage_runners,
        lifecycle_store=lifecycle_store,
        fatal_exceptions=fatal_exceptions,
    ).run(process_date)
