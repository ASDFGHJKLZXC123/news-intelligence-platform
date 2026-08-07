"""End-to-end daily-brief generation, with durable failure handling (ADR 0009, spec).

The one coordinator that runs Stage 6 for a single ``brief_date``: it derives the ADR 0009 window,
durably reserves the brief's version, composes and grounds it through the accepted deterministic and
model stages, and either publishes the result or records a durable ``failed`` marker -- and it owns
the session boundaries the rest of Stage 6 deliberately does not, because durability is its whole
purpose.

Two transactions, on purpose:

* **The durable start commits before any model work.** The next global daily-brief version is
  allocated ``generating`` under the lifecycle's advisory lock and committed on its own, so the
  report is externally observable the instant generation begins and no later failure can erase the
  row (ADR 0009: reruns add versions, never mutate; the brief always ships or is visibly ``failed``).
* **The main transaction commits exactly once, at the end.** Selection, bounded context, material,
  composition, the grounding gate, section persistence, the ``generated_by_run_id`` attachment, and
  the publish-or-fail transition all share one unit of work with the orchestrator's LLM audit rows
  (the injected orchestrator is built ``commit_on_write=False`` for this session). On success or a
  blocked gate they commit together; on an unexpected fault they roll back together, leaving no
  partial sections and no orphan audit rows.

A blocked grounding/copyright/consistency gate is an *expected* completed generation: its
audit-safe sections are retained and the report is durably ``failed``, and the coordinator returns a
frozen result that says so -- it does not raise, because nothing needs retrying. Every *unexpected*
fault after the durable start is routed through one typed exit, so once the row is durably
``generating`` a raw connection/rollback/close error can never escape: a pipeline exception, but also
a failed main-session acquisition, a failed rollback or close on any path, is caught: the main
transaction is rolled back and closed (both attempted, neither allowed to escape raw), then in a
clean transaction the original report is reloaded and legally moved to ``failed`` -- a report already
terminal is never rewritten -- before a :class:`DailyBriefGenerationError` is raised carrying the
original cause and, deterministically aggregated, every cleanup fault that occurred alongside it.

The pure coordinator imports no engine and no ``Settings``: it depends on injected ``session_factory``
and ``orchestrator_factory(session)`` seams (with pure-repository defaults for everything else), so a
disposable test can never bind it to the default ``news`` database by accident. Production wiring is
the explicit :func:`build_generation_orchestrator_factory` hook.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

from sqlalchemy.orm import Session

from db.models.core import (
    REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    REPORT_CONTENT_POLICY_PREDICTION_BACKED,
)
from services.reports.composition import (
    compose_brief,
    descriptive_only_context,
    descriptive_only_inputs,
)
from services.reports.context import BriefContext, build_brief_context
from services.reports.context_repository import SQLAlchemyBriefContextRepository
from services.reports.contracts import BriefInputs
from services.reports.grounding import GateOutcome, run_grounding_gate
from services.reports.lifecycle import (
    TERMINAL_STATUSES,
    ReportLifecycleRepository,
    ReportSnapshot,
    ReportStatus,
    default_daily_brief_title,
    generated_by_run_id_for,
)
from services.reports.material import build_brief_material
from services.reports.repository import SQLAlchemyBriefInputRepository
from services.reports.selection import build_brief_inputs
from services.reports.window import BriefWindow, window_for_date

#: A nonblank, deterministic reason for every scheduled run/rerun. The lifecycle drops it on version
#: 1 (there is no prior to change from) and requires a nonblank one on every rerun, so passing it
#: unconditionally is what lets a scheduled retry allocate ``version + 1`` without the caller having
#: to know whether a prior version exists.
DEFAULT_CHANGE_REASON = "scheduled daily brief generation"

# The injectable seams. ``session_factory`` opens one caller-owned session; ``orchestrator_factory``
# builds the LLM orchestrator *for that session* so its audit rows share the session's transaction.
SessionFactory = Callable[[], Session]
OrchestratorFactory = Callable[[Session], object]
InputsBuilder = Callable[[Session, BriefWindow], BriefInputs]
ContextBuilder = Callable[[Session, BriefInputs], BriefContext]
LifecycleRepositoryFactory = Callable[[Session], ReportLifecycleRepository]


class DailyBriefGenerationError(RuntimeError):
    """An unexpected fault after the durable start; carries what failed and whether it was marked.

    Raised only for the unexpected path (never for a blocked gate, which is an expected outcome). It
    names the durably-created report and its ``brief_date``, preserves the original cause's type and
    text, and reports whether the durable ``failed`` marker was written. If the failure-marking clean
    transaction *also* failed, its cause is preserved too and the error still raises -- fail loud, and
    with both causes visible, rather than swallowing either.
    """

    def __init__(
        self,
        *,
        report_id: uuid.UUID,
        brief_date: datetime.date,
        cause: BaseException,
        durable_failure_marked: bool,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.report_id = report_id
        self.brief_date = brief_date
        self.cause = cause
        self.original_cause_type = type(cause).__name__
        self.original_cause_text = str(cause)
        self.durable_failure_marked = durable_failure_marked
        self.cleanup_error = cleanup_error
        message = (
            f"daily brief generation for {brief_date.isoformat()} (report {report_id}) failed: "
            f"{self.original_cause_type}: {self.original_cause_text}; "
            f"durable failure marked: {durable_failure_marked}"
        )
        if cleanup_error is not None:
            message += (
                f"; failure marking ALSO failed: {type(cleanup_error).__name__}: {cleanup_error}"
            )
        super().__init__(message)


@dataclass(frozen=True)
class DailyBriefGenerationResult:
    """One generation's outcome as frozen primitives -- no ORM row, no session, safe to return.

    ``report`` is the final detached :class:`ReportSnapshot`; the remaining fields are the useful
    facts a caller (or a log line) needs without re-reading the database.
    """

    report: ReportSnapshot
    brief_date: datetime.date
    window: BriefWindow
    selected_event_ids: tuple[uuid.UUID, ...]
    selected_event_count: int
    quiet_day: bool
    gate_outcome: GateOutcome
    published: bool
    section_count: int
    version: int
    change_reason: str | None
    generated_by_run_id: uuid.UUID | None


def _production_inputs_builder(
    session: Session,
    window: BriefWindow,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> BriefInputs:
    return build_brief_inputs(
        SQLAlchemyBriefInputRepository(
            session,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        ),
        window,
    )


def _production_context_builder(
    session: Session,
    inputs: BriefInputs,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> BriefContext:
    return build_brief_context(
        SQLAlchemyBriefContextRepository(
            session,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        ),
        inputs.top_events,
        prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
    )


def build_generation_orchestrator_factory(
    *, settings: object, redis_client: object
) -> OrchestratorFactory:
    """The production ``orchestrator_factory``: an orchestrator bound to one session, flush-only.

    The factory contract the coordinator relies on (clause 10): the orchestrator's LLM audit rows
    must share the coordinator's main transaction, so it is built ``commit_on_write=False`` -- the
    rows flush with the report and commit or roll back with it, never on their own. ``build_``
    ``production_orchestrator`` is imported lazily so the pure coordinator module never pulls in the
    global engine wiring.
    """

    def factory(session: Session) -> object:
        from services.llm.runtime import build_production_orchestrator

        return build_production_orchestrator(
            settings=settings,  # type: ignore[arg-type]
            session=session,
            redis_client=redis_client,
            commit_on_write=False,
        )

    return factory


def generate_daily_brief(
    brief_date: datetime.date,
    *,
    session_factory: SessionFactory,
    orchestrator_factory: OrchestratorFactory,
    change_reason: str | None = None,
    title: str | None = None,
    inputs_builder: InputsBuilder | None = None,
    context_builder: ContextBuilder | None = None,
    lifecycle_repository_factory: LifecycleRepositoryFactory = ReportLifecycleRepository,
    prediction_backed_outputs_enabled: bool = False,
) -> DailyBriefGenerationResult:
    """Generate, ground, and durably publish-or-fail one daily brief for ``brief_date``.

    ``session_factory`` and ``orchestrator_factory`` are the required seams (production supplies the
    real session maker and :func:`build_generation_orchestrator_factory`); everything else defaults to
    the accepted pure builders and the real lifecycle repository. Returns a frozen
    :class:`DailyBriefGenerationResult`; raises :class:`DailyBriefGenerationError` only on an
    unexpected fault after the durable start.
    """

    window = window_for_date(brief_date)
    reason = change_reason if (change_reason and change_reason.strip()) else DEFAULT_CHANGE_REASON
    resolved_inputs_builder = inputs_builder or (
        lambda session, brief_window: _production_inputs_builder(
            session,
            brief_window,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
    )
    resolved_context_builder = context_builder or (
        lambda session, inputs: _production_context_builder(
            session,
            inputs,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
    )

    # 1. Durable start: allocate the next version `generating` and COMMIT it on its own, so the
    #    report is externally observable and no later failure can erase the row. A close failure
    #    after that commit is itself a post-durable fault, routed through the typed failure path
    #    (which marks this same row `failed`) rather than allowed to escape raw.
    report, start_close_error = _durable_start(
        brief_date,
        title=title or default_daily_brief_title(brief_date),
        change_reason=reason,
        session_factory=session_factory,
        lifecycle_repository_factory=lifecycle_repository_factory,
        content_policy=(
            REPORT_CONTENT_POLICY_PREDICTION_BACKED
            if prediction_backed_outputs_enabled
            else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        ),
    )
    if start_close_error is not None:
        _mark_failed_or_raise(
            report=report,
            cause=start_close_error,
            cleanup_errors=(),
            session_factory=session_factory,
            lifecycle_repository_factory=lifecycle_repository_factory,
        )

    # 2. Main transaction: everything else, one commit at the end. The session acquisition itself
    #    can fail after the durable start; that too must end in a durable `failed` marker for the
    #    same version, never a raw error, so it is caught and routed through the same clean path.
    try:
        session = session_factory()
    except Exception as cause:  # noqa: BLE001 -- routed to the typed failure path, never raw
        _mark_failed_or_raise(
            report=report,
            cause=cause,
            cleanup_errors=(),
            session_factory=session_factory,
            lifecycle_repository_factory=lifecycle_repository_factory,
        )

    try:
        result = _run_main(
            report=report,
            window=window,
            session=session,
            orchestrator_factory=orchestrator_factory,
            inputs_builder=resolved_inputs_builder,
            context_builder=resolved_context_builder,
            lifecycle_repository_factory=lifecycle_repository_factory,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
    except Exception as cause:  # noqa: BLE001 -- re-raised, typed, after a durable failure marker
        # Abandon the main transaction: roll back and close, both attempted, neither escaping raw.
        # Any cleanup fault is carried into the typed error alongside the original pipeline cause.
        _mark_failed_or_raise(
            report=report,
            cause=cause,
            cleanup_errors=_rollback_and_close(session),
            session_factory=session_factory,
            lifecycle_repository_factory=lifecycle_repository_factory,
        )

    # Success or a blocked gate: the one main commit already happened inside `_run_main`. Closing the
    # now-terminal session must still not escape raw -- a close failure here is a post-durable fault
    # routed through the typed path, which reloads the terminal row and never rewrites it.
    close_error = _safe_close(session)
    if close_error is not None:
        _mark_failed_or_raise(
            report=report,
            cause=close_error,
            cleanup_errors=(),
            session_factory=session_factory,
            lifecycle_repository_factory=lifecycle_repository_factory,
        )
    return result


def _durable_start(
    brief_date: datetime.date,
    *,
    title: str,
    change_reason: str,
    session_factory: SessionFactory,
    lifecycle_repository_factory: LifecycleRepositoryFactory,
    content_policy: str,
) -> tuple[ReportSnapshot, BaseException | None]:
    """Allocate the `generating` version, commit it alone, and return the durable snapshot.

    A *pre-commit* fault means nothing is durable yet: the session is rolled back and closed (both
    attempted) and the original error escapes raw -- the coordinator's typed contract only covers
    faults *after* the durable start. Once the commit succeeds the row is durably ``generating``; the
    post-commit ``close`` is still attempted, and if it raises that close error is returned as the
    second tuple element so the coordinator routes it -- a post-durable fault -- through the typed
    failure path instead of letting it escape raw. A clean run returns ``(report, None)``.
    """

    session = session_factory()
    try:
        repo = lifecycle_repository_factory(session)
        report = repo.create_generating_daily_brief(
            brief_date=brief_date,
            title=title,
            change_reason=change_reason,
            generated_by_run_id=None,  # not known until the gate; attached later, pre-publish
            content_policy=content_policy,
        )
        session.commit()
    except Exception:
        _rollback_and_close(session)  # attempted; faults dropped -- the original error escapes raw
        raise
    return report, _safe_close(session)


def _run_main(
    *,
    report: ReportSnapshot,
    window: BriefWindow,
    session: Session,
    orchestrator_factory: OrchestratorFactory,
    inputs_builder: InputsBuilder,
    context_builder: ContextBuilder,
    lifecycle_repository_factory: LifecycleRepositoryFactory,
    prediction_backed_outputs_enabled: bool,
) -> DailyBriefGenerationResult:
    """The composed-and-grounded body, committed exactly once. Never marks anything itself."""

    orchestrator = orchestrator_factory(session)  # built for THIS session: audit rows share it
    try:
        inputs = inputs_builder(session, window)
        context = context_builder(session, inputs)
        if not prediction_backed_outputs_enabled:
            # The durable report marker promises the *whole* composition path was descriptive.
            # Enforce that promise after injected/custom builders too; a caller cannot smuggle
            # alert/prior-report prose or forecast probabilities into a descriptive_only.v1 row.
            inputs = descriptive_only_inputs(inputs)
            context = descriptive_only_context(context)
        material = build_brief_material(
            inputs,
            context,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
        draft = compose_brief(
            orchestrator,
            inputs=inputs,
            context=context,
            material=material,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )

        repo = lifecycle_repository_factory(session)
        # Advance to the gate after composition and before grounding, per the lifecycle order.
        repo.transition(report.id, ReportStatus.GROUNDING_CHECK)
        gate = run_grounding_gate(orchestrator, inputs=inputs, context=context, draft=draft)
    except BaseException as cause:
        close = getattr(orchestrator, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "daily brief model work and orchestrator cleanup both failed",
                    [cause, cleanup_error],
                ) from cause
        raise
    else:
        close = getattr(orchestrator, "close", None)
        if callable(close):
            close()

    sections = repo.persist_sections(report.id, gate)

    # Attach the provenance while still pre-publish, deterministically from the gate's run ids. A
    # quiet brief made no run (`None`), so nothing is attached.
    run_id = generated_by_run_id_for(gate)
    if run_id is not None:
        repo.attach_generated_by_run_id(report.id, run_id)

    if gate.outcome is GateOutcome.PASS:
        final = repo.publish(report.id, gate)
    else:
        final = repo.transition(report.id, ReportStatus.FAILED)

    session.commit()  # the one and only commit of the main transaction

    return DailyBriefGenerationResult(
        report=final,
        brief_date=window.brief_date,
        window=window,
        selected_event_ids=tuple(event.event_id for event in inputs.top_events),
        selected_event_count=len(inputs.top_events),
        quiet_day=inputs.is_quiet_day,
        gate_outcome=gate.outcome,
        published=final.status == ReportStatus.PUBLISHED.value,
        section_count=len(sections),
        version=final.version,
        change_reason=final.change_reason,
        generated_by_run_id=final.generated_by_run_id,
    )


def _mark_failed_or_raise(
    *,
    report: ReportSnapshot,
    cause: BaseException,
    cleanup_errors: tuple[BaseException, ...],
    session_factory: SessionFactory,
    lifecycle_repository_factory: LifecycleRepositoryFactory,
) -> NoReturn:
    """In a clean transaction, mark the durably-created report `failed`, then raise a typed error.

    ``cleanup_errors`` are faults already collected while abandoning the prior session (a failed
    rollback and/or close); they are preserved, never dropped. A fresh session reloads the report and
    legally moves ``generating``/``grounding_check`` to ``failed``; a report already terminal is never
    rewritten, and ``durable_failure_marked`` then reflects only whether it is already ``failed`` --
    so a report that had already ``published`` reads as not-marked, accurately distinguishing the two.
    Every fault along the way -- the failure-session acquisition, the reload, the transition, the
    commit, a rollback, a close -- is captured and surfaced, never swallowed; the failure ``close`` is
    always attempted even if its rollback raised, and ``durable_failure_marked`` is ``True`` only if
    the failure commit actually succeeded. All collected cleanup faults are folded deterministically
    into the raised :class:`DailyBriefGenerationError`'s ``cleanup_error``.
    """

    marked = False
    errors: list[BaseException] = list(cleanup_errors)

    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 -- acquisition failed; nothing opened to close
        errors.append(exc)
        _raise_generation_error(report=report, cause=cause, marked=False, errors=errors)

    try:
        repo = lifecycle_repository_factory(session)
        current = repo.get_report(report.id)
        if current is None:
            errors.append(RuntimeError(f"report {report.id} not found during failure cleanup"))
        elif current.status in {status.value for status in TERMINAL_STATUSES}:
            # Already terminal: never rewrite it. `failed` counts as marked; `published` does not.
            marked = current.status == ReportStatus.FAILED.value
        else:
            repo.transition(report.id, ReportStatus.FAILED)
            session.commit()  # `marked` becomes True only once this actually commits
            marked = True
    except Exception as exc:  # noqa: BLE001 -- surfaced in the typed error, never swallowed
        errors.append(exc)
        errors.extend(_rollback_and_close(session))  # close attempted even if the rollback raised
    else:
        close_error = _safe_close(session)
        if close_error is not None:
            errors.append(close_error)

    _raise_generation_error(report=report, cause=cause, marked=marked, errors=errors)


def _raise_generation_error(
    *,
    report: ReportSnapshot,
    cause: BaseException,
    marked: bool,
    errors: list[BaseException],
) -> NoReturn:
    """Raise the one typed error, folding every collected cleanup fault into ``cleanup_error``."""

    raise DailyBriefGenerationError(
        report_id=report.id,
        brief_date=report.brief_date,  # type: ignore[arg-type]  -- a daily brief always has one
        cause=cause,
        durable_failure_marked=marked,
        cleanup_error=_aggregate_cleanup_errors(errors),
    ) from cause


def _rollback_and_close(session: Session) -> tuple[BaseException, ...]:
    """Roll back then close a session, attempting both; return the faults in order, none swallowed.

    The rollback is attempted first and the close is attempted regardless of whether it raised, so a
    session is never leaked because its rollback failed. Callers thread the returned faults into the
    typed failure error rather than letting either escape raw or override a pending typed error.
    """

    errors: list[BaseException] = []
    try:
        session.rollback()
    except Exception as exc:  # noqa: BLE001 -- carried into the typed error, not raised raw
        errors.append(exc)
    try:
        session.close()
    except Exception as exc:  # noqa: BLE001 -- carried into the typed error, not raised raw
        errors.append(exc)
    return tuple(errors)


def _safe_close(session: Session) -> BaseException | None:
    """Attempt ``session.close()``; return the fault if it raised, else ``None``. Never raises.

    The close is always genuinely attempted (so a caller/test can observe the attempt); only its
    fault, if any, is returned for the caller to route through the typed path instead of raw.
    """

    try:
        session.close()
    except Exception as exc:  # noqa: BLE001 -- returned so the caller can route it, never raw
        return exc
    return None


def _aggregate_cleanup_errors(errors: list[BaseException]) -> BaseException | None:
    """Fold zero, one, or many cleanup faults into one deterministic preserved cause.

    ``None`` for none; the lone exception for one; otherwise an :class:`ExceptionGroup` holding them
    in occurrence order, so several coinciding cleanup faults are all preserved and none discarded.
    """

    if not errors:
        return None
    if len(errors) == 1:
        return errors[0]
    return ExceptionGroup("daily brief failure cleanup encountered multiple faults", list(errors))


__all__ = [
    "DEFAULT_CHANGE_REASON",
    "DailyBriefGenerationError",
    "DailyBriefGenerationResult",
    "build_generation_orchestrator_factory",
    "generate_daily_brief",
]
