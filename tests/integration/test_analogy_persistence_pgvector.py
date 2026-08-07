"""The durable analogy set against a real PostgreSQL, on a throwaway database.

The unit suite proves what the reconciliation *decides*. Only a real database can prove what it
*does*, and four of item 3's guarantees are exactly that kind of claim:

* the unique ``(event_id, historical_episode_id)`` constraint is respected by a rerun -- a fake
  session cannot violate a constraint it does not have;
* ``event_analogies.llm_run_id`` really resolves to the ``llm_runs`` row the orchestrator wrote in
  the same transaction. The id only exists because the shared session flushed it, so this is the
  one place ``commit_on_write=False`` can be shown to pay for itself;
* a failure rolls back to the *previously committed* set rather than to an event with half its
  analogies replaced;
* the 0-100 ``similarity_score`` bound is enforced by the table, not merely by our own check.

This test never touches the developer's database. It provisions its own ``nip_analogy_p_*``,
asserts that it did, and drops it in a finally block.
"""

from __future__ import annotations

import datetime
import math
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from apps.api.analogies import AnalogyReadRepository
from db.base import Base
from db.models.core import (
    EMBEDDING_DIM,
    Event,
    EventAnalogy,
    EventEmbedding,
    HistoricalEpisode,
    HistoricalEpisodeEmbedding,
    Job,
    LLMRun,
)
from packages.config.settings import Settings, get_settings
from services.analogies.rerank import (
    RERANK_PROMPT_TEMPLATE_VERSION,
    RERANK_SCHEMA,
    RERANK_SCHEMA_VERSION,
    RegimeCaveatMissingError,
)
from services.analogies.service import AnalogyStatus, generate_event_analogies
from services.llm.cache import InMemoryLLMPromptCache
from services.llm.fake_providers import CallableLLMProvider
from services.llm.orchestrator import LLMOrchestrator
from services.llm.repository import SQLAlchemyLLMRuntimeRepository
from services.nlp.episodes import (
    EPISODE_EMBEDDING_INPUT_CONTRACT_VERSION,
    episode_embedding_input_sha256,
)

pytestmark = pytest.mark.integration

# Every throwaway database this test creates carries this prefix, so a leaked one is obvious.
DISPOSABLE_DB_PREFIX = "nip_analogy_p_"

# The closed set of tables this pipeline writes: the corpus it reads, the analogies it reconciles,
# and the audit rows the orchestrator persists into the same transaction.
ANALOGY_MODELS = (
    Event,
    EventEmbedding,
    HistoricalEpisode,
    HistoricalEpisodeEmbedding,
    EventAnalogy,
    LLMRun,
    Job,
)

MODEL = "text-embedding-3-small"
VERSION = "current"
CURRENT_REGIME = ("post_QE", "post_dodd_frank")

EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
SVB_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTINENTAL_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
LTCM_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _vector(similarity: float) -> list[float]:
    """A unit vector whose cosine similarity to the event's vector is exactly `similarity`."""
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    vector[1] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


@pytest.fixture
def disposable_db(require_postgres: None):
    """Create a throwaway database on the configured server, and drop it afterwards."""
    configured = make_url(get_settings().database_url)
    name = f"{DISPOSABLE_DB_PREFIX}{uuid.uuid4().hex[:12]}"

    # The whole point of this fixture: never point the pipeline at a shared database.
    assert name != configured.database
    assert name.startswith(DISPOSABLE_DB_PREFIX)

    admin = create_engine(
        configured.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))

        engine = create_engine(configured.set(database=name), future=True)
        try:
            with engine.begin() as connection:
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
    episode_id: uuid.UUID, *, similarity: float, regime_tags: list[str]
) -> HistoricalEpisode:
    return HistoricalEpisode(
        id=episode_id,
        name=f"episode {episode_id}",
        episode_type="banking_stress",
        onset_date=datetime.date(2023, 3, 8),
        onset_summary="A lender with concentrated uninsured deposits faces withdrawals.",
        onset_indicators={"uninsured_deposit_pct": 94},
        onset_embedding=_vector(similarity),
        model=MODEL,
        model_version=VERSION,
        version=1,
        outcome_summary="The bank failed and was placed into receivership.",
        outcomes=["failure"],
        resolution_mechanism="FDIC systemic risk exception",
        geography="United States",
        affected_industries=["banking"],
        regime_tags=regime_tags,
        is_counterexample=False,
    )


def _episode_embedding(episode: HistoricalEpisode) -> HistoricalEpisodeEmbedding:
    return HistoricalEpisodeEmbedding(
        historical_episode_id=episode.id,
        model=episode.model,
        model_version=episode.model_version,
        episode_version=episode.version,
        dimension=EMBEDDING_DIM,
        onset_embedding=episode.onset_embedding,
        input_sha256=episode_embedding_input_sha256(episode),
        input_contract_version=EPISODE_EMBEDDING_INPUT_CONTRACT_VERSION,
        snapshot_manifest_sha256=None,
    )


