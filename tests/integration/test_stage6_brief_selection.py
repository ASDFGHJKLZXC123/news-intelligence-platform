"""Stage 6 against a live database: migration 0016, and the SQL that loads brief inputs.

The selection *rules* are pure and are tested without a database in tests/unit/. What can
only be checked against a real Postgres is that migration 0016 runs and preserves rows, and
that the repository's SQL -- the window predicate, the four correlated risk maxima, the
credibility sum, the `peak_severity` filter, the version pick -- really returns the rows the
pure layer is written against.

Database hygiene (per the Stage 6 workflow rules), enforced structurally:

- **Never the default `news` database.** Every test owns a throwaway ``nip_stage6_sel_<hex>``
  database on the disposable host (localhost:55432 by default), created and dropped -- with a
  leak check -- by :func:`tests.integration._stage6_db.disposable_database`. The default engine
  (`db.base.engine`/`SessionLocal`, bound to whatever `DATABASE_URL` names) is never imported
  and never used, for data or for migrations.
- **Alembic targets the disposable database, not the default.** `alembic/env.py` reads the URL
  from application `Settings` at run time, so the fixture overrides ``DATABASE_URL`` and clears
  the `get_settings` cache around the whole test, then *asserts* the settings now resolve to the
  disposable database before any migration runs. Accidental default targeting is therefore
  structurally impossible: the fixture refuses to proceed unless the target is the disposable db.
- The override and the disposable database are both torn down in ``finally``, so a crash cannot
  leave the process pointed at, or a database on, the server.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from db.models import (
    Alert,
    Article,
    Company,
    EntityProfile,
    Event,
    EventArticle,
    EventCompany,
    EventIndustry,
    Report,
    ReportSection,
    RiskScoreObservation,
    RiskWarning,
    Source,
    User,
)
from packages.config.settings import get_settings
from services.reports import (
    DAILY_BRIEF_REPORT_TYPE,
    EVENT_RISK_TARGET_TYPE,
    SQLAlchemyBriefInputRepository,
    build_brief_inputs,
    window_for_date,
)
from tests.integration._stage6_db import (
    FORBIDDEN_DB,
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration

_ALEMBIC = Config("alembic.ini")
_SEL_PREFIX = "nip_stage6_sel_"

BRIEF_DATE = datetime.date(2026, 7, 14)
WINDOW = window_for_date(BRIEF_DATE)
INSIDE = WINDOW.end - datetime.timedelta(hours=2)
BEFORE = WINDOW.start - datetime.timedelta(hours=2)


@contextlib.contextmanager
def _database_url_override(url: str) -> Iterator[None]:
    """Point application `Settings` (and thus Alembic's env.py) at ``url`` for the duration.

    ``get_settings`` is ``lru_cache``d and `alembic/env.py` reads it fresh on every command, so
    both the env var and the cache must be swapped -- and restored -- or the override would leak
    into the rest of the session (and the default engine's view would drift).
    """
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


def _assert_settings_target(name: str) -> None:
    """Refuse to run unless `Settings` now resolve to the disposable database, never `news`."""
    target = get_settings().database_url.rsplit("/", 1)[-1]
    assert target == name, f"settings target {target!r} is not the disposable database {name!r}"
    assert target.startswith(_SEL_PREFIX)
    assert target != FORBIDDEN_DB


class _SelectionDB:
    """A migrated, disposable database and its own engine. Never the default engine."""

    def __init__(self, engine, name: str) -> None:  # noqa: ANN001 - Engine
        self.engine = engine
        self.name = name


@pytest.fixture
def selection_db() -> Iterator[_SelectionDB]:
    """A disposable database migrated to head, with Alembic and all sessions pointed at it."""
    require_disposable_postgres()
    with disposable_database(_SEL_PREFIX) as name:
        with _database_url_override(url_for(name)):
            _assert_settings_target(name)
            engine = create_engine(url_for(name))
            # Structural guard: this engine can only be the disposable database, never `news`.
            assert engine.url.database == name != FORBIDDEN_DB
            try:
                command.upgrade(_ALEMBIC, "head")
                yield _SelectionDB(engine, name)
            finally:
                engine.dispose()


# --------------------------------------------------------------------------------------
# Migration 0016
# --------------------------------------------------------------------------------------


def test_migration_adds_hotness_without_disturbing_existing_events(
    selection_db: _SelectionDB,
) -> None:
    engine = selection_db.engine
    command.downgrade(_ALEMBIC, "0015")
    with engine.begin() as conn:
        assert "hotness_score" not in {c["name"] for c in inspect(conn).get_columns("events")}
        conn.execute(
            text(
                "INSERT INTO events (id, title, severity_score, article_count, source_count) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 'Legacy event', 61.5, 4, 2)"
            )
        )

    command.upgrade(_ALEMBIC, "0016")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT severity_score, hotness_score FROM events WHERE title = 'Legacy event'")
        ).one()
        # The event survives with its severity intact, and its hotness is NULL -- not a number
        # reverse-engineered from inputs the pipeline never recorded for it.
        assert float(row.severity_score) == 61.5
        assert row.hotness_score is None


def test_the_hotness_check_constraint_is_live(selection_db: _SelectionDB) -> None:
    with (
        selection_db.engine.begin() as conn,
        pytest.raises(Exception, match="ck_events_hotness_score"),
    ):
        conn.execute(
            text(
                "INSERT INTO events (id, title, hotness_score) "
                "VALUES ('22222222-2222-2222-2222-222222222222', 'Too hot', 100.01)"
            )
        )


def test_migration_is_reversible(selection_db: _SelectionDB) -> None:
    engine = selection_db.engine
    command.downgrade(_ALEMBIC, "0015")
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert "hotness_score" not in {c["name"] for c in inspector.get_columns("events")}
        assert "ix_events_updated_at_hotness" not in {
            i["name"] for i in inspector.get_indexes("events")
        }
    command.upgrade(_ALEMBIC, "head")
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert "hotness_score" in {c["name"] for c in inspector.get_columns("events")}
        assert "ix_events_updated_at_hotness" in {
            i["name"] for i in inspector.get_indexes("events")
        }


# --------------------------------------------------------------------------------------
# The repository's SQL
# --------------------------------------------------------------------------------------


def _seed(engine) -> dict[str, uuid.UUID]:  # noqa: ANN001 - Engine
    """One window's worth of rows, built so every SQL path has something to find."""
    ids: dict[str, uuid.UUID] = {}
    with Session(engine) as session:
        broadsheet = Source(name="Broadsheet", feed_url="https://a.test/f", authority_score=0.9)
        tabloid = Source(name="Tabloid", feed_url="https://b.test/f", authority_score=0.2)
        unrated = Source(name="Unrated", feed_url="https://c.test/f", authority_score=None)
        session.add_all([broadsheet, tabloid, unrated])
        session.flush()

        # `hot` ranks on hotness alone; `risky` is cooler but carries a big linked risk;
        # `cold` is below the floor; `stale` updated before the window opened.
        hot = Event(title="Hot", hotness_score=90.0, updated_at=INSIDE, first_seen_at=BEFORE)
        risky = Event(title="Risky", hotness_score=45.0, updated_at=INSIDE, first_seen_at=INSIDE)
        cold = Event(title="Cold", hotness_score=12.0, updated_at=INSIDE, first_seen_at=INSIDE)
        unscored = Event(title="Unscored", hotness_score=None, updated_at=INSIDE)
        stale = Event(title="Stale", hotness_score=99.0, updated_at=BEFORE, first_seen_at=BEFORE)
        session.add_all([hot, risky, cold, unscored, stale])
        session.flush()
        ids |= {"hot": hot.id, "risky": risky.id, "cold": cold.id, "stale": stale.id}

        # Two articles from the *same* credible source plus one from a weak one: the
        # credibility sum is over linked articles (0.9 + 0.9 + 0.2), not distinct sources.
        for index, source in enumerate((broadsheet, broadsheet, tabloid, unrated)):
            article = Article(
                source_id=source.id,
                url=f"https://x.test/{index}",
                url_hash=f"hash-{index}",
                title=f"article {index}",
            )
            session.add(article)
            session.flush()
            session.add(EventArticle(event_id=hot.id, article_id=article.id))

        # Four linked risk sources on `risky`; the warning's 88 is the true maximum.
        session.add(
            RiskScoreObservation(
                target_type=EVENT_RISK_TARGET_TYPE,
                target_id=str(risky.id),
                risk_type="banking",
                score=30.0,
                level="medium",
                as_of=INSIDE,
            )
        )
        session.add(
            RiskWarning(
                event_id=risky.id,
                risk_type="banking",
                risk_score=88.0,
                probability=0.4,
                horizon="0_6m",
                severity="high",
                warning_message="w",
                created_at=INSIDE,
            )
        )
        profile = EntityProfile(canonical_name="Acme", normalized_name="acme")
        session.add(profile)
        session.flush()
        company = Company(entity_profile_id=profile.id, display_name="Acme")
        session.add(company)
        session.flush()
        session.add(
            EventCompany(
                event_id=risky.id, company_id=company.id, risk_score=55.0, created_at=INSIDE
            )
        )
        session.add(
            EventIndustry(
                event_id=risky.id, industry_id="banks", risk_score=12.0, created_at=INSIDE
            )
        )
        # An observation dated after the cutoff must be invisible to this brief.
        session.add(
            RiskScoreObservation(
                target_type=EVENT_RISK_TARGET_TYPE,
                target_id=str(risky.id),
                risk_type="banking",
                score=99.0,
                level="critical",
                as_of=WINDOW.end + datetime.timedelta(hours=1),
            )
        )

        # A country risk that moved across the window.
        for score, level, as_of in (
            (40.0, "medium", WINDOW.start - datetime.timedelta(days=1)),
            (72.0, "high", INSIDE),
        ):
            session.add(
                RiskScoreObservation(
                    target_type="country",
                    target_id="TR",
                    risk_type="currency",
                    score=score,
                    level=level,
                    as_of=as_of,
                )
            )

        user = User(email="brief@test.example")
        session.add(user)
        session.flush()
        # A resolved all-clear: severity has decayed to Low, peak_severity remembers Critical.
        session.add(
            Alert(
                user_id=user.id,
                title="All clear",
                message="m",
                severity="low",
                peak_severity="critical",
                alert_type="risk_threshold",
                state="resolved",
                resolved_at=INSIDE,
                updated_at=INSIDE,
            )
        )
        # Never rose above Medium: not the brief's business, even on resolution.
        session.add(
            Alert(
                user_id=user.id,
                title="Minor",
                message="m",
                severity="low",
                peak_severity="medium",
                alert_type="risk_threshold",
                state="resolved",
                resolved_at=INSIDE,
                updated_at=INSIDE,
            )
        )

        # Yesterday's brief: v2 published is the one that counts, not v3 (still generating).
        yesterday = BRIEF_DATE - datetime.timedelta(days=1)
        for version, status in ((1, "published"), (2, "published"), (3, "generating")):
            report = Report(
                report_type=DAILY_BRIEF_REPORT_TYPE,
                brief_date=yesterday,
                title=f"Brief v{version}",
                status=status,
                version=version,
            )
            session.add(report)
            session.flush()
            ids[f"report_v{version}"] = report.id
            session.add(
                ReportSection(
                    report_id=report.id,
                    section_order=1,
                    title="Risk radar",
                    body=f"body v{version}",
                    evidence_refs=[uuid.UUID(int=version)],
                )
            )

        session.commit()
    return ids


def test_repository_loads_the_windows_events_with_risk_and_credibility(
    selection_db: _SelectionDB,
) -> None:
    ids = _seed(selection_db.engine)
    with Session(selection_db.engine) as session:
        rows = SQLAlchemyBriefInputRepository(
            session,
            prediction_backed_outputs_enabled=True,
        ).events_in_window(WINDOW)

    by_id = {row.event_id: row for row in rows}
    # The window predicate is `(previous cutoff, cutoff]`: the stale event is excluded.
    assert ids["stale"] not in by_id
    assert ids["hot"] in by_id and ids["risky"] in by_id and ids["cold"] in by_id

    hot = by_id[ids["hot"]]
    # Summed over linked articles: 0.9 + 0.9 + 0.2 + NULL. A NULL authority adds nothing, and
    # the twice-covering credible source is counted twice -- this is not a source count.
    assert hot.credibility_sum == pytest.approx(2.0)
    assert hot.observation_risk is None  # no linked risk at all, not a risk of zero

    risky = by_id[ids["risky"]]
    assert risky.observation_risk == pytest.approx(30.0)  # the post-cutoff 99 is invisible
    assert risky.warning_risk == pytest.approx(88.0)
    assert risky.company_risk == pytest.approx(55.0)
    assert risky.industry_risk == pytest.approx(12.0)


def test_repository_finds_an_all_clear_by_its_peak_severity(selection_db: _SelectionDB) -> None:
    _seed(selection_db.engine)
    with Session(selection_db.engine) as session:
        changes = SQLAlchemyBriefInputRepository(
            session,
            prediction_backed_outputs_enabled=True,
        ).alert_state_changes(WINDOW)

    titles = {change.title for change in changes}
    # The Critical alert's stand-down is found even though its live severity reads 'low'.
    assert "All clear" in titles
    assert "Minor" not in titles


def test_repository_returns_every_version_and_selection_picks_the_published_one(
    selection_db: _SelectionDB,
) -> None:
    ids = _seed(selection_db.engine)
    yesterday = BRIEF_DATE - datetime.timedelta(days=1)
    with Session(selection_db.engine) as session:
        versions = SQLAlchemyBriefInputRepository(
            session,
            prediction_backed_outputs_enabled=True,
        ).prior_brief_versions(yesterday)

    assert {version.version for version in versions} == {1, 2, 3}
    assert ids["report_v2"] in {version.report_id for version in versions}


def test_end_to_end_selection_over_the_live_schema(selection_db: _SelectionDB) -> None:
    ids = _seed(selection_db.engine)
    with Session(selection_db.engine) as session:
        inputs = build_brief_inputs(
            SQLAlchemyBriefInputRepository(
                session,
                prediction_backed_outputs_enabled=True,
            ),
            WINDOW,
        )

    # `risky` outranks `hot`: 0.6*45 + 0.4*88 = 62.2 against 0.6*90 + 0.4*0 = 54.
    assert [event.event_id for event in inputs.top_events] == [ids["risky"], ids["hot"]]
    assert inputs.top_events[0].ranking_score == pytest.approx(62.2)
    assert inputs.top_events[0].max_linked_risk.provenance == "risk_warning"
    assert inputs.top_events[1].ranking_score == pytest.approx(54.0)
    # `hot` began before the window opened and updated into it.
    assert inputs.top_events[1].developing is True
    assert inputs.top_events[0].developing is False

    # The sub-40 event and the unscored one are both absent, and both absences are declared.
    assert ids["cold"] not in {event.event_id for event in inputs.top_events}
    assert "events_missing_hotness" in {note.code for note in inputs.data_quality_notes}

    assert [change.title for change in inputs.executive_summary.alert_state_changes] == [
        "All clear"
    ]
    move = inputs.executive_summary.largest_risk_move
    assert move is not None
    assert move.key.target_id == "TR"
    assert move.delta == pytest.approx(32.0)
    assert move.is_reversal  # medium -> high

    assert inputs.prior_brief is not None
    assert inputs.prior_brief.version == 2
    assert inputs.prior_brief.key_claim_ids == (uuid.UUID(int=2),)
