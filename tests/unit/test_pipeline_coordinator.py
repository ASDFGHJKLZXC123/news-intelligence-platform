"""Unit tests for the pure, manual-first daily pipeline coordinator."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from services.pipeline import (
    EXCLUDED_GATE_G_STAGES,
    ORDERED_STAGES,
    DailyPipelineCoordinator,
    ExcludedGateGStage,
    PipelineConfigurationError,
    PipelineIdentity,
    PipelineRunResult,
    PipelineStage,
    PipelineState,
    StageContext,
    StageItemFailure,
    StageResult,
    StageStatus,
    daily_pipeline_identity,
    pipeline_run_result_from_dict,
)

PROCESS_DATE = datetime.date(2026, 7, 29)


@dataclass
class RecordingStore:
    existing: PipelineRunResult | None = None
    begun: list[PipelineIdentity] = field(default_factory=list)
    saved: list[PipelineRunResult] = field(default_factory=list)

    def begin_or_get_succeeded(
        self,
        identity: PipelineIdentity,
    ) -> PipelineRunResult | None:
        self.begun.append(identity)
        return self.existing

    def save_terminal(self, result: PipelineRunResult) -> None:
        self.saved.append(result)


def _successful_runners(
    calls: list[PipelineStage] | None = None,
) -> dict[PipelineStage, Callable[[StageContext], StageResult]]:
    def runner(context: StageContext) -> StageResult:
        if calls is not None:
            calls.append(context.stage)
        return StageResult.succeeded(
            item_count=1,
            output={
                "stage": context.stage,
                "process_date": context.identity.process_date,
                "process_id": context.identity.process_id,
            },
        )

    return {stage: runner for stage in ORDERED_STAGES}


def test_process_date_identity_is_canonical_and_deterministic() -> None:
    from_date = daily_pipeline_identity(PROCESS_DATE)
    from_string = daily_pipeline_identity("2026-07-29")

    assert from_date == from_string
    assert from_date.process_key == "daily-pipeline:2026-07-29"
    assert from_date.process_id == daily_pipeline_identity(PROCESS_DATE).process_id
    assert from_date.process_id != daily_pipeline_identity("2026-07-30").process_id

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        daily_pipeline_identity("20260729")
    with pytest.raises(TypeError, match="calendar date"):
        daily_pipeline_identity(datetime.datetime(2026, 7, 29, 5, 30))


def test_all_stages_run_in_order_and_serialize_to_plain_json() -> None:
    calls: list[PipelineStage] = []
    store = RecordingStore()

    result = DailyPipelineCoordinator(
        stage_runners=_successful_runners(calls),
        lifecycle_store=store,
    ).run(PROCESS_DATE)

    assert calls == list(ORDERED_STAGES)
    assert result.state is PipelineState.SUCCEEDED
    assert [execution.stage for execution in result.stages] == list(ORDERED_STAGES)
    assert store.begun == [result.identity]
    assert store.saved == [result]

    serialized = result.to_dict()
    assert json.loads(json.dumps(serialized)) == serialized
    assert serialized["identity"]["process_date"] == "2026-07-29"
    assert serialized["stages"][0]["output"]["process_id"] == str(result.identity.process_id)
    assert serialized["stages"][0]["output"]["stage"] == "ingestion"
    assert type(serialized["stages"][0]["output"]["stage"]) is str
    assert all(execution["duration_ms"] >= 0.0 for execution in serialized["stages"])


def test_stage_durations_use_the_injected_monotonic_clock() -> None:
    timestamps = iter(
        value
        for index in range(len(ORDERED_STAGES))
        for value in (index * 10_000_000, index * 10_000_000 + (index + 1) * 1_000_000)
    )

    result = DailyPipelineCoordinator(
        stage_runners=_successful_runners(),
        monotonic_ns=lambda: next(timestamps),
    ).run(PROCESS_DATE)

    assert [execution.duration_ms for execution in result.stages] == [
        1.0,
        2.0,
        3.0,
        4.0,
        5.0,
        6.0,
        7.0,
    ]
    assert pipeline_run_result_from_dict(result.to_dict()) == result


def test_partial_fanout_output_is_usable_and_all_dependents_continue() -> None:
    calls: list[PipelineStage] = []
    runners = _successful_runners(calls)

    def partial_embeddings(context: StageContext) -> StageResult:
        calls.append(context.stage)
        return StageResult.partially_failed(
            succeeded_count=2,
            failures=(
                StageItemFailure(
                    item_key="article-3",
                    code="provider_timeout",
                    message="embedding provider timed out",
                    retryable=True,
                ),
            ),
            output={"embedded_article_ids": ["article-1", "article-2"]},
        )

    runners[PipelineStage.ARTICLE_EMBEDDINGS] = partial_embeddings
    result = DailyPipelineCoordinator(stage_runners=runners).run(PROCESS_DATE)

    assert calls == list(ORDERED_STAGES)
    assert result.state is PipelineState.PARTIALLY_FAILED
    embeddings = result.execution_for(PipelineStage.ARTICLE_EMBEDDINGS)
    assert embeddings.result.status is StageStatus.PARTIALLY_FAILED
    assert embeddings.result.usable is True
    assert embeddings.result.attempted_count == 3
    assert embeddings.result.succeeded_count == 2
    assert result.execution_for(PipelineStage.CLUSTERING).result.status is StageStatus.SUCCEEDED
    assert result.execution_for(PipelineStage.DAILY_BRIEF).result.status is StageStatus.SUCCEEDED


def test_failed_enrichment_branch_does_not_block_independent_branch_or_brief() -> None:
    calls: list[PipelineStage] = []
    runners = _successful_runners(calls)

    def failed_entity_linking(context: StageContext) -> StageResult:
        calls.append(context.stage)
        return StageResult.failed(
            StageItemFailure(
                item_key="__stage__",
                code="identity_index_unavailable",
                message="identity index unavailable",
                retryable=True,
            )
        )

    runners[PipelineStage.ENTITY_LINKING] = failed_entity_linking
    result = DailyPipelineCoordinator(stage_runners=runners).run(PROCESS_DATE)

    assert calls == list(ORDERED_STAGES)
    assert result.state is PipelineState.PARTIALLY_FAILED
    assert result.execution_for(PipelineStage.ENTITY_LINKING).result.status is StageStatus.FAILED
    assert (
        result.execution_for(PipelineStage.EVENT_EMBEDDINGS).result.status is StageStatus.SUCCEEDED
    )
    assert result.execution_for(PipelineStage.ANALOGIES).result.status is StageStatus.SUCCEEDED
    assert result.execution_for(PipelineStage.DAILY_BRIEF).result.status is StageStatus.SUCCEEDED


def test_hard_prerequisite_failure_skips_dependents_and_pipeline_fails() -> None:
    calls: list[PipelineStage] = []
    runners = _successful_runners(calls)

    def exploding_embeddings(context: StageContext) -> StageResult:
        calls.append(context.stage)
        raise RuntimeError("embedding service unavailable")

    runners[PipelineStage.ARTICLE_EMBEDDINGS] = exploding_embeddings
    result = DailyPipelineCoordinator(stage_runners=runners).run(PROCESS_DATE)

    assert calls == [PipelineStage.INGESTION, PipelineStage.ARTICLE_EMBEDDINGS]
    assert result.state is PipelineState.FAILED

    embeddings = result.execution_for(PipelineStage.ARTICLE_EMBEDDINGS)
    assert embeddings.result.status is StageStatus.FAILED
    assert embeddings.result.failures[0].code == "unhandled_stage_exception"
    assert embeddings.result.failures[0].error_type == "RuntimeError"

    clustering = result.execution_for(PipelineStage.CLUSTERING)
    assert clustering.result.status is StageStatus.SKIPPED
    assert clustering.blocked_by == (PipelineStage.ARTICLE_EMBEDDINGS,)
    assert result.execution_for(PipelineStage.ENTITY_LINKING).blocked_by == (
        PipelineStage.CLUSTERING,
    )
    assert result.execution_for(PipelineStage.EVENT_EMBEDDINGS).blocked_by == (
        PipelineStage.CLUSTERING,
    )
    assert result.execution_for(PipelineStage.ANALOGIES).blocked_by == (
        PipelineStage.EVENT_EMBEDDINGS,
    )
    assert result.execution_for(PipelineStage.DAILY_BRIEF).blocked_by == (PipelineStage.CLUSTERING,)


class _WorkerShutdown(Exception):
    """Stand-in for a runtime teardown signal such as Celery's ``SoftTimeLimitExceeded``."""


