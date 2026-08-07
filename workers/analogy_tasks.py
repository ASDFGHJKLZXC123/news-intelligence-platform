"""Celery task for the Stage 5 historical-analogy rerank over one event.

Independently callable by ``event_id`` and routed to the pipeline queue. No Beat entry: neither
ADR 0004 nor the episode spec schedules an analogy run, and what decides an event is worth
reranking is that it has just been embedded -- not that a clock fired.

Nothing is built, connected, or configured at import. The session, the Redis client, and the
production orchestrator (live providers, prompt cache, RPM/TPM limiter) are all constructed inside
the task, so importing this module costs nothing and reaches nothing; unit tests replace
``SessionLocal`` and the two build hooks with fakes.

The task owns the transaction, and that is the point of ``commit_on_write=False``: the
orchestrator's ``llm_runs``/``jobs`` audit writes and the ``event_analogies`` reconciliation are
one unit of work. One commit, only after the whole result succeeded; one rollback on any failure,
which leaves the event's previously durable analogies exactly as they were rather than half
replaced. A rerun re-derives the same set, so the Stage1Task retry that follows a provider fault
is safe.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from celery import shared_task

from db.base import SessionLocal
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import get_settings
from packages.jobs import Stage1Job
from services.analogies.compatibility import normalize_tags
from services.analogies.contracts import DEFAULT_MIN_SIMILARITY, DEFAULT_TOP_K
from services.analogies.service import EventAnalogyResult, generate_event_analogies
from services.llm.runtime import build_production_orchestrator
from workers.celery_app import QUEUE_PIPELINE, Stage1Task

logger = get_logger("workers.analogy_tasks")

TASK_NAME = "workers.analogy_tasks.run_event_analogy_rerank"


def build_redis_client() -> Any:
    # Imported and constructed here, never at module import: `from_url` builds a lazy connection
    # pool, so no socket is opened until the orchestrator actually reads the prompt cache.
    import redis

    return redis.Redis.from_url(get_settings().redis_url)


def build_orchestrator(session: Any, redis_client: Any) -> Any:
    """The production runtime, sharing this task's session and never committing on its own."""

    return build_production_orchestrator(
        settings=get_settings(),
        session=session,
        redis_client=redis_client,
        commit_on_write=False,
    )


def _close_errors(*resources: Any) -> tuple[BaseException, ...]:
    """Close every supplied resource and return all faults without skipping later closes."""

    errors: list[BaseException] = []
    for resource in resources:
        if resource is None:
            continue
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as exc:
            errors.append(exc)
    return tuple(errors)


def _identity_regime_tags(values: Sequence[str] | None) -> list[str]:
    """Regime tags as retrieval will fold them: the ``normalize_tags`` vocabulary, sorted.

    Retrieval compares tags as a normalized set, so ``["post_QE", "pre_fiat"]`` and
    ``["Pre-Fiat", "post_qe", "post_QE"]`` are one question. Sorting is what makes the set
    representable as JSON without the hash order leaking into the key.
    """

    return sorted(normalize_tags(values))


def _identity_filter(values: Sequence[str] | None) -> list[str] | None:
    """A geography/industry filter as retrieval will validate it (``_validated_filter``).

    ``None`` survives as null: unfiltered is a different question from "only these", and
    flattening it to ``[]`` would collide the two. A supplied-but-blank filter canonicalizes to
    ``[]`` and stays distinct from null on purpose -- retrieval rejects it as an invalid request,
    and an invalid request is its own job, not a silent alias for the unfiltered one.
    """

    if values is None:
        return None
    return sorted({value.strip().lower() for value in values if value and value.strip()})


def _log_fields(result: EventAnalogyResult) -> dict[str, Any]:
    """The result as log extras. ``message`` is reserved on a LogRecord, so it is renamed here.

    Passing it through unrenamed raises ``KeyError: Attempt to overwrite 'message'`` -- but only
    once logging is actually configured, which is to say everywhere except an unconfigured test.
    """

    fields = result.as_dict()
    fields["analogy_message"] = fields.pop("message")
    return fields


@shared_task(name=TASK_NAME, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_event_analogy_rerank(
    event_id: str,
    regime_tags: Sequence[str] | None = None,
    geographies: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> dict[str, Any]:
    """Retrieve, rerank, and durably reconcile one event's historical analogies."""

    identifier = uuid.UUID(str(event_id))
    # This payload *is* the job's identity: `Stage1Job.create` hashes it into the job_key, so two
    # runs that differ only in a field missing from here are indistinguishable in the jobs table.
    # The geography and industry filters are hard filters on the retrieval -- a run restricted to
    # `geographies=["US"]` selects a different candidate set, and therefore a different durable
    # analogy set, than an unfiltered one. They belong in the identity.
    #
    # Every token is canonicalized *exactly* the way retrieval canonicalizes it, because identity
    # has to be as coarse as behaviour and no coarser. Retrieval folds these to sets -- so `["US",
    # "EU"]`, `["eu", "us"]`, and `["US", "us", "EU"]` all name one candidate set, one durable
    # analogy set, and therefore must name one job. Hashing them raw would mint a fresh job_key and
    # job_id per spelling, and the retry that follows a provider fault would no longer be
    # recognizable as a retry of the run it is retrying. Only the identity is canonicalized: the
    # service below is still handed the caller's own values, and stays the sole owner of validation.
    payload: dict[str, Any] = {
        "event_id": str(identifier),
        "regime_tags": _identity_regime_tags(regime_tags),
        "geographies": _identity_filter(geographies),
        "industries": _identity_filter(industries),
        "top_k": top_k,
        "min_similarity": min_similarity,
    }
    job = Stage1Job.create(TASK_NAME, payload).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    session = SessionLocal()
    redis_client: Any = None
    orchestrator: Any = None
    try:
        redis_client = build_redis_client()
        orchestrator = build_orchestrator(session, redis_client)
        result = generate_event_analogies(
            session,
            identifier,
            orchestrator=orchestrator,
            regime_tags=regime_tags,
            geographies=geographies,
            industries=industries,
            top_k=top_k,
            min_similarity=min_similarity,
        )
        precommit_cleanup_errors = _close_errors(orchestrator, redis_client)
        orchestrator = None
        redis_client = None
        if precommit_cleanup_errors:
            raise BaseExceptionGroup(
                "event analogy resource cleanup failed",
                list(precommit_cleanup_errors),
            )
        # The only commit: the LLM audit rows and the reconciled analogy set land together.
        session.commit()
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info("event analogy rerank completed", extra=_log_fields(result))
        payload = {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
            **result.as_dict(),
        }
    except BaseException as cause:
        # A retrieval, provider, or contract failure: the event keeps the analogies it already had,
        # and Stage1Task retries with backoff. A stale set is never served as a fresh one, and a
        # failed rerank is never reported as "no reliable analogy".
        cleanup_errors: list[BaseException] = []
        try:
            session.rollback()
        except BaseException as rollback_error:
            cleanup_errors.append(rollback_error)
        cleanup_errors.extend(_close_errors(orchestrator, redis_client, session))
        orchestrator = None
        redis_client = None
        set_job_id(None)
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("event analogy rerank failed", extra={"event_id": str(identifier)})
        if cleanup_errors:
            raise BaseExceptionGroup(
                "event analogy rerank and cleanup both failed",
                [cause, *cleanup_errors],
            ) from cause
        raise
    else:
        cleanup_errors = _close_errors(session)
        set_job_id(None)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "event analogy session cleanup failed",
                list(cleanup_errors),
            )
        return payload
