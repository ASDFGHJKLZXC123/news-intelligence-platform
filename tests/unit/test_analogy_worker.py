"""The analogy Celery task: registration, the transaction boundary, and cleanup.

What a fake can honestly prove is *ordering* -- that the commit happens only after the whole result
succeeded, that any failure rolls back instead, and that the session and the Redis client are
closed either way. What a rollback actually erases is proved against a real PostgreSQL in
``tests/integration/test_analogy_persistence_pgvector.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from packages.config import metrics
from services.analogies.persistence import AnalogyReconciliation
from services.analogies.service import AnalogyStatus, EventAnalogyResult
from workers import analogy_tasks
from workers.celery_app import QUEUE_PIPELINE, Stage1Task, celery_app

EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
RUN_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")


class GenerationFailed(RuntimeError):
    """A provider or contract failure, injected where the transaction boundary matters."""


class FakeSession:
    def __init__(self, close_error: BaseException | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.close_error = close_error

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeRedis:
    def __init__(self, close_error: BaseException | None = None) -> None:
        self.closed = False
        self.close_error = close_error

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeOrchestrator:
    def __init__(self, close_error: BaseException | None = None) -> None:
        self.close_count = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def _result(status: AnalogyStatus = AnalogyStatus.MATCHED) -> EventAnalogyResult:
    return EventAnalogyResult(
        event_id=EVENT_ID,
        status=status,
        message="1 episode(s) reranked by structural similarity",
        reconciliation=AnalogyReconciliation(inserted=1),
        llm_run_id=RUN_ID,
        trace_id="trace-1",
        tier="T2",
        considered_count=3,
        candidate_count=3,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generate: Any = None,
) -> tuple[FakeSession, FakeRedis, list[dict[str, Any]]]:
    session, redis_client = FakeSession(), FakeRedis()
    orchestrator = FakeOrchestrator()
    calls: list[dict[str, Any]] = []

    def _generate(session_arg: Any, event_id: uuid.UUID, **kwargs: Any) -> EventAnalogyResult:
        calls.append({"session": session_arg, "event_id": event_id, **kwargs})
        if generate is not None:
            return generate()
        return _result()

    monkeypatch.setattr(analogy_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(analogy_tasks, "build_redis_client", lambda: redis_client)
    monkeypatch.setattr(analogy_tasks, "build_orchestrator", lambda _s, _r: orchestrator)
    monkeypatch.setattr(analogy_tasks, "generate_event_analogies", _generate)
    metrics.reset()
    return session, redis_client, calls


# --- registration ----------------------------------------------------------------------
def test_the_task_is_registered_on_the_pipeline_queue_with_retry_defaults() -> None:
    task = celery_app.tasks[analogy_tasks.TASK_NAME]

    assert isinstance(task, Stage1Task)
    assert task.queue == QUEUE_PIPELINE
    assert task.max_retries == 3
    assert task.retry_backoff is True


def test_the_rerank_has_no_beat_entry() -> None:
    """A clock cannot know when an event has just been embedded."""
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    assert analogy_tasks.TASK_NAME not in scheduled


# --- the transaction boundary ----------------------------------------------------------
def test_a_successful_run_commits_once_and_closes_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, redis_client, calls = _install(monkeypatch)

    payload = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))

    assert (session.commits, session.rollbacks) == (1, 0)
    assert session.closed is True
    assert redis_client.closed is True
    assert calls[0]["orchestrator"].close_count == 1
    assert payload["status"] == "ok"
    assert payload["state"] == "succeeded"
    assert metrics.get(metrics.JOB_SUCCESSES) == 1
    assert metrics.get(metrics.JOB_FAILURES) == 0


def test_a_failed_run_rolls_back_and_never_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The event keeps the analogies it already had; a retry re-derives them from scratch."""

    def _raise() -> EventAnalogyResult:
        raise GenerationFailed("provider exhausted")

    session, redis_client, calls = _install(monkeypatch, generate=_raise)

    with pytest.raises(GenerationFailed):
        analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))

    assert (session.commits, session.rollbacks) == (0, 1)
    assert session.closed is True
    assert redis_client.closed is True
    assert calls[0]["orchestrator"].close_count == 1
    assert metrics.get(metrics.JOB_FAILURES) == 1


