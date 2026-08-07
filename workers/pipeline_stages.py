"""Production adapters from the pure daily coordinator to existing Celery task bodies.

The outer coordinator itself is the one broker-delivered task.  Inside that worker process these
adapters invoke the already accepted task bodies synchronously, in dependency order.  Each body
keeps its existing transaction boundary and retry-safe/idempotent writer; the adapter only turns
its JSON result or exception into the coordinator's structured :class:`StageResult`.

There is intentionally no crisis-model import and no predictive stage.  Event ids come from the
clustering result, which confines per-event entity/analogy fan-out to events created by this
attempt.  A partial fan-out continues and is surfaced as ``partially_failed`` rather than being
laundered into success.

The one exception to "every task exception becomes pipeline state" is ``fatal_exceptions``.
Practically all of a real run's wall time is spent *inside* ``task.run`` -- HTTP fetches, LLM
calls, database writes -- so a runtime teardown signal such as Celery's ``SoftTimeLimitExceeded``
lands here, not in the coordinator between two stages.  Recording it as an item failure would let
a fan-out keep starting new per-item work through the graceful window until the hard kill, leaving
the date RUNNING until its lease expires.  These adapters therefore re-raise the caller's declared
fatal types verbatim, which aborts the fan-out at that item and hands the signal to the
coordinator's identical guard.  The default is an empty tuple, which matches nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy import select

from db.base import SessionLocal
from db.models import Source
from services.pipeline import (
    PipelineIdentity,
    PipelineStage,
    StageContext,
    StageItemFailure,
    StageResult,
    StageRunner,
    contains_fatal_exception,
    normalize_fatal_exceptions,
)
from workers.analogy_tasks import run_event_analogy_rerank
from workers.clustering_tasks import run_article_clustering
from workers.embedding_tasks import run_article_embedding, run_event_embedding
from workers.entity_linking_tasks import run_event_entity_linking
from workers.ingestion_tasks import ingest_source_feed
from workers.report_tasks import run_daily_brief_generation


class RunnableTask(Protocol):
    """The part of a Celery task used for in-process orchestration."""

    def run(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]: ...


SessionFactory = Callable[[], Any]


class EventScopeStore(Protocol):
    """Durable event ids accumulated for one logical pipeline date."""

    @property
    def active_run_token(self) -> Any:
        """The RUNNING-attempt lease token guarding in-transaction scope writes."""
        ...

    def add_event_ids(
        self,
        identity: PipelineIdentity,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...

    def event_ids(self, identity: PipelineIdentity) -> tuple[str, ...]: ...


def _failure(item_key: str, code: str, cause: BaseException) -> StageItemFailure:
    message = str(cause).strip() or f"{type(cause).__name__} without an error message"
    return StageItemFailure(
        item_key=item_key,
        code=code,
        message=message,
        retryable=bool(getattr(cause, "retryable", True)),
        error_type=type(cause).__name__,
    )


def _run_one(
    task: RunnableTask,
    item_key: str,
    fatal_exceptions: tuple[type[BaseException], ...],
    *args: Any,
    **kwargs: Any,
) -> tuple[dict[str, Any] | None, StageItemFailure | None]:
    """Run one task body, returning either its payload or this item's failure.

    ``fatal_exceptions`` is positional rather than keyword-only because ``**kwargs`` is forwarded
    verbatim to ``task.run``; a keyword here would collide with a task argument of the same name.
    """

    try:
        payload = task.run(*args, **kwargs)
    except fatal_exceptions:
        # Runtime teardown, not an item outcome (see the module docstring). Re-raising is what
        # stops a fan-out at this item instead of spending the graceful window on the next one.
        raise
    except Exception as exc:  # noqa: BLE001 - converted into explicit per-item pipeline state
        if contains_fatal_exception(exc, fatal_exceptions):
            raise
        return None, _failure(item_key, "task_failed", exc)
    if not isinstance(payload, Mapping):
        return None, StageItemFailure(
            item_key=item_key,
            code="invalid_task_result",
            message=f"task returned {type(payload).__name__}, expected a mapping",
            retryable=False,
            error_type="TypeError",
        )
    return dict(payload), None


def _fanout(
    task: RunnableTask,
    item_keys: Sequence[str],
    fatal_exceptions: tuple[type[BaseException], ...],
) -> StageResult:
    successes: list[dict[str, Any]] = []
    failures: list[StageItemFailure] = []
    # A fatal type re-raised by `_run_one` propagates straight out of this loop: the remaining
    # items are deliberately never attempted, and no partial StageResult is invented for a run
    # that is being torn down mid-flight.
    for item_key in item_keys:
        payload, failure = _run_one(task, item_key, fatal_exceptions, item_key)
        if failure is not None:
            failures.append(failure)
        else:
            assert payload is not None
            successes.append(payload)
    output = {
        "item_keys": list(item_keys),
        "results": successes,
    }
    if failures and successes:
        return StageResult.partially_failed(
            succeeded_count=len(successes),
            failures=tuple(failures),
            output=output,
        )
    if failures:
        return StageResult.failed(*failures, output=output)
    return StageResult.succeeded(item_count=len(successes), output=output)


def _event_ids(
    context: StageContext,
    event_scope_store: EventScopeStore | None,
) -> tuple[str, ...]:
    clustering = context.execution_for(PipelineStage.CLUSTERING)
    raw = [] if clustering is None else clustering.result.output.get("event_ids", [])
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise TypeError("clustering output event_ids must be a list of strings")
    durable = () if event_scope_store is None else event_scope_store.event_ids(context.identity)
    return tuple(sorted({*raw, *durable}))


class ProductionPipelineStages:
    """Build the seven injected runners from existing, independently tested task bodies."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        ingestion_task: RunnableTask = ingest_source_feed,
        article_embedding_task: RunnableTask = run_article_embedding,
        clustering_task: RunnableTask = run_article_clustering,
        entity_linking_task: RunnableTask = run_event_entity_linking,
        event_embedding_task: RunnableTask = run_event_embedding,
        analogy_task: RunnableTask = run_event_analogy_rerank,
        daily_brief_task: RunnableTask = run_daily_brief_generation,
        event_scope_store: EventScopeStore | None = None,
        fatal_exceptions: tuple[type[BaseException], ...] = (),
    ) -> None:
        """``fatal_exceptions`` are re-raised out of a task body instead of becoming item failures.

        Declared by the caller and validated with the coordinator's own
        :func:`~services.pipeline.normalize_fatal_exceptions`, so one wiring site names the types
        once for both layers of a run.  Defaulting to an empty tuple keeps every other caller --
        including tests that build these adapters directly -- on the pre-existing behaviour where
        every task exception is per-item pipeline state.
        """

        self._session_factory = session_factory
        self._ingestion_task = ingestion_task
        self._article_embedding_task = article_embedding_task
        self._clustering_task = clustering_task
        self._entity_linking_task = entity_linking_task
        self._event_embedding_task = event_embedding_task
        self._analogy_task = analogy_task
        self._daily_brief_task = daily_brief_task
        self._event_scope_store = event_scope_store
        self._fatal_exceptions = normalize_fatal_exceptions(fatal_exceptions)

    def runners(self) -> dict[PipelineStage, StageRunner]:
        return {
            PipelineStage.INGESTION: self.ingestion,
            PipelineStage.ARTICLE_EMBEDDINGS: self.article_embeddings,
            PipelineStage.CLUSTERING: self.clustering,
            PipelineStage.ENTITY_LINKING: self.entity_linking,
            PipelineStage.EVENT_EMBEDDINGS: self.event_embeddings,
            PipelineStage.ANALOGIES: self.analogies,
            PipelineStage.DAILY_BRIEF: self.daily_brief,
        }

    def ingestion(self, _context: StageContext) -> StageResult:
        session = self._session_factory()
        try:
            source_ids = [
                str(value)
                for value in session.scalars(
                    select(Source.id).where(Source.active.is_(True)).order_by(Source.id)
                ).all()
            ]
        finally:
            session.close()
        return _fanout(self._ingestion_task, source_ids, self._fatal_exceptions)

    def article_embeddings(self, _context: StageContext) -> StageResult:
        payload, failure = _run_one(
            self._article_embedding_task,
            "__stage__",
            self._fatal_exceptions,
        )
        if failure is not None:
            return StageResult.failed(failure)
        assert payload is not None
        return StageResult.succeeded(item_count=1, output=payload)

    def clustering(self, context: StageContext) -> StageResult:
        kwargs: dict[str, Any] = {"pipeline_job_id": str(context.identity.process_id)}
        if self._event_scope_store is not None:
            token = self._event_scope_store.active_run_token
            kwargs["pipeline_run_token"] = None if token is None else str(token)
        payload, failure = _run_one(
            self._clustering_task,
            "__stage__",
            self._fatal_exceptions,
            **kwargs,
        )
        if failure is not None:
            return StageResult.failed(failure)
        assert payload is not None
        raw_event_ids = payload.get("event_ids", [])
        if not isinstance(raw_event_ids, list) or not all(
            isinstance(value, str) for value in raw_event_ids
        ):
            return StageResult.failed(
                StageItemFailure(
                    item_key="__stage__",
                    code="invalid_task_result",
                    message="clustering task event_ids must be a list of strings",
                    retryable=False,
                    error_type="TypeError",
                )
            )
        if self._event_scope_store is not None:
            durable_ids = self._event_scope_store.add_event_ids(
                context.identity,
                tuple(raw_event_ids),
            )
            payload["event_ids"] = list(durable_ids)
        return StageResult.succeeded(item_count=1, output=payload)

    def entity_linking(self, context: StageContext) -> StageResult:
        return _fanout(
            self._entity_linking_task,
            _event_ids(context, self._event_scope_store),
            self._fatal_exceptions,
        )

    def event_embeddings(self, _context: StageContext) -> StageResult:
        payload, failure = _run_one(
            self._event_embedding_task,
            "__stage__",
            self._fatal_exceptions,
        )
        if failure is not None:
            return StageResult.failed(failure)
        assert payload is not None
        return StageResult.succeeded(item_count=1, output=payload)

    def analogies(self, context: StageContext) -> StageResult:
        return _fanout(
            self._analogy_task,
            _event_ids(context, self._event_scope_store),
            self._fatal_exceptions,
        )

    def daily_brief(self, context: StageContext) -> StageResult:
        date = context.identity.process_date.isoformat()
        payload, failure = _run_one(self._daily_brief_task, date, self._fatal_exceptions, date)
        if failure is not None:
            return StageResult.failed(failure)
        assert payload is not None
        if payload.get("status") != "published":
            return StageResult.failed(
                StageItemFailure(
                    item_key=date,
                    code="daily_brief_not_published",
                    message=f"daily brief completed with status {payload.get('status')!r}",
                    retryable=False,
                ),
                output=payload,
            )
        return StageResult.succeeded(item_count=1, output=payload)


__all__ = ["EventScopeStore", "ProductionPipelineStages", "RunnableTask"]
