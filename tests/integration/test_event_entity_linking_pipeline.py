"""ADR 0005 entity linking against a real PostgreSQL, on a throwaway database.

Same rule as the identity pipeline test: this writes entity and event rows, so it must never
touch the developer's database. It provisions its own, asserts that it did, and drops it. The
default database is opened only to issue ``CREATE DATABASE``/``DROP DATABASE``, which does not
read or modify anything inside it.

What only a real database can prove, and what is therefore checked here: the composite primary
key on ``event_entities`` really does collapse three mentions of one company into one row; the
``confidence_score`` CHECK constraint really is 0..1; the foreign keys to ``events`` and
``entity_profiles`` really are satisfied by the order the service flushes in; and the whole run
survives a commit and a rerun.

Both stages the ADR puts outside the deterministic core are still injected: the extractor stands
in for ``en_core_web_trf`` (which is a deployment prerequisite, never a test download) and the
adjudicator stands in for the LLM (no network, no key, no token). Everything between them --
alias lookup, redirects, the seven weighted signals, the bands, the persistence -- is real.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from db.base import Base
from db.models import (
    Article,
    EntityAlias,
    EntityProfile,
    EntityRedirect,
    EntityRelationship,
    EntityResolutionRun,
    Event,
    EventArticle,
    EventEntity,
    Job,
    LLMRun,
    Source,
)
from packages.config.settings import get_settings
from services.entities.adjudication import AdjudicationDecision, MentionAdjudication
from services.entities.event_linking import link_event_entities
from services.entities.event_links import (
    ROLE_ASSERTED,
    ROLE_SPECULATIVE,
    risk_eligible_event_links,
)
from services.entities.news_linking import LinkBand
from services.entities.parent_exposure import PROPAGATED_PARENT, event_parent_exposures
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import (
    ArticleMentions,
    ArticleText,
    EntityLabel,
    EntityMention,
    SentenceContext,
)
from services.provider_data.common import normalize_alias, normalize_name

pytestmark = pytest.mark.integration

# Every throwaway database this test creates carries this prefix, so a leaked one is obvious.
DISPOSABLE_DB_PREFIX = "nip_linking_it_"

# Only the tables the linking pipeline touches; the set is closed under its foreign keys.
LINKING_MODELS = (
    Source,
    Article,
    Event,
    EventArticle,
    EntityProfile,
    EntityAlias,
    # The linker follows redirects at resolve time, so the table has to be there even when the
    # store holds none: an absent one is a hard error, not an empty read.
    EntityRedirect,
    EntityRelationship,
    EntityResolutionRun,
    EventEntity,
    Job,
    LLMRun,
)

ACME_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ZETA_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
PARENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
PUBLISHED = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)

# The real ADR weights, applied to the seeded store below:
#   Acme  = ticker .25 + co-mentions .25 + context .20 + url .15 + location .05 = 0.90 -> ACCEPT
#   Zeta  = ticker .25 +                   context .20 +           location .05 = 0.50 -> ADJUDICATE
ACME_SCORE = 0.9
ZETA_SCORE = 0.5
PARENT_EDGE_CONFIDENCE = 0.9


@pytest.fixture
def disposable_db(require_postgres: None):
    """Create a throwaway database on the configured server, and drop it afterwards."""

    configured = make_url(get_settings().database_url)
    name = f"{DISPOSABLE_DB_PREFIX}{uuid.uuid4().hex[:12]}"

    # The whole point of this fixture: never point the pipeline at the developer's database.
    assert name != configured.database
    assert name.startswith(DISPOSABLE_DB_PREFIX)

    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT. The maintenance
    # database is only a connection target here; nothing in it is read or written.
    admin = create_engine(
        configured.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))

        engine = create_engine(configured.set(database=name), future=True)
        try:
            # Built from the same ORM metadata the migrations create their tables from.
            Base.metadata.create_all(engine, tables=[m.__table__ for m in LINKING_MODELS])
            yield sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
                {"n": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def seeded(disposable_db):
    """One event over three articles, and an identity store with a subsidiary and its parent."""

    with disposable_db() as session:
        acme = _profile(ACME_ID, "Acme Corp", ticker="ACME", website="https://acme.example.com")
        zeta = _profile(ZETA_ID, "Zeta Corp", ticker="ZTA")
        parent = _profile(PARENT_ID, "Example Holdings")
        session.add_all([acme, zeta, parent])
        session.flush()

        session.add_all(
            [
                _alias(acme, "Acme Corp", "legal_name"),
                _alias(acme, "Acme Brands", "brand_product"),
                _alias(acme, "Acme Labs", "colloquial"),
                _alias(zeta, "Zeta Corp", "legal_name"),
                EntityRelationship(
                    id=uuid.uuid4(),
                    parent_entity_id=PARENT_ID,
                    child_entity_id=ACME_ID,
                    relationship_type="parent_of",
                    provider="gleif",
                    confidence_score=PARENT_EDGE_CONFIDENCE,
                    valid_from=datetime.date(2020, 1, 1),
                ),
            ]
        )

        source = Source(
            id=uuid.uuid4(),
            name="Example Wire",
            source_type="rss",
            feed_url="https://wire.example.com/feed",
        )
        session.add(source)
        session.flush()

        acme_article = _article(
            source,
            url="https://acme.example.com/press/q2",
            title="Acme reported revenue",
            body="Acme reported revenue for the period. ACME shares rose. "
            "Acme Brands and Acme Labs expanded.",
        )
        zeta_article = _article(
            source,
            url="https://wire.example.com/markets",
            title="Zeta reported revenue",
            body="Zeta reported revenue for the period. ZTA shares were flat.",
        )
        french = _article(
            source,
            url="https://wire.example.com/fr",
            title="Acme en France",
            body="Acme a publie ses resultats.",
            language="fr",
        )
        event = Event(id=EVENT_ID, title="Results season", country="US")
        session.add_all([acme_article, zeta_article, french, event])
        session.flush()
        session.add_all(
            [
                EventArticle(event_id=EVENT_ID, article_id=item.id)
                for item in (acme_article, zeta_article, french)
            ]
        )
        session.commit()
        keys = (str(acme_article.id), str(zeta_article.id))

    return disposable_db, keys


def _profile(
    identifier: uuid.UUID, name: str, *, ticker: str | None = None, website: str | None = None
) -> EntityProfile:
    return EntityProfile(
        id=identifier,
        canonical_name=name,
        normalized_name=normalize_name(name),
        entity_type="company",
        country="US",
        primary_ticker=ticker,
        website=website,
    )


def _alias(profile: EntityProfile, surface: str, alias_type: str) -> EntityAlias:
    return EntityAlias(
        id=uuid.uuid4(),
        entity_id=profile.id,
        alias=surface,
        normalized_alias=normalize_alias(surface),
        alias_type=alias_type,
        source="sec-edgar",
    )


def _article(source: Source, *, url: str, title: str, body: str, language: str = "en") -> Article:
    return Article(
        id=uuid.uuid4(),
        source_id=source.id,
        url=url,
        url_hash=uuid.uuid4().hex,
        title=title,
        body=body,
        language=language,
        published_at=PUBLISHED,
    )


def _mention(
    surface: str,
    *,
    article_key: str,
    sentence: str,
    previous_text: str | None = None,
    next_text: str | None = None,
    start_char: int,
    status: AssertionStatus = AssertionStatus.ASSERTED,
) -> EntityMention:
    return EntityMention(
        article_key=article_key,
        text=surface,
        label=EntityLabel.ORG,
        start_char=start_char,
        end_char=start_char + len(surface),
        sentence=SentenceContext(
            index=0,
            text=sentence,
            start_char=0,
            end_char=len(sentence),
            previous_text=previous_text,
            next_text=next_text,
        ),
        assertion_status=status,
    )


class FakeExtractor:
    """Stands in for ``en_core_web_trf``: the spans it would find in the seeded bodies."""

    def __init__(
        self, keys: tuple[str, str], *, status: AssertionStatus = AssertionStatus.ASSERTED
    ):
        acme_key, zeta_key = keys
        self.calls: list[int] = []
        self.by_key = {
            acme_key: (
                _mention(
                    "Acme",
                    article_key=acme_key,
                    sentence="Acme reported revenue for the period.",
                    next_text="ACME shares rose.",
                    start_char=0,
                    status=status,
                ),
                _mention(
                    "Acme Brands",
                    article_key=acme_key,
                    sentence="Acme Brands and Acme Labs expanded.",
                    previous_text="ACME shares rose.",
                    start_char=60,
                    status=status,
                ),
                _mention(
                    "Acme Labs",
                    article_key=acme_key,
                    sentence="Acme Brands and Acme Labs expanded.",
                    previous_text="ACME shares rose.",
                    start_char=76,
                    status=status,
                ),
            ),
            zeta_key: (
                _mention(
                    "Zeta",
                    article_key=zeta_key,
                    sentence="Zeta reported revenue for the period.",
                    next_text="ZTA shares were flat.",
                    start_char=0,
                    status=status,
                ),
            ),
        }

    def __call__(self, articles: Sequence[ArticleText]) -> tuple[ArticleMentions, ...]:
        self.calls.append(len(articles))
        return tuple(
            ArticleMentions(article_key=item.key, mentions=self.by_key.get(item.key, ()))
            for item in articles
        )


class FakeAdjudicator:
    """Stands in for the LLM: it selects the top candidate, or abstains, without a network call."""

    def __init__(self, decision: AdjudicationDecision = AdjudicationDecision.SELECTED) -> None:
        self.decision = decision
        self.calls: list[str] = []

    def adjudicate(self, mention: EntityMention, result) -> MentionAdjudication:
        self.calls.append(result.surface)
        if self.decision is not AdjudicationDecision.SELECTED:
            return MentionAdjudication(
                target_id=result.target_id, decision=self.decision, reason="test"
            )
        top = result.candidates[0]
        return MentionAdjudication(
            target_id=result.target_id,
            decision=AdjudicationDecision.SELECTED,
            reason="test",
            entity_id=top.entity_id,
            confidence_score=top.score,
            trace_id="trace-it",
        )


def test_the_disposable_database_is_not_the_developer_database(disposable_db) -> None:
    with disposable_db() as session:
        name = session.execute(text("SELECT current_database()")).scalar_one()

    assert name.startswith(DISPOSABLE_DB_PREFIX)
    assert name != make_url(get_settings().database_url).database


def test_an_event_links_end_to_end_through_real_constraints(seeded) -> None:
    factory, keys = seeded
    extractor, adjudicator = FakeExtractor(keys), FakeAdjudicator()

    with factory() as session:
        result = link_event_entities(
            session, EVENT_ID, extractor=extractor, adjudicator=adjudicator
        )
        session.commit()

    # One batched extraction call, over the two English articles only.
    assert extractor.calls == [2]
    assert result.articles_total == 3
    assert result.articles_processed == 2

    # Acme accepted deterministically and cost nothing; only the ambiguous Zeta hit the model.
    assert result.accepted_count == 3
    assert adjudicator.calls == ["Zeta"]
    bands = {outcome.surface: outcome.band for outcome in result.mentions}
    assert bands == {
        "Acme": LinkBand.ACCEPT,
        "Acme Brands": LinkBand.ACCEPT,
        "Acme Labs": LinkBand.ACCEPT,
        "Zeta": LinkBand.ADJUDICATE,
    }

    with factory() as session:
        links = {
            link.entity_profile_id: link
            for link in session.execute(select(EventEntity)).scalars().all()
        }
        runs = session.execute(select(EntityResolutionRun)).scalars().all()

        # Three mentions of Acme are one row: the composite primary key says so, and the service
        # merged onto it instead of letting a second insert fail at commit.
        assert set(links) == {ACME_ID, ZETA_ID}
        assert float(links[ACME_ID].confidence_score) == pytest.approx(ACME_SCORE)
        # The adjudicated link carries the selected candidate's deterministic score.
        assert float(links[ZETA_ID].confidence_score) == pytest.approx(ZETA_SCORE)
        assert {link.role for link in links.values()} == {ROLE_ASSERTED}

        # Every mention is audited, and all four resolved.
        assert len(runs) == 4
        assert all(run.target_type == "news_mention" for run in runs)
        assert {run.matched_entity_id for run in runs} == {ACME_ID, ZETA_ID}


def test_a_rerun_over_the_same_event_is_idempotent(seeded) -> None:
    factory, keys = seeded

    for _ in range(2):
        with factory() as session:
            link_event_entities(
                session, EVENT_ID, extractor=FakeExtractor(keys), adjudicator=FakeAdjudicator()
            )
            session.commit()

    with factory() as session:
        links = session.execute(select(EventEntity)).scalars().all()
        runs = session.execute(select(EntityResolutionRun)).scalars().all()

    assert len(links) == 2
    assert len(runs) == 4
    assert sorted(float(link.confidence_score) for link in links) == [
        pytest.approx(ZETA_SCORE),
        pytest.approx(ACME_SCORE),
    ]


def test_the_parent_exposure_is_derived_at_scoring_time_and_never_persisted(seeded) -> None:
    factory, keys = seeded

    with factory() as session:
        link_event_entities(
            session, EVENT_ID, extractor=FakeExtractor(keys), adjudicator=FakeAdjudicator()
        )
        session.commit()

        exposures = event_parent_exposures(session, EVENT_ID, as_of=PUBLISHED.date())
        persisted = session.execute(select(EventEntity)).scalars().all()

    # The brand stayed linked to the child that owns its alias; the parent is derived, not stored.
    assert sorted(str(link.entity_profile_id) for link in persisted) == sorted(
        [str(ACME_ID), str(ZETA_ID)]
    )
    assert len(exposures) == 1
    exposure = exposures[0]
    assert (exposure.entity_profile_id, exposure.source_entity_id) == (PARENT_ID, ACME_ID)
    assert exposure.exposure_type == PROPAGATED_PARENT
    assert exposure.depth == 1
    assert exposure.confidence_score == pytest.approx(ACME_SCORE * PARENT_EDGE_CONFIDENCE)
    assert exposure.evidence[0].provider == "gleif"


def test_a_speculative_mention_is_persisted_but_is_never_a_risk_input(seeded) -> None:
    """ADR 0005: denied/speculative links are excluded from risk-score inputs -- and from parents."""

    factory, keys = seeded
    extractor = FakeExtractor(keys, status=AssertionStatus.SPECULATIVE)

    with factory() as session:
        link_event_entities(session, EVENT_ID, extractor=extractor, adjudicator=FakeAdjudicator())
        session.commit()

    with factory() as session:
        links = session.execute(select(EventEntity)).scalars().all()
        eligible = risk_eligible_event_links(session, EVENT_ID)
        exposures = event_parent_exposures(session, EVENT_ID, as_of=PUBLISHED.date())

    # Both companies are still linked and still auditable...
    assert len(links) == 2
    assert {link.role for link in links} == {ROLE_SPECULATIVE}
    # ...and neither is a risk input, so neither propagates to a parent.
    assert eligible == ()
    assert exposures == ()


def test_an_abstaining_adjudicator_attaches_nothing_for_the_ambiguous_mention(seeded) -> None:
    factory, keys = seeded
    adjudicator = FakeAdjudicator(AdjudicationDecision.NIL)

    with factory() as session:
        result = link_event_entities(
            session, EVENT_ID, extractor=FakeExtractor(keys), adjudicator=adjudicator
        )
        session.commit()

    assert result.linked_count == 3  # the three Acme mentions; Zeta linked to nothing

    with factory() as session:
        links = session.execute(select(EventEntity)).scalars().all()
        zeta_run = session.execute(
            select(EntityResolutionRun).where(EntityResolutionRun.input_names.contains(["Zeta"]))
        ).scalar_one()

    assert [link.entity_profile_id for link in links] == [ACME_ID]
    # Unmatched, so the surface stays in the weekly review queue rather than being reported linked.
    assert zeta_run.matched_entity_id is None
    assert float(zeta_run.confidence_score) == pytest.approx(ZETA_SCORE)


def test_the_confidence_check_constraint_is_real(seeded) -> None:
    factory, _keys = seeded

    with factory() as session:
        session.add(
            EventEntity(
                event_id=EVENT_ID,
                entity_profile_id=ACME_ID,
                role=ROLE_ASSERTED,
                confidence_score=1.5,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_a_link_to_an_entity_that_does_not_exist_is_rejected_by_the_foreign_key(seeded) -> None:
    factory, _keys = seeded

    with factory() as session:
        session.add(
            EventEntity(
                event_id=EVENT_ID,
                entity_profile_id=uuid.uuid4(),
                role=ROLE_ASSERTED,
                confidence_score=0.9,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_the_pipeline_mints_no_identity_row_of_its_own(seeded) -> None:
    factory, keys = seeded

    with factory() as session:
        link_event_entities(
            session, EVENT_ID, extractor=FakeExtractor(keys), adjudicator=FakeAdjudicator()
        )
        session.commit()

    with factory() as session:
        # Linking chooses among the entities identity ingestion seeded; it never creates one.
        assert len(session.execute(select(EntityProfile)).scalars().all()) == 3
        assert len(session.execute(select(EntityAlias)).scalars().all()) == 4
        # The injected adjudicator never reaches the orchestrator, so no LLM run was written.
        assert session.execute(select(LLMRun)).scalars().all() == []