def test_cleanup_faults_do_not_hide_generation_failure_or_skip_session_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_error = GenerationFailed("provider exhausted")
    orchestrator_close_error = RuntimeError("orchestrator close failed")
    redis_close_error = RuntimeError("redis close failed")
    session_close_error = RuntimeError("session close failed")
    session = FakeSession(session_close_error)
    redis_client = FakeRedis(redis_close_error)
    orchestrator = FakeOrchestrator(orchestrator_close_error)

    monkeypatch.setattr(analogy_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(analogy_tasks, "build_redis_client", lambda: redis_client)
    monkeypatch.setattr(
        analogy_tasks,
        "build_orchestrator",
        lambda _session, _redis_client: orchestrator,
    )

    def fail_generation(*_args: Any, **_kwargs: Any) -> Any:
        raise generation_error

    monkeypatch.setattr(analogy_tasks, "generate_event_analogies", fail_generation)

    with pytest.raises(BaseExceptionGroup) as captured:
        analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))

    assert list(captured.value.exceptions) == [
        generation_error,
        orchestrator_close_error,
        redis_close_error,
        session_close_error,
    ]
    assert session.rollbacks == 1 and session.closed is True
    assert orchestrator.close_count == 1
    assert redis_client.closed is True


def test_the_orchestrator_shares_the_tasks_session_and_never_commits_on_its_own() -> None:
    """`commit_on_write=False`: the LLM audit rows and the analogy set are one unit of work."""
    captured: dict[str, Any] = {}

    def _build(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    from services.llm import runtime

    original = runtime.build_production_orchestrator
    try:
        analogy_tasks.build_production_orchestrator = _build  # type: ignore[assignment]
        analogy_tasks.build_orchestrator(FakeSession(), FakeRedis())
    finally:
        analogy_tasks.build_production_orchestrator = original  # type: ignore[assignment]

    assert captured["commit_on_write"] is False
    assert isinstance(captured["session"], FakeSession)
    assert isinstance(captured["redis_client"], FakeRedis)


# --- what the task passes through, and what it returns ---------------------------------
def test_the_filters_reach_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _redis, calls = _install(monkeypatch)

    analogy_tasks.run_event_analogy_rerank(
        str(EVENT_ID),
        regime_tags=["post_QE"],
        geographies=["United States"],
        industries=["banking"],
        top_k=3,
        min_similarity=0.7,
    )

    call = calls[0]
    assert call["session"] is session
    assert call["event_id"] == EVENT_ID
    assert call["regime_tags"] == ["post_QE"]
    assert call["geographies"] == ["United States"]
    assert call["industries"] == ["banking"]
    assert (call["top_k"], call["min_similarity"]) == (3, 0.7)


def test_the_task_result_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _install(monkeypatch)

    payload = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))

    assert json.loads(json.dumps(payload)) == payload
    assert payload["event_id"] == str(EVENT_ID)
    assert payload["llm_run_id"] == str(RUN_ID)
    assert payload["tier"] == "T2"


def test_a_no_match_is_a_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _redis, _calls = _install(
        monkeypatch, generate=lambda: _result(AnalogyStatus.NO_RELIABLE_ANALOGY)
    )

    payload = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))

    # It committed: the no-match cleared any stale rows, and that removal must persist.
    assert session.commits == 1
    # The job succeeded *and* the answer was "no analogy". Two different questions, two keys.
    assert payload["status"] == "ok"
    assert payload["analogy_status"] == "no_reliable_analogy"
    assert payload["state"] == "succeeded"
    assert payload["match_count"] == 0


def test_the_result_is_safe_to_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`message` is reserved on a LogRecord, and a raw ``as_dict()`` as extras raises a KeyError.

    Only when logging is actually enabled, which is why this test enables it: an unconfigured
    logger never builds the record, so the failure would otherwise hide until production.
    """
    _install(monkeypatch)

    with caplog.at_level(logging.INFO, logger="workers.analogy_tasks"):
        payload = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))

    assert payload["status"] == "ok"
    record = next(
        r for r in caplog.records if r.getMessage() == "event analogy rerank completed"
    )
    assert record.analogy_message == "1 episode(s) reranked by structural similarity"
    assert record.analogy_status == "matched"


def test_a_malformed_event_id_fails_before_any_session_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _redis, _calls = _install(monkeypatch)

    with pytest.raises(ValueError, match="badly formed"):
        analogy_tasks.run_event_analogy_rerank("not-a-uuid")

    assert (session.commits, session.rollbacks, session.closed) == (0, 0, False)


def test_the_job_identity_distinguishes_the_optional_geography_and_industry_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs that retrieve different candidate sets must not share one job identity.

    `Stage1Job.create` hashes the task payload into the job_key, so a filter missing from that
    payload is a filter the jobs table cannot see: a US-only rerun and an unfiltered one would
    collide on the same key and the same derived job id, and the second would look like a duplicate
    of the first. Geography and industry are *hard filters* on retrieval -- they select a different
    candidate set and therefore a different durable analogy set. They are part of the question being
    asked, not decoration on it.
    """
    _install(monkeypatch)

    unfiltered = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))
    us_only = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), geographies=["US"])
    banking = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), industries=["banking"])

    keys = {unfiltered["job_key"], us_only["job_key"], banking["job_key"]}
    assert len(keys) == 3, "an omitted filter would collapse these into one job identity"


