"""Historical-episode retrieval against a real pgvector, on a throwaway database.

The unit suite proves what the *statements* say. Only PostgreSQL can prove what they do: that
``<=>`` really is cosine distance on the stored vectors, that the correlated NOT EXISTS really
does keep a parent arc out of the ranking even when the parent is the nearest vector of all, that
``unnest``-based industry overlap really matches case-insensitively, and that the model-space
predicate really does hide an episode embedded by another model.

The corpus is built so that every hard filter has something to catch, and each excluded episode is
*more* similar to the current event than the ones that should win -- an exclusion that silently
stopped working would immediately change the answer rather than merely failing to shrink it.

Vectors are exact by construction: the event's vector is e0, and an episode at cosine similarity
s is ``s*e0 + sqrt(1-s^2)*e1``, so the similarity the service reports is the similarity seeded.

This test never touches the developer's database. It provisions its own ``nip_analogy_it_*``,
asserts that it did, and drops it in a finally block.
"""

from __future__ import annotations

import datetime
import math
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from db.base import Base
from db.models.core import EMBEDDING_DIM, Event, EventEmbedding, HistoricalEpisode
from packages.config.settings import get_settings
from services.analogies import (
    RetrievalStatus,
    retrieve_analogies_for_event,
)

pytestmark = pytest.mark.integration

# Every throwaway database this test creates carries this prefix, so a leaked one is obvious.
DISPOSABLE_DB_PREFIX = "nip_analogy_it_"

# The closed set of tables retrieval reads. `historical_episodes` self-references for nesting.
ANALOGY_MODELS = (Event, EventEmbedding, HistoricalEpisode)

MODEL = "text-embedding-3-small"
VERSION = "current"
CURRENT_REGIME = ("post_QE", "post_dodd_frank")

EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
PARENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
CHILD_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
STANDALONE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
WRONG_FAMILY_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
WRONG_MODEL_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
DISTANT_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")


def _vector(similarity: float) -> list[float]:
    """A unit vector whose cosine similarity to the event's vector is exactly `similarity`."""
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    vector[1] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


EVENT_VECTOR = _vector(1.0)


@pytest.fixture
def disposable_db(require_postgres: None):
    """Create a throwaway database on the configured server, and drop it afterwards."""
    configured = make_url(get_settings().database_url)
    name = f"{DISPOSABLE_DB_PREFIX}{uuid.uuid4().hex[:12]}"

    # The whole point of this fixture: never point the retrieval core at a shared database.
    assert name != configured.database
    assert name.startswith(DISPOSABLE_DB_PREFIX)

    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT. The maintenance
    # database is only a connection target; nothing in it is read or written.
    admin = create_engine(
        configured.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))

        engine = create_engine(configured.set(database=name), future=True)
        try:
            with engine.begin() as connection:
                # The HNSW cosine index on the onset embedding needs the extension in place.
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            Base.metadata.create_all(engine, tables=[m.__table__ for m in ANALOGY_MODELS])
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


def _episode(
    episode_id: uuid.UUID,
    *,
    similarity: float,
    episode_type: str = "banking_stress",
    model: str = MODEL,
    **fields: object,
) -> HistoricalEpisode:
    defaults: dict[str, object] = {
        "name": f"episode {episode_id}",
        "onset_date": datetime.date(2023, 3, 8),
        "onset_summary": "A lender with concentrated uninsured deposits faces withdrawals.",
        "onset_indicators": {"uninsured_deposit_pct": 94},
        "geography": "United States",
        "affected_industries": ["banking"],
        "regime_tags": list(CURRENT_REGIME),
        "is_counterexample": False,
    }
    defaults.update(fields)
    return HistoricalEpisode(
        id=episode_id,
        episode_type=episode_type,
        onset_embedding=_vector(similarity),
        model=model,
        model_version=VERSION,
        **defaults,
    )


@pytest.fixture
def seeded(disposable_db):
    """One embedded event, and a corpus where every hard filter has a nearer episode to exclude."""
    with disposable_db() as session:
        session.add(
            Event(
                id=EVENT_ID, title="Regional lender halts withdrawals", event_type="banking_stress"
            )
        )
        session.flush()
        session.add(
            EventEmbedding(
                event_id=EVENT_ID,
                model=MODEL,
                model_version=VERSION,
                dimension=EMBEDDING_DIM,
                embedding=EVENT_VECTOR,
            )
        )
        session.add_all(
            [
                # The parent arc: the nearest vector in the corpus, and never ranked.
                _episode(
                    PARENT_ID,
                    similarity=0.99,
                    name="2023 regional banking stress",
                    onset_summary="Deposit flight spreads across mid-sized US lenders.",
                    outcome_summary="Three lenders failed; the arc was contained by March.",
                    outcomes=["systemic_crisis"],
                    resolution_mechanism="Systemic risk exception",
                ),
                # A child of that arc: the top-ranked match, in the current regime.
                _episode(
                    CHILD_ID,
                    similarity=0.95,
                    name="Silicon Valley Bank",
                    parent_episode_id=PARENT_ID,
                    outcome_summary="The bank failed and was placed into receivership.",
                    outcomes=["failure", "bailout"],
                    resolution_mechanism="FDIC systemic risk exception",
                ),
                # A standalone counterexample from another regime, and another geography.
                _episode(
                    STANDALONE_ID,
                    similarity=0.80,
                    name="1866 Overend Gurney",
                    geography="United Kingdom",
                    affected_industries=["banking", "Insurance"],
                    regime_tags=["pre_fiat"],
                    is_counterexample=True,
                    outcome_summary="The panic subsided without a systemic collapse.",
                    outcomes=["contained"],
                    resolution_mechanism="Lender of last resort",
                ),
                # Nearer than either match, and excluded: wrong family, wrong model space, and
                # (for the last) simply too far away once similarity is measured.
                _episode(WRONG_FAMILY_ID, similarity=0.99, episode_type="pandemic"),
                _episode(WRONG_MODEL_ID, similarity=0.99, model="text-embedding-3-large"),
                _episode(DISTANT_ID, similarity=0.30, outcomes=["recovery"]),
            ]
        )
        session.commit()

    return disposable_db