def test_configured_fatal_exception_propagates_instead_of_becoming_a_stage_failure() -> None:
    calls: list[PipelineStage] = []
    runners = _successful_runners(calls)
    store = RecordingStore()

    def shutting_down(context: StageContext) -> StageResult:
        calls.append(context.stage)
        raise _WorkerShutdown("soft time limit exceeded")

    runners[PipelineStage.CLUSTERING] = shutting_down
    coordinator = DailyPipelineCoordinator(
        stage_runners=runners,
        lifecycle_store=store,
        fatal_exceptions=(_WorkerShutdown,),
    )

    with pytest.raises(_WorkerShutdown, match="soft time limit"):
        coordinator.run(PROCESS_DATE)

    # No later stage was started and no terminal result was invented for a run that never ended.
    assert calls == [
        PipelineStage.INGESTION,
        PipelineStage.ARTICLE_EMBEDDINGS,
        PipelineStage.CLUSTERING,
    ]
    assert store.saved == []


def test_nested_fatal_exception_group_propagates_instead_of_becoming_a_stage_failure() -> None:
    calls: list[PipelineStage] = []
    runners = _successful_runners(calls)
    store = RecordingStore()
    grouped = ExceptionGroup(
        "task and cleanup both failed",
        [
            RuntimeError("rollback failed"),
            ExceptionGroup("task failure", [_WorkerShutdown("soft time limit exceeded")]),
        ],
    )

    def shutting_down(context: StageContext) -> StageResult:
        calls.append(context.stage)
        raise grouped

    runners[PipelineStage.CLUSTERING] = shutting_down
    coordinator = DailyPipelineCoordinator(
        stage_runners=runners,
        lifecycle_store=store,
        fatal_exceptions=(_WorkerShutdown,),
    )

    with pytest.raises(ExceptionGroup) as captured:
        coordinator.run(PROCESS_DATE)

    assert captured.value is grouped
    assert calls == [
        PipelineStage.INGESTION,
        PipelineStage.ARTICLE_EMBEDDINGS,
        PipelineStage.CLUSTERING,
    ]
    assert store.saved == []