@pytest.fixture
def seeded(disposable_db):
    """One embedded event and three in-family episodes, all comfortably over the 0.60 threshold."""
    with disposable_db() as session:
        session.add(
            Event(
                id=EVENT_ID,
                title="Regional lender discloses deposit outflows",
                summary="Uninsured depositors withdraw after a securities loss.",
                event_type="banking_stress",
                country="US",
            )
        )
        session.flush()
        session.add(
            EventEmbedding(
                event_id=EVENT_ID,
                model=MODEL,
                model_version=VERSION,
                dimension=EMBEDDING_DIM,
                embedding=_vector(1.0),
            )
        )
        episodes = (
            _episode(SVB_ID, similarity=0.95, regime_tags=list(CURRENT_REGIME)),
            _episode(CONTINENTAL_ID, similarity=0.90, regime_tags=list(CURRENT_REGIME)),
            # Shares none of the current regime's tags: item 2 flags it, item 3 must caveat it.
            _episode(LTCM_ID, similarity=0.85, regime_tags=["pre_QE"]),
        )
        session.add_all(episodes)
        session.flush()
        session.add_all([_episode_embedding(episode) for episode in episodes])
        session.commit()
    return disposable_db


def _finding(
    episode_id: uuid.UUID, *, score: float = 80.0, caveats: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "historical_episode_id": str(episode_id),
        "explanation": "Same deposit-run mechanism.",
        "regime_caveats": list(caveats),
        "similarity_score": score,
        "confidence": 0.7,
    }


def _payload(*findings: dict[str, Any], no_finding_reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": RERANK_SCHEMA,
        "schema_version": RERANK_SCHEMA_VERSION,
        "prompt_template_version": RERANK_PROMPT_TEMPLATE_VERSION,
        "analogies": list(findings),
    }
    if no_finding_reason is not None:
        payload["no_finding_reason"] = no_finding_reason
    return payload


def _orchestrator(session: Any, decide: Any) -> LLMOrchestrator:
    """The production wiring, minus the network: the repository shares the caller's transaction."""
    provider = CallableLLMProvider(lambda request: decide(request))
    return LLMOrchestrator(
        settings=Settings(),
        repository=SQLAlchemyLLMRuntimeRepository(session, commit_on_write=False),
        providers_by_tier={"T1": (provider,), "T2": (provider,)},
        cache=InMemoryLLMPromptCache(),
    )


def _run(session: Any, decide: Any) -> Any:
    return generate_event_analogies(
        session,
        EVENT_ID,
        orchestrator=_orchestrator(session, decide),
        regime_tags=CURRENT_REGIME,
    )


def _durable(session: Any) -> dict[uuid.UUID, EventAnalogy]:
    rows = session.execute(select(EventAnalogy).where(EventAnalogy.event_id == EVENT_ID)).scalars()
    return {row.historical_episode_id: row for row in rows}


def test_a_run_persists_the_selection_and_its_llm_run_in_one_transaction(seeded) -> None:
    with seeded() as session:
        result = _run(session, lambda _r: _payload(_finding(SVB_ID, score=91.0)))
        session.commit()

    assert result.status is AnalogyStatus.MATCHED

    with seeded() as session:
        rows = _durable(session)
        assert set(rows) == {SVB_ID}
        row = rows[SVB_ID]
        assert float(row.similarity_score) == 91.0
        assert row.evidence_refs["vector_similarity"] == 0.95
        assert row.rationale == "Same deposit-run mechanism."

        # The FK really resolves: the id exists only because the shared session flushed the run.
        assert row.llm_run_id is not None
        run = session.get(LLMRun, row.llm_run_id)
        assert run is not None
        assert run.output_schema_name == RERANK_SCHEMA
        assert run.status == "succeeded"


def test_a_rerun_reconciles_the_set_without_violating_the_unique_pair(seeded) -> None:
    with seeded() as session:
        _run(session, lambda _r: _payload(_finding(SVB_ID, score=70.0), _finding(CONTINENTAL_ID)))
        session.commit()

    with seeded() as session:
        # SVB is kept (with a new score), Continental is dropped, LTCM is new -- and LTCM is the
        # out-of-regime episode, so it may only be selected with an explicit caveat.
        result = _run(
            session,
            lambda _r: _payload(
                _finding(SVB_ID, score=93.0),
                _finding(LTCM_ID, score=64.0, caveats=("Pre-QE: no central-bank backstop.",)),
            ),
        )
        session.commit()

    assert (result.reconciliation.inserted, result.reconciliation.updated) == (1, 1)
    assert result.reconciliation.removed == 1

    with seeded() as session:
        rows = _durable(session)
        assert set(rows) == {SVB_ID, LTCM_ID}
        assert float(rows[SVB_ID].similarity_score) == 93.0
        assert rows[LTCM_ID].regime_caveats == ["Pre-QE: no central-bank backstop."]
        # Item 2's deterministic reason rode along on its own column, separate from the model's.
        assert rows[LTCM_ID].limitations