def test_the_same_question_still_produces_the_same_job_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency survives the fix: the same filters keep one stable key, in either argument order."""
    _install(monkeypatch)

    first = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), geographies=["US", "EU"])
    second = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), geographies=["EU", "US"])

    assert first["job_key"] == second["job_key"]


def test_an_unfiltered_run_is_not_the_same_question_as_an_empty_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` means "unfiltered" and an explicit list means "only these". Different questions."""
    _install(monkeypatch)

    unfiltered = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))
    filtered = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), geographies=["US"])

    assert unfiltered["job_key"] != filtered["job_key"]


# --- identity is canonical: as coarse as retrieval, and no coarser -----------------------
def _identity(payload: dict[str, Any]) -> tuple[str, str]:
    """The whole derived identity, not just the key: job_id is a uuid5 *of* the key."""
    return payload["job_key"], payload["job_id"]


@pytest.mark.parametrize(
    ("filter_name", "canonical", "restated"),
    [
        # Order, case, and duplicates are spelling, not meaning: retrieval folds regime tags with
        # `normalize_tags` and the two hard filters with `_validated_filter`, so each trio below
        # selects one candidate set -- and must therefore select one job.
        ("regime_tags", ["post_QE", "pre_fiat"], ["Pre-Fiat", "post_qe", "post_QE"]),
        ("geographies", ["US", "EU"], [" eu ", "us", "US"]),
        ("industries", ["banking", "energy"], ["Energy", "BANKING", "banking"]),
    ],
)
def test_order_case_and_duplicates_do_not_change_the_job_identity(
    monkeypatch: pytest.MonkeyPatch,
    filter_name: str,
    canonical: list[str],
    restated: list[str],
) -> None:
    """The same semantic question keeps one job_key and one job_id, however it is spelled.

    A fresh identity per spelling would mean the retry that follows a provider fault is not
    recognizable as a retry of the run it retries -- the jobs table would read two runs where one
    question was asked once.
    """
    _install(monkeypatch)

    first = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), **{filter_name: canonical})
    second = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), **{filter_name: restated})

    assert _identity(first) == _identity(second)


def test_a_filter_that_changes_retrieval_still_changes_the_job_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonicalizing must not over-merge: different candidate sets stay different jobs."""
    _install(monkeypatch)

    identities = {
        _identity(analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), **kwargs))
        for kwargs in (
            {"regime_tags": ["post_QE"]},
            {"regime_tags": ["pre_fiat"]},
            {"geographies": ["US"]},
            {"geographies": ["US", "EU"]},
            {"industries": ["banking"]},
        )
    }

    assert len(identities) == 5


@pytest.mark.parametrize("filter_name", ["geographies", "industries"])
def test_a_blank_only_filter_canonicalizes_to_empty_but_stays_distinct_from_unfiltered(
    monkeypatch: pytest.MonkeyPatch, filter_name: str
) -> None:
    """Supplied-but-empty is an invalid request downstream, not a synonym for unfiltered.

    `_validated_filter` raises on it rather than treating it as "match everything", so it is its
    own question -- and its own job -- and must not collide with the `None` run that retrieval
    would happily answer.
    """
    _install(monkeypatch)

    unfiltered = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID))
    empty = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), **{filter_name: []})
    blank_only = analogy_tasks.run_event_analogy_rerank(str(EVENT_ID), **{filter_name: ["  ", ""]})

    assert _identity(empty) == _identity(blank_only)
    assert _identity(empty) != _identity(unfiltered)


def test_canonicalizing_the_identity_does_not_rewrite_what_the_service_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the job's identity is folded. The service still receives the caller's own values.

    Retrieval owns normalization *and* validation -- it is what raises on an empty filter and on an
    unknown tag. Handing it pre-folded values would hide the caller's actual request from the layer
    whose job it is to judge it.
    """
    _session, _redis, calls = _install(monkeypatch)

    analogy_tasks.run_event_analogy_rerank(
        str(EVENT_ID),
        regime_tags=["Pre-Fiat", "post_QE", "post_QE"],
        geographies=[" us ", "US"],
        industries=["Banking"],
    )

    call = calls[0]
    assert call["regime_tags"] == ["Pre-Fiat", "post_QE", "post_QE"]
    assert call["geographies"] == [" us ", "US"]
    assert call["industries"] == ["Banking"]