def test_ordinary_stage_exceptions_still_become_failures_when_fatal_types_are_configured() -> None:
    runners = _successful_runners()

    def exploding_clustering(context: StageContext) -> StageResult:
        del context
        raise RuntimeError("clustering backend unavailable")

    runners[PipelineStage.CLUSTERING] = exploding_clustering
    result = DailyPipelineCoordinator(
        stage_runners=runners,
        fatal_exceptions=(_WorkerShutdown,),
    ).run(PROCESS_DATE)

    clustering = result.execution_for(PipelineStage.CLUSTERING)
    assert result.state is PipelineState.FAILED
    assert clustering.result.status is StageStatus.FAILED
    assert clustering.result.failures[0].code == "unhandled_stage_exception"
    assert clustering.result.failures[0].error_type == "RuntimeError"


def test_fatal_exceptions_must_be_exception_classes() -> None:
    with pytest.raises(PipelineConfigurationError, match="not an exception class"):
        DailyPipelineCoordinator(
            stage_runners=_successful_runners(),
            fatal_exceptions=("SoftTimeLimitExceeded",),  # type: ignore[arg-type]
        )


def test_a_bare_fatal_exception_class_is_a_configuration_error_not_a_type_error() -> None:
    # `fatal_exceptions=SomeError` instead of `(SomeError,)` is the easy mistake; it must fail
    # the same way every other misconfigured constructor argument does.
    with pytest.raises(PipelineConfigurationError, match="must be a tuple"):
        DailyPipelineCoordinator(
            stage_runners=_successful_runners(),
            fatal_exceptions=_WorkerShutdown,  # type: ignore[arg-type]
        )


def test_succeeded_date_is_an_idempotent_skip_with_no_stage_or_terminal_write() -> None:
    first_store = RecordingStore()
    succeeded = DailyPipelineCoordinator(
        stage_runners=_successful_runners(),
        lifecycle_store=first_store,
    ).run(PROCESS_DATE)
    assert succeeded.state is PipelineState.SUCCEEDED

    calls: list[PipelineStage] = []
    rerun_store = RecordingStore(existing=succeeded)
    replay = DailyPipelineCoordinator(
        stage_runners=_successful_runners(calls),
        lifecycle_store=rerun_store,
    ).run(PROCESS_DATE)

    assert calls == []
    assert replay is not succeeded
    assert replay.identity == succeeded.identity
    assert replay.stages == succeeded.stages
    assert replay.state is PipelineState.SUCCEEDED
    assert replay.idempotent_skip is True
    assert rerun_store.begun == [succeeded.identity]
    assert rerun_store.saved == []


def test_gate_g_stages_are_explicitly_excluded_and_cannot_be_configured() -> None:
    result = DailyPipelineCoordinator(stage_runners=_successful_runners()).run(PROCESS_DATE)

    assert result.excluded_gate_g_stages == (
        ExcludedGateGStage.CRISIS_PREDICTION,
        ExcludedGateGStage.PREDICTIVE_ROLLUPS,
        ExcludedGateGStage.COMPOSITE_ALERTS,
    )
    assert EXCLUDED_GATE_G_STAGES == result.excluded_gate_g_stages
    assert not (
        {stage.value for stage in ORDERED_STAGES}
        & {stage.value for stage in EXCLUDED_GATE_G_STAGES}
    )

    forbidden_runners: dict[PipelineStage | str, Callable[[StageContext], StageResult]] = {
        **_successful_runners(),
        "crisis_prediction": lambda context: StageResult.succeeded(),
    }
    with pytest.raises(PipelineConfigurationError, match="Gate G is closed"):
        DailyPipelineCoordinator(stage_runners=forbidden_runners)
