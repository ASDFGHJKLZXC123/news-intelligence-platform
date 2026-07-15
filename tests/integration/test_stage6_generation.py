"""Stage 6 daily-brief generation end-to-end, against a live disposable Postgres.

What can only be proven against a real database lives here: that the coordinator drives the production
SQL selection/context repositories and the lifecycle repository through one composed-and-grounded run
with genuine transaction boundaries -- a durable start committed on its own, a single main commit, a
``generated_by_run_id`` foreign key that resolves to a real ``llm_runs`` row, and an unexpected fault
that rolls the main transaction back (no partial sections, no orphan audit rows) while the durably
created *same* version is marked ``failed`` in a clean transaction.

The LLM is a scripted orchestrator that returns validated ``ReportComposition``/``ClaimGrounding``
contracts with no network, and persists a real ``LLMRun`` row per call *into the coordinator's main
session* -- so the audit rows share that transaction (committing with the report or rolling back with
it) and the provenance FK has a real target, without weakening any database constraint.

Database hygiene (per the Stage 6 workflow rules): never the default ``news`` database. Each test owns
a throwaway ``nip_stage6_generation_<hex>`` database on ``localhost:55432``, migrated to head, and
dropped -- with a leak check -- in ``finally``. The default engine (`db.base`) is never imported.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from db.models.core import (
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EventArticle,
    EvidenceItem,
    LLMRun,
    Report,
    ReportSection,
    Source,
)
from packages.config.settings import get_settings
from services.llm.adapters import LLMInvocationMode
from services.llm.contracts import validate_llm_contract_payload
from services.llm.orchestrator import LLMOrchestratorResult
from services.llm.policy import LLMTier
from services.llm.selection import RepresentativeArticleSelection
from services.reports import (
    DAILY_BRIEF_REPORT_TYPE,
    DailyBriefGenerationError,
    generate_daily_brief,
    window_for_date,
)
from services.reports.grounding import GateOutcome
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_SCHEMA,
    GROUNDING_SCHEMA_VERSION,
)
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    COMPOSITION_SCHEMA_VERSION,
)
from tests.integration._stage6_db import (
    FORBIDDEN_DB,
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration

UTC = datetime.UTC
_ALEMBIC = Config("alembic.ini")
_GEN_PREFIX = "nip_stage6_generation_"

BUSY_DATE = datetime.date(2026, 7, 14)
QUIET_DATE = datetime.date(2026, 7, 20)
WINDOW = window_for_date(BUSY_DATE)
INSIDE = WINDOW.end - datetime.timedelta(hours=2)
BEFORE = WINDOW.start - datetime.timedelta(hours=6)

_EMPTY_SELECTION = RepresentativeArticleSelection(
    selected_articles=(),
    used_token_budget=0,
    selected_count=0,
    dropped_count=0,
    truncated_by_budget=False,
)

#: Per-section word targets, so a scripted composition lands inside each budget and is not degraded.
_SECTION_TARGETS = {"executive_summary": 108, "top_event": 150, "risk_radar": 60, "historical_parallels": 100}


# --------------------------------------------------------------------------------------
# Disposable, migrated database (never `news`)
# --------------------------------------------------------------------------------------


@contextlib.contextmanager
def _database_url_override(url: str) -> Iterator[None]:
    """Point application `Settings` (and thus Alembic's env.py) at ``url`` for the duration."""
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A disposable database migrated to head; Alembic is asserted to target it, never `news`."""
    require_disposable_postgres()
    with disposable_database(_GEN_PREFIX) as name:
        with _database_url_override(url_for(name)):
            target = get_settings().database_url.rsplit("/", 1)[-1]
            assert target == name and target.startswith(_GEN_PREFIX) and target != FORBIDDEN_DB
            engine = create_engine(url_for(name))
            assert engine.url.database == name != FORBIDDEN_DB
            try:
                command.upgrade(_ALEMBIC, "head")
                yield engine
            finally:
                engine.dispose()


# --------------------------------------------------------------------------------------
# Scripted orchestrator: validated contracts, real LLMRun rows sharing the main session
# --------------------------------------------------------------------------------------


def _words(count: int) -> str:
    return " ".join(f"w{i}" for i in range(count))


def _compose_payload(request: Any) -> dict[str, Any]:
    kind = request.context["section_kind"]
    claim_ids = list(request.context["allowed_claim_ids"])
    base_payload = {
        "schema_name": COMPOSITION_SCHEMA,
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "prompt_template_version": COMPOSITION_PROMPT_TEMPLATE_VERSION,
    }
    if not claim_ids:
        return {**base_payload, "blocks": [], "no_finding_reason": "nothing citable"}
    target = _SECTION_TARGETS.get(kind, 100)
    base = max(1, target // len(claim_ids))
    return {
        **base_payload,
        "blocks": [{"text": _words(base), "claim_ids": [cid]} for cid in claim_ids],
    }


def _all_supported_payload(request: Any) -> dict[str, Any]:
    return {
        "schema_name": GROUNDING_SCHEMA,
        "schema_version": GROUNDING_SCHEMA_VERSION,
        "prompt_template_version": GROUNDING_PROMPT_TEMPLATE_VERSION,
        "verdicts": [
            {"claim_id": cid, "verdict": "supported"} for cid in request.context["allowed_claim_ids"]
        ],
    }


class ScriptedDBOrchestrator:
    """Returns validated contracts and persists one real ``LLMRun`` per call into ``session``.

    ``compose``/``ground`` are ``(request) -> payload`` callables; a callable may raise to model an
    unexpected mid-pipeline fault. The persisted run row shares the caller's transaction (added and
    flushed, never committed), so its id is a real ``generated_by_run_id`` FK target and it rolls back
    with the report on a fault.
    """

    def __init__(self, session: Session, *, compose: Any = None, ground: Any = None) -> None:
        self._session = session
        self._compose = compose or _compose_payload
        self._ground = ground or _all_supported_payload
        self.run_ids: list[uuid.UUID] = []

    def run(self, request: Any) -> LLMOrchestratorResult:
        schema = request.requested_schema
        if schema == COMPOSITION_SCHEMA:
            payload, tier = self._compose(request), LLMTier.T2
        elif schema == GROUNDING_SCHEMA:
            payload, tier = self._ground(request), LLMTier.T1
        else:  # pragma: no cover - a mis-routed schema is a test bug
            raise AssertionError(f"unexpected schema {schema}")

        contract = validate_llm_contract_payload(
            schema_name=schema, payload=payload, allowed_ids=list(request.allowed_ids)
        )
        run = LLMRun(
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_template_version=request.prompt_template_version,
            provider="scripted",
            model="scripted-1",
            status="succeeded",
            output_schema_name=schema,
            trace_id="scripted-trace",
        )
        self._session.add(run)
        self._session.flush()  # populate run.id as a real FK target, still inside the caller's txn
        self.run_ids.append(run.id)
        return LLMOrchestratorResult(
            run=run,
            contract=contract,
            trace_id="scripted-trace",
            cache_hit=False,
            tier=tier,
            mode=LLMInvocationMode.REALTIME,
            queue="essential",
            degraded_provider=None,
            route_degradation_reasons=(),
            selected_articles=_EMPTY_SELECTION,
        )


def _session_factory(engine: Engine):  # noqa: ANN202
    return lambda: Session(engine)


def _orchestrator_factory(*, compose: Any = None, ground: Any = None):  # noqa: ANN202
    return lambda session: ScriptedDBOrchestrator(session, compose=compose, ground=ground)


# --------------------------------------------------------------------------------------
# Seeding: one window's worth of a citable event
# --------------------------------------------------------------------------------------


def _seed_busy_event(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """Event -> article -> article EvidenceItem -> supportive Claim, all inside the busy window."""
    with Session(engine) as session:
        source = Source(name="Reuters", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.9)
        session.add(source)
        session.flush()
        article = Article(
            source_id=source.id,
            url=f"https://x/{uuid.uuid4()}",
            url_hash=uuid.uuid4().hex,
            title="Regional bank under funding pressure",
            summary="A concise summary of the funding pressure.",
            body="Body text about the bank.",
            published_at=INSIDE,
        )
        session.add(article)
        session.flush()
        event = Event(
            title="Regional bank stress",
            hotness_score=72.0,
            updated_at=INSIDE,
            first_seen_at=BEFORE,
        )
        session.add(event)
        session.flush()
        session.add(EventArticle(event_id=event.id, article_id=article.id))
        evidence = EvidenceItem(
            source_type="article", source_id=str(article.id), title="Regional bank under funding pressure"
        )
        session.add(evidence)
        session.flush()
        claim = Claim(claim_text="The bank faces a liquidity squeeze.", confidence_score=0.9)
        session.add(claim)
        session.flush()
        session.add(
            ClaimEvidence(claim_id=claim.id, evidence_item_id=evidence.id, support_type="supports")
        )
        session.commit()
        return event.id, claim.id


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------


def test_busy_brief_publishes_with_real_citations_and_a_real_run_id(engine: Engine) -> None:
    event_id, claim_id = _seed_busy_event(engine)

    result = generate_daily_brief(
        BUSY_DATE,
        session_factory=_session_factory(engine),
        orchestrator_factory=_orchestrator_factory(),
    )

    assert result.published is True
    assert result.gate_outcome is GateOutcome.PASS
    assert result.quiet_day is False
    assert result.version == 1
    assert result.change_reason is None
    assert event_id in result.selected_event_ids
    assert result.generated_by_run_id is not None

    with Session(engine) as session:
        report = session.get(Report, result.report.id)
        assert report.status == "published"
        assert report.report_type == DAILY_BRIEF_REPORT_TYPE
        assert report.user_id is None
        # The provenance FK resolves to a real, audit-compatible llm_runs row.
        run = session.get(LLMRun, report.generated_by_run_id)
        assert run is not None and run.provider == "scripted"
        # A generated section cites the real claim, and the disclaimer is last.
        sections = (
            session.execute(
                select(ReportSection)
                .where(ReportSection.report_id == report.id)
                .order_by(ReportSection.section_order)
            )
            .scalars()
            .all()
        )
        assert sections, "a published brief must have sections"
        cited = {ref for s in sections if s.evidence_refs for ref in s.evidence_refs}
        assert claim_id in cited  # claim-level citation, resolved end-to-end
        assert sections[-1].grounding_status == "passed"
        # Audit rows are present and committed with the report (the orchestrator shared the txn).
        run_count = session.execute(select(func.count()).select_from(LLMRun)).scalar_one()
        assert run_count >= 1


def test_a_quiet_day_publishes_with_no_events_no_runs_and_no_run_id(engine: Engine) -> None:
    # No events seeded for QUIET_DATE: a valid, published quiet brief.
    result = generate_daily_brief(
        QUIET_DATE,
        session_factory=_session_factory(engine),
        orchestrator_factory=_orchestrator_factory(),
    )

    assert result.quiet_day is True
    assert result.published is True
    assert result.gate_outcome is GateOutcome.PASS
    assert result.selected_event_count == 0
    assert result.generated_by_run_id is None

    with Session(engine) as session:
        report = session.get(Report, result.report.id)
        assert report.status == "published"
        assert report.generated_by_run_id is None
        run_count = session.execute(select(func.count()).select_from(LLMRun)).scalar_one()
        assert run_count == 0  # a quiet brief makes no LLM call


def test_a_mid_pipeline_fault_fails_the_same_version_and_leaves_no_partial_rows(engine: Engine) -> None:
    _seed_busy_event(engine)

    def exploding_compose(request: Any) -> dict[str, Any]:
        if request.context["section_kind"] == "top_event":
            raise RuntimeError("injected mid-pipeline fault")
        return _compose_payload(request)

    with pytest.raises(DailyBriefGenerationError) as excinfo:
        generate_daily_brief(
            BUSY_DATE,
            session_factory=_session_factory(engine),
            orchestrator_factory=_orchestrator_factory(compose=exploding_compose),
        )

    error = excinfo.value
    assert error.brief_date == BUSY_DATE
    assert "injected mid-pipeline fault" in error.original_cause_text
    assert error.durable_failure_marked is True
    assert error.cleanup_error is None

    with Session(engine) as session:
        # The durably created version 1 exists and is now `failed` -- no second version was created.
        reports = (
            session.execute(
                select(Report).where(Report.brief_date == BUSY_DATE).order_by(Report.version)
            )
            .scalars()
            .all()
        )
        assert [r.version for r in reports] == [1]
        assert reports[0].id == error.report_id
        assert reports[0].status == "failed"
        # The main transaction rolled back: no sections, and no orphan LLM audit rows.
        section_count = session.execute(
            select(func.count()).select_from(ReportSection)
        ).scalar_one()
        run_count = session.execute(select(func.count()).select_from(LLMRun)).scalar_one()
        assert section_count == 0
        assert run_count == 0


def test_a_rerun_creates_version_2_with_a_reason_and_leaves_version_1_unchanged(engine: Engine) -> None:
    _seed_busy_event(engine)

    v1 = generate_daily_brief(
        BUSY_DATE,
        session_factory=_session_factory(engine),
        orchestrator_factory=_orchestrator_factory(),
    )
    assert v1.version == 1 and v1.published is True

    with Session(engine) as session:
        v1_report = session.get(Report, v1.report.id)
        v1_before = (v1_report.status, v1_report.version, v1_report.change_reason, v1_report.generated_by_run_id)
        v1_sections_before = [
            (s.section_order, s.title, s.body, tuple(s.evidence_refs or ()), s.grounding_status)
            for s in session.execute(
                select(ReportSection)
                .where(ReportSection.report_id == v1.report.id)
                .order_by(ReportSection.section_order)
            )
            .scalars()
            .all()
        ]

    v2 = generate_daily_brief(
        BUSY_DATE,
        session_factory=_session_factory(engine),
        orchestrator_factory=_orchestrator_factory(),
        change_reason="reprocessed upstream event",
    )
    assert v2.version == 2
    assert v2.change_reason == "reprocessed upstream event"
    assert v2.published is True
    assert v2.report.id != v1.report.id  # a new row, never a mutation of v1

    with Session(engine) as session:
        v1_report = session.get(Report, v1.report.id)
        v1_after = (v1_report.status, v1_report.version, v1_report.change_reason, v1_report.generated_by_run_id)
        v1_sections_after = [
            (s.section_order, s.title, s.body, tuple(s.evidence_refs or ()), s.grounding_status)
            for s in session.execute(
                select(ReportSection)
                .where(ReportSection.report_id == v1.report.id)
                .order_by(ReportSection.section_order)
            )
            .scalars()
            .all()
        ]
    assert v1_after == v1_before  # the prior published version is untouched, byte for byte
    assert v1_sections_after == v1_sections_before
    assert v1_after[2] is None  # v1 still stores no change_reason