def test_retrieval_ranks_only_leaf_episodes_of_the_same_family_and_model_space(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(session, EVENT_ID, regime_tags=CURRENT_REGIME)

    assert result.status is RetrievalStatus.MATCHED
    matched = [match.candidate.episode_id for match in result.matches]

    # Ranked by real cosine distance: the child (0.95) then the standalone (0.80).
    assert matched == [CHILD_ID, STANDALONE_ID]
    # The parent is the nearest vector in the corpus and is still not ranked; the other three are
    # excluded by family, by model space, and by the threshold.
    assert PARENT_ID not in matched
    assert WRONG_FAMILY_ID not in matched
    assert WRONG_MODEL_ID not in matched
    assert DISTANT_ID not in matched

    similarities = [match.candidate.similarity for match in result.matches]
    assert similarities == pytest.approx([0.95, 0.80], abs=1e-5)


def test_outcomes_and_the_parent_arc_are_attached_only_after_matching(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(session, EVENT_ID, regime_tags=CURRENT_REGIME)

    child, standalone = result.matches
    assert child.outcome.outcomes == ("failure", "bailout")
    assert child.outcome.resolution_mechanism == "FDIC systemic risk exception"
    assert standalone.outcome.outcomes == ("contained",)

    # The parent comes back as onset-only context for the child that matched.
    assert child.parent is not None
    assert child.parent.episode_id == PARENT_ID
    assert child.parent.onset_summary == "Deposit flight spreads across mid-sized US lenders."
    assert standalone.parent is None


def test_the_regime_gate_flags_rather_than_drops_an_out_of_regime_episode(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(session, EVENT_ID, regime_tags=CURRENT_REGIME)

    child, standalone = result.matches
    assert child.candidate.regime_caveats_required is False
    assert standalone.candidate.regime_caveats_required is True
    assert standalone.candidate.regime_caveat_reasons


def test_conflicting_outcomes_come_back_as_a_distribution_over_the_matched_episodes(
    seeded,
) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(session, EVENT_ID, regime_tags=CURRENT_REGIME)

    distribution = result.distribution
    assert distribution is not None
    assert distribution.matched_count == 2
    # All three tags tie at one occurrence, so the order is the canonical `EPISODE_OUTCOMES` one
    # (contained, ..., bailout, failure) that `summarize_outcomes` tie-breaks on -- deliberately not
    # the order the tags happen to sit in inside the episode's `outcomes` array. A tally order that
    # depended on array insertion would reshuffle itself as the corpus was re-curated.
    assert [(t.outcome, t.count, t.rate) for t in distribution.tallies] == [
        ("contained", 1, 0.5),
        ("bailout", 1, 0.5),
        ("failure", 1, 0.5),
    ]
    assert (distribution.counterexample_count, distribution.counterexample_rate) == (1, 0.5)
    assert distribution.untagged_count == 0


def test_the_geography_filter_matches_case_insensitively(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(
            session, EVENT_ID, regime_tags=CURRENT_REGIME, geographies=["united states"]
        )

    assert [match.candidate.episode_id for match in result.matches] == [CHILD_ID]


def test_the_industry_filter_overlaps_the_array_case_insensitively(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(
            session, EVENT_ID, regime_tags=CURRENT_REGIME, industries=["INSURANCE"]
        )

    # Only the standalone lists insurance, even though the child is the nearer vector.
    assert [match.candidate.episode_id for match in result.matches] == [STANDALONE_ID]


def test_nothing_over_the_threshold_is_the_allowed_no_reliable_analogy_answer(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(
            session, EVENT_ID, regime_tags=CURRENT_REGIME, min_similarity=0.98
        )

    assert result.status is RetrievalStatus.NO_RELIABLE_ANALOGY
    assert result.message == "no reliable analogy"
    assert result.matches == ()
    assert result.distribution is None
    assert result.best_similarity == pytest.approx(0.95, abs=1e-5)


def test_k_bounds_the_neighbourhood(seeded) -> None:
    with seeded() as session:
        result = retrieve_analogies_for_event(
            session, EVENT_ID, regime_tags=CURRENT_REGIME, top_k=1
        )

    assert [match.candidate.episode_id for match in result.matches] == [CHILD_ID]
