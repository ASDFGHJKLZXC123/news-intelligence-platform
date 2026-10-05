"""Reusable personal coordinator assembly. Importing it never constructs Celery/broker."""

from __future__ import annotations

import datetime
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from db.base import SessionLocal
from db.models import PersonalProfileRevision, PersonalRun, PersonalWriterMode
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import Settings, get_settings
from services.personal.coordinator import run_personal_daily
from services.personal.deadlines import (
    RuntimeCancelled,
    bind_budget,
    check_budget,
    require_call_budget,
)
from services.personal.local_runtime import PersonalPaidRuntime
from services.personal.offline_fixture import OfflinePersonalFixture, load_offline_personal_fixture
from services.personal.paid_runtime import (
    build_paid_embedding_provider,
    build_paid_orchestrator,
    runtime_readiness,
)
from services.personal.rss_runtime import DeadlineRSSProvider
from services.personal.runs import WriterModeConflict
from services.personal.settings import processing_block_reason
from services.personal.spending import PaidRoute, SpendingLedger

logger = get_logger("services.personal.runner")


@dataclass(frozen=True)
class _RuntimeSpec:
    route: dict[str, Any]
    authorized_spend_usd: float
    ai_block_reason: str | None = None
    workspace_id: uuid.UUID | None = None


def validate_delivery_configuration(
    runtime, durable_mode, delivery_transport, recorded_transport=None
):
    if runtime.personal_processing_mode != durable_mode or durable_mode != "personal":
        raise WriterModeConflict("configured personal processing mode disagrees with durable mode")
    if runtime.personal_processing_transport != delivery_transport:
        raise WriterModeConflict(
            "actual delivery transport differs from selected runtime transport"
        )
    if recorded_transport is not None and recorded_transport != delivery_transport:
        raise WriterModeConflict("personal run was delivered by another runtime transport")