def test_a_no_match_rerun_clears_the_previously_durable_set(seeded) -> None:
    with seeded() as session:
        _run(session, lambda _r: _payload(_finding(SVB_ID)))
        session.commit()

    with seeded() as session:
        result = _run(session, lambda _r: _payload(no_finding_reason="nothing comparable"))
        session.commit()

    assert result.status is AnalogyStatus.NO_RELIABLE_ANALOGY

    with seeded() as session:
        assert _durable(session) == {}  # nothing stale is left to be served as current


def test_a_failed_rerun_rolls_back_to_the_previously_committed_set(seeded) -> None:
    """The guarantee the worker's rollback exists for, and the one a fake session cannot prove."""
    with seeded() as session:
        _run(session, lambda _r: _payload(_finding(SVB_ID, score=88.0)))
        session.commit()

    with seeded() as session:
        # The model returns the out-of-regime episode with no caveat: a typed failure, raised after
        # the reconciliation of an earlier, successful-looking part of the run would have started.
        with pytest.raises(RegimeCaveatMissingError):
            _run(session, lambda _r: _payload(_finding(SVB_ID, score=10.0), _finding(LTCM_ID)))
        session.rollback()

    with seeded() as session:
        rows = _durable(session)
        assert set(rows) == {SVB_ID}
        assert float(rows[SVB_ID].similarity_score) == 88.0  # the old score, not the failed run's

        # The failed rerun's audit row rolled back with it -- it shared the one transaction -- so
        # the only LLMRun left is the committed first run, the one the surviving analogy cites.
        runs = session.execute(select(LLMRun)).scalars().all()
        assert len(runs) == 1
        assert runs[0].id == rows[SVB_ID].llm_run_id


def test_the_table_enforces_the_zero_to_one_hundred_score_bound(seeded) -> None:
    """Defence in depth: our own check raises first, but the column agrees."""
    with seeded() as session, pytest.raises(IntegrityError):
        session.add(
            EventAnalogy(
                event_id=EVENT_ID,
                historical_episode_id=SVB_ID,
                similarity_score=101.0,
                rationale="off the contract scale",
            )
        )
        session.flush()


def test_the_table_enforces_one_row_per_event_and_episode(seeded) -> None:
    with seeded() as session, pytest.raises(IntegrityError):
        for _ in range(2):
            session.add(
                EventAnalogy(
                    event_id=EVENT_ID,
                    historical_episode_id=SVB_ID,
                    similarity_score=80.0,
                    rationale="duplicate pair",
                )
            )
        session.flush()


# --- the read API's ordering, against the database that actually applies the LIMIT ------------
def test_the_read_api_limit_cuts_after_the_full_tie_break_not_before_it(seeded) -> None:
    """The bug a fake repository cannot show: LIMIT is applied to the order the *database* produced.

    Two analogies tie at 88.0. One has a 0.95 vector prior and the larger episode id; the other has
    0.61 and the smaller. Ordering by score and id alone, `limit=1` returns the 0.61 row -- and no
    amount of re-sorting in Python can recover the better row, because it was never fetched. The
    vector prior lives in a JSONB payload, so the tie-break has to reach into it *in SQL*.

    This also proves the JSONB cast executes: a wrong one compiles perfectly and fails at runtime.
    """
    # The better vector prior deliberately sits on the *larger* id, so an id-ascending tie-break
    # would rank it last and `limit=1` would drop precisely the row that should have won.
    larger_id, smaller_id = max(SVB_ID, CONTINENTAL_ID), min(SVB_ID, CONTINENTAL_ID)

    with seeded() as session:
        for episode_id, vector_similarity in ((larger_id, 0.95), (smaller_id, 0.61)):
            session.add(
                EventAnalogy(
                    event_id=EVENT_ID,
                    historical_episode_id=episode_id,
                    similarity_score=88.0,
                    rationale="tied on the structural score",
                    evidence_refs={"vector_similarity": vector_similarity},
                )
            )
        session.flush()

        rows = AnalogyReadRepository(session).list_analogies(EVENT_ID, limit=1)

        assert [episode.id for _analogy, episode in rows] == [larger_id]


def test_the_read_api_treats_a_missing_vector_similarity_as_zero_in_sql(seeded) -> None:
    """A row with no stored prior sorts last among its ties, exactly as `_vector_similarity` does."""
    with seeded() as session:
        session.add(
            EventAnalogy(
                event_id=EVENT_ID,
                historical_episode_id=SVB_ID,
                similarity_score=88.0,
                rationale="no stored vector prior",
                evidence_refs={},
            )
        )
        session.add(
            EventAnalogy(
                event_id=EVENT_ID,
                historical_episode_id=CONTINENTAL_ID,
                similarity_score=88.0,
                rationale="has one",
                evidence_refs={"vector_similarity": 0.5},
            )
        )
        session.flush()

        rows = AnalogyReadRepository(session).list_analogies(EVENT_ID, limit=2)

        assert [episode.id for _analogy, episode in rows] == [CONTINENTAL_ID, SVB_ID]
