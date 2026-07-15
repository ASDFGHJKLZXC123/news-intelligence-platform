"""Stage 6 composition context against a live Postgres: the nontrivial loader joins.

The pure reductions are tested DB-free in tests/unit/test_report_evidence.py,
test_report_parallels.py and test_report_material.py. What can only be checked against a real
Postgres is the SQL that feeds them -- above all the evidence join, which matches an
`evidence_items.source_id` (TEXT, holding `str(article.id)`) against `articles.id` (UUID) by
casting, and would silently return *nothing* if the cast form did not match. A loader that finds
no evidence looks exactly like an event that has none.

Database hygiene (per the Stage 6 workflow rules):

- Never the default `news` database. This module creates and owns a throwaway
  `nip_stage6_ctx_<hex>` database on localhost:55432, and drops it in a ``finally`` -- then
  asserts it is actually gone, so a crashed run cannot leave a leak behind.
- Only the tables the three loaders touch are created (a subset of the metadata), so the test
  needs no PostGIS and no full migration chain.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from db.models.core import (
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EventAnalogy,
    EventArticle,
    EvidenceItem,
    ForecastScenario,
    HistoricalEpisode,
    LLMRun,
    Source,
)
from services.reports.context import ExcerptOrigin
from services.reports.context_repository import SQLAlchemyBriefContextRepository
from tests.integration._stage6_db import (
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration

UTC = datetime.UTC

_CTX_PREFIX = "nip_stage6_ctx_"

_CONTEXT_TABLES = [
    Source.__table__,
    Article.__table__,
    Event.__table__,
    EventArticle.__table__,
    EvidenceItem.__table__,
    Claim.__table__,
    ClaimEvidence.__table__,
    LLMRun.__table__,
    HistoricalEpisode.__table__,
    EventAnalogy.__table__,
    ForecastScenario.__table__,
]

_EMBEDDING = [0.0] * 1535 + [1.0]


@pytest.fixture
def context_db():
    """A throwaway database with only the context tables, dropped and leak-checked on exit.

    Never the default `news` database: :func:`disposable_database` creates and owns a
    ``nip_stage6_ctx_<hex>`` database on the disposable host and drops it (then asserts it is
    gone) in a ``finally``. Only the tables the three loaders touch are created, so no PostGIS
    and no full migration chain are needed. The default engine is never used.
    """
    require_disposable_postgres()
    with disposable_database(_CTX_PREFIX) as name:
        engine = create_engine(url_for(name))
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            from db.base import Base

            Base.metadata.create_all(bind=engine, tables=_CONTEXT_TABLES)
            yield engine
        finally:
            engine.dispose()


def _seed_common(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """One source, one article, one event, wired together. Returns (event_id, article_id)."""
    source = Source(name="Reuters", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.8)
    session.add(source)
    session.flush()

    article = Article(
        source_id=source.id,
        url=f"https://x/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex,
        title="Bank under pressure",
        summary="A concise summary of the article.",
        body="Full body text that must never be exposed whole. " * 20,
        published_at=datetime.datetime(2026, 7, 14, 4, tzinfo=UTC),
    )
    session.add(article)
    session.flush()

    event = Event(title="Regional bank stress", hotness_score=70.0)
    session.add(event)
    session.flush()
    session.add(EventArticle(event_id=event.id, article_id=article.id))
    session.flush()
    return event.id, article.id


def _evidence_item(article_id: uuid.UUID, *, source_type: str = "article") -> EvidenceItem:
    return EvidenceItem(
        source_type=source_type,
        source_id=str(article_id),
        title="Bank under pressure",
        publisher="Reuters",
        url="https://x/a",
        published_at=datetime.datetime(2026, 7, 14, 4, tzinfo=UTC),
    )


# --------------------------------------------------------------------------------------
# Evidence join
# --------------------------------------------------------------------------------------


def test_supportive_linked_claim_is_loaded_through_the_cast_join(context_db) -> None:
    with Session(context_db) as session:
        event_id, article_id = _seed_common(session)
        evidence = _evidence_item(article_id)
        session.add(evidence)
        session.flush()
        claim = Claim(claim_text="The bank faces a liquidity squeeze.", confidence_score=0.9)
        session.add(claim)
        session.flush()
        session.add(
            ClaimEvidence(
                claim_id=claim.id,
                evidence_item_id=evidence.id,
                support_type="supports",
                confidence_score=0.8,
            )
        )
        session.commit()

        rows = SQLAlchemyBriefContextRepository(session).evidence_for_events([event_id]).rows

    assert len(rows) == 1
    row = rows[0]
    assert row.event_id == event_id
    assert row.article_id == article_id
    assert row.support_type == "supports"
    # Summary is preferred over the body, and the body is never exposed whole.
    assert row.excerpt.origin is ExcerptOrigin.SUMMARY
    assert row.excerpt.text == "A concise summary of the article."


def test_contradicting_non_article_and_disconnected_claims_are_all_excluded(context_db) -> None:
    with Session(context_db) as session:
        event_id, article_id = _seed_common(session)

        # (a) A contradicting link on the article's evidence item.
        art_ev = _evidence_item(article_id)
        session.add(art_ev)
        session.flush()
        contra = Claim(claim_text="The bank is fine.", confidence_score=0.9)
        session.add(contra)
        session.flush()
        session.add(
            ClaimEvidence(
                claim_id=contra.id, evidence_item_id=art_ev.id, support_type="contradicts"
            )
        )

        # (b) A supportive link, but on a NON-article evidence item (a filing).
        filing_ev = EvidenceItem(source_type="filing", source_id=str(uuid.uuid4()), title="10-K")
        session.add(filing_ev)
        session.flush()
        filing_claim = Claim(claim_text="Filing-derived claim.", confidence_score=0.9)
        session.add(filing_claim)
        session.flush()
        session.add(
            ClaimEvidence(
                claim_id=filing_claim.id, evidence_item_id=filing_ev.id, support_type="supports"
            )
        )

        # (c) A supportive claim on an article-typed evidence item whose source_id points at NO
        #     article the event links (a disconnected article id).
        orphan_ev = _evidence_item(uuid.uuid4())
        session.add(orphan_ev)
        session.flush()
        orphan_claim = Claim(claim_text="Unlinked claim.", confidence_score=0.9)
        session.add(orphan_claim)
        session.flush()
        session.add(
            ClaimEvidence(
                claim_id=orphan_claim.id, evidence_item_id=orphan_ev.id, support_type="supports"
            )
        )
        session.commit()

        rows = SQLAlchemyBriefContextRepository(session).evidence_for_events([event_id]).rows

    assert rows == ()


def test_rss_evidence_on_a_linked_article_uuid_is_excluded_by_exact_source_type(context_db) -> None:
    with Session(context_db) as session:
        event_id, article_id = _seed_common(session)
        # An `rss`-typed evidence item whose source_id *equals* the linked article's UUID: it
        # satisfies the cast join `evidence_items.source_id = articles.id`, so the only thing
        # keeping it out of the brief is the exact `source_type = 'article'` equality. Supportive,
        # linked, and article-id-matched -- and still excluded, because its type is not `article`.
        rss_ev = _evidence_item(article_id, source_type="rss")
        session.add(rss_ev)
        session.flush()
        claim = Claim(claim_text="RSS-typed claim on a linked article.", confidence_score=0.9)
        session.add(claim)
        session.flush()
        session.add(
            ClaimEvidence(claim_id=claim.id, evidence_item_id=rss_ev.id, support_type="supports")
        )
        session.commit()

        rows = SQLAlchemyBriefContextRepository(session).evidence_for_events([event_id]).rows

    assert rows == ()


def test_body_excerpt_is_bounded_in_the_database(context_db) -> None:
    with Session(context_db) as session:
        source = Source(name="AP", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.6)
        session.add(source)
        session.flush()
        long_body = "z" * 5_000
        article = Article(
            source_id=source.id,
            url=f"https://x/{uuid.uuid4()}",
            url_hash=uuid.uuid4().hex,
            title="No summary",
            summary=None,
            body=long_body,
        )
        session.add(article)
        session.flush()
        event = Event(title="Body-only event", hotness_score=70.0)
        session.add(event)
        session.flush()
        session.add(EventArticle(event_id=event.id, article_id=article.id))
        evidence = _evidence_item(article.id)
        session.add(evidence)
        session.flush()
        claim = Claim(claim_text="Body claim.", confidence_score=0.9)
        session.add(claim)
        session.flush()
        session.add(
            ClaimEvidence(claim_id=claim.id, evidence_item_id=evidence.id, support_type="supports")
        )
        session.commit()

        rows = SQLAlchemyBriefContextRepository(session).evidence_for_events([event.id]).rows

    assert len(rows) == 1
    excerpt = rows[0].excerpt
    assert excerpt.origin is ExcerptOrigin.BODY
    assert len(excerpt.text) == 200
    assert excerpt.truncated is True


# --------------------------------------------------------------------------------------
# Analogy join (event -> analogy -> episode + optional parent)
# --------------------------------------------------------------------------------------


def test_analogy_joins_episode_and_optional_parent(context_db) -> None:
    with Session(context_db) as session:
        event_id, _ = _seed_common(session)
        parent = HistoricalEpisode(
            name="Great Depression",
            episode_type="banking_stress",
            onset_date=datetime.date(1929, 10, 1),
            onset_summary="Crash",
            onset_embedding=_EMBEDDING,
        )
        session.add(parent)
        session.flush()
        episode = HistoricalEpisode(
            name="1998 LTCM",
            episode_type="banking_stress",
            onset_date=datetime.date(1998, 8, 1),
            onset_summary="Leverage unwind",
            onset_embedding=_EMBEDDING,
            outcome_summary="Contained",
            outcomes=["bailout"],
            resolution_mechanism="recap",
            is_counterexample=True,
            parent_episode_id=parent.id,
        )
        session.add(episode)
        session.flush()
        session.add(
            EventAnalogy(
                event_id=event_id,
                historical_episode_id=episode.id,
                similarity_score=82.0,
                rationale="Similar leverage dynamics",
                regime_caveats=["floating FX now"],
            )
        )
        session.commit()

        analogies = SQLAlchemyBriefContextRepository(session).analogies_for_events([event_id]).rows

    assert len(analogies) == 1
    analogy = analogies[0]
    assert analogy.onset.is_counterexample is True
    assert analogy.onset.parent is not None
    assert analogy.onset.parent.name == "Great Depression"
    assert analogy.outcome.outcomes == ("bailout",)
    assert analogy.episode_id == analogy.onset.episode_id


# --------------------------------------------------------------------------------------
# Forecast load (whole sets, newest first)
# --------------------------------------------------------------------------------------


def test_forecast_scenarios_load_for_selected_events(context_db) -> None:
    with Session(context_db) as session:
        event_id, _ = _seed_common(session)
        set_id = uuid.uuid4()
        probs = {"base_case": 0.4, "upside_case": 0.3, "downside_case": 0.2, "tail_risk_case": 0.1}
        for name, prob in probs.items():
            session.add(
                ForecastScenario(
                    event_id=event_id,
                    scenario_set_id=set_id,
                    scenario_name=name,
                    probability=prob,
                    risk_score=60.0,
                    severity="high",
                    horizon="0_6m",
                    narrative="n",
                )
            )
        session.commit()

        rows = SQLAlchemyBriefContextRepository(session).forecasts_for_events([event_id]).rows

    assert len(rows) == 4
    assert {r.scenario_name for r in rows} == set(probs)
    assert all(r.scenario_set_id == set_id for r in rows)