def _runtime_spec(
    run_id: uuid.UUID,
    token: uuid.UUID,
    *,
    runtime: Settings,
    session_factory=SessionLocal,
    delivery_transport: str | None = None,
) -> _RuntimeSpec:
    with session_factory() as session:
        row = session.execute(
            select(PersonalRun, PersonalProfileRevision)
            .join(
                PersonalProfileRevision,
                PersonalProfileRevision.id == PersonalRun.profile_revision_id,
            )
            .where(PersonalRun.id == run_id)
        ).one_or_none()
        if row is None:
            raise ValueError("personal run or frozen profile is unavailable")
        run, profile = row
        durable_mode = session.scalar(
            select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
        )
        validate_delivery_configuration(
            runtime,
            durable_mode,
            delivery_transport or runtime.personal_processing_transport,
            run.delivery_transport,
        )
        if run.ownership_token != token or run.state != "queued":
            raise ValueError("personal worker delivery is stale")
        settings = profile.settings or {}
        route = settings.get("model_route")
        if (
            profile.schema_revision != "personal-profile.v2"
            and profile.execution_profile == "assisted"
            and (not isinstance(route, dict) or not route)
        ):
            raise ValueError("assisted personal run has no frozen model route")
        try:
            authorized_spend = float(settings.get("authorized_spend_usd") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("personal spending allowance must be finite") from exc
        if not math.isfinite(authorized_spend) or authorized_spend < 0:
            raise ValueError("personal spending allowance must be finite and non-negative")
        reason = (
            processing_block_reason(profile)
            if profile.schema_revision == "personal-profile.v2"
            else None
        )
        # Implemented capability is separate from activation: the server gate defaults
        # off, and frozen profile configuration/credentials alone cannot enable dispatch.
        if (
            profile.schema_revision == "personal-profile.v2"
            and isinstance(route, dict)
            and route.get("mode") == "live"
        ):
            reason = reason or runtime_readiness(runtime, route)
        return _RuntimeSpec(
            route=(
                {"mode": "raw"}
                if profile.execution_profile == "raw" or reason is not None
                else dict(route)
                if isinstance(route, dict)
                else {}
            ),
            authorized_spend_usd=authorized_spend,
            ai_block_reason=reason,
            workspace_id=run.workspace_id
            if profile.schema_revision == "personal-profile.v2"
            else None,
        )


def _close(resource: object | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _offline_runtime(
    settings: Settings, spec: _RuntimeSpec
) -> tuple[OfflinePersonalFixture, object, None]:
    if settings.app_env not in {"local", "test"}:
        raise ValueError("offline personal fixtures are limited to local/test processes")
    fixture = load_offline_personal_fixture(settings.personal_offline_fixture_path)
    fixture.validate_route(spec.route)
    return fixture, fixture.embedding_provider(), None


class _DisabledEmbeddingProvider:
    model_name = "disabled"
    model_version = "raw"

    def embed(self, _texts):  # noqa: ANN001, ANN202
        raise RuntimeError("raw personal profile attempted an embedding dispatch")


def _mark_queued_setup_failure(run_id, token, *, session_factory=SessionLocal):
    from services.personal.processing import fail_owned_launch

    with bind_budget(None), session_factory() as session:
        try:
            run = session.get(PersonalRun, run_id)
            if run is None:
                return False
            failed = fail_owned_launch(
                session,
                run_id,
                token,
                run.fencing_generation,
                code="worker_setup_failed",
                message="Personal update could not start; an explicit retry may be available.",
            )
            session.commit()
            return failed
        except Exception:
            session.rollback()
            return False


class BudgetRedisClient:
    """Keep 4A Redis calls finite within the remaining graceful budget."""

    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        method = getattr(self.delegate, name)
        if not callable(method):
            return method

        def bounded(*args, **kwargs):
            if name != "close":
                # Five seconds each for connect and response, with no retries.
                require_call_budget(10)
            return method(*args, **kwargs)

        return bounded


def execute_personal_task(
    run_id: uuid.UUID,
    token: uuid.UUID,
    *,
    now: datetime.datetime | None = None,
    session_factory=SessionLocal,
    settings_factory=get_settings,
    rss_provider_factory=DeadlineRSSProvider,
    generation: int | None = None,
    on_acquired=None,
    rotate_token: bool = False,
    delivery_transport: str | None = None,
) -> dict[str, Any]:
    """Execute the registered worker behavior; tests call this same boundary directly."""

    set_job_id(str(run_id))
    metrics.increment(metrics.JOB_STARTS)
    embedding_provider: object | None = None
    fixture: OfflinePersonalFixture | None = None
    ledger: SpendingLedger | None = None
    try:
        settings = settings_factory()
        spec = _runtime_spec(
            run_id,
            token,
            runtime=settings,
            session_factory=session_factory,
            delivery_transport=delivery_transport,
        )
        if spec.route.get("mode") == "raw":
            embedding_provider = _DisabledEmbeddingProvider()

            def rss_factory(_source):  # noqa: ANN001, ANN202
                return rss_provider_factory()

            def orchestrator_factory(*_args, **_kwargs):  # noqa: ANN202
                raise RuntimeError("raw personal profile attempted report generation")

            fixture_label = None
        elif spec.route.get("mode") == "offline_fixture":
            fixture, embedding_provider, _ = _offline_runtime(settings, spec)
            rss_factory = fixture.rss_provider
            orchestrator_factory = fixture.orchestrator
            fixture_label = fixture.label
        elif spec.route.get("mode") == "live" and spec.workspace_id is not None:
            # Durable personal ownership permits one runner regardless of delivery
            # transport. Celery delivery must not add a Redis dependency to this
            # business runtime; legacy processing keeps its own explicit Redis assembly.
            local_runtime = PersonalPaidRuntime.from_settings(
                settings,
                {
                    spec.route["generation"]["provider"],
                    spec.route["embedding"]["provider"],
                },
            )
            ledger = SpendingLedger(
                session_factory=session_factory,
                workspace_id=spec.workspace_id,
                run_id=run_id,
                ownership_token=token,
            )
            generation_route = PaidRoute.from_mapping(spec.route["generation"], role="generation")
            embedding_provider = build_paid_embedding_provider(
                settings=settings,
                route=PaidRoute.from_mapping(spec.route["embedding"], role="embedding"),
                ledger=ledger,
                local_runtime=local_runtime,
            )

            def rss_factory(_source):  # noqa: ANN001, ANN202
                return rss_provider_factory()

            def orchestrator_factory(session, frozen_route):  # noqa: ANN001, ANN202
                if frozen_route != spec.route:
                    raise ValueError("personal snapshot route differs from its frozen worker route")
                return build_paid_orchestrator(
                    session=session,
                    settings=settings,
                    route=generation_route,
                    ledger=ledger,
                    local_runtime=local_runtime,
                )

            fixture_label = None
        else:
            raise ValueError(
                "live assisted processing requires the dedicated bounded smoke harness"
            )

        def acquired(run):
            if ledger is not None:
                ledger.ownership_token = run.ownership_token
            if on_acquired is not None:
                on_acquired(run)

        check_budget()
        result = run_personal_daily(
            run_id,
            token,
            session_factory=session_factory,
            rss_provider_factory=rss_factory,
            embedding_provider=embedding_provider,
            orchestrator_factory=orchestrator_factory,
            now=now or datetime.datetime.now(datetime.UTC),
            ai_block_reason=spec.ai_block_reason,
            generation=generation,
            on_acquired=acquired,
            rotate_token=rotate_token,
            paid_block_reason=(lambda: ledger.blocked_code) if ledger is not None else None,
        )
        with session_factory() as session:
            terminal_state = session.scalar(
                select(PersonalRun.state).where(PersonalRun.id == run_id)
            )
        if terminal_state == "succeeded":
            metrics.increment(metrics.JOB_SUCCESSES)
        else:
            metrics.increment(metrics.JOB_FAILURES)
        return {
            "status": terminal_state,
            "run_id": str(result.run_id),
            "report_id": str(result.report.report.id) if result.report is not None else None,
            "published": result.report.published if result.report is not None else False,
            "feeds_attempted": result.feeds_attempted,
            "feeds_succeeded": result.feeds_succeeded,
            "feeds_failed": result.feeds_failed,
            "articles_captured": result.articles_captured,
            "articles_admitted": result.articles_admitted,
            "events_observed": result.events_observed,
            "claims_supported": result.claims_supported,
            "fixture_label": fixture_label,
        }
    except (Exception, RuntimeCancelled) as exc:
        metrics.increment(metrics.JOB_FAILURES)
        if not isinstance(exc, WriterModeConflict):
            _mark_queued_setup_failure(run_id, token, session_factory=session_factory)
        logger.exception("personal daily task failed", extra={"personal_run_id": str(run_id)})
        raise
    finally:
        try:
            for resource in (embedding_provider,):
                try:
                    _close(resource)
                except Exception:
                    logger.warning(
                        "personal task resource cleanup failed",
                        exc_info=True,
                        extra={"personal_run_id": str(run_id)},
                    )
        finally:
            set_job_id(None)
