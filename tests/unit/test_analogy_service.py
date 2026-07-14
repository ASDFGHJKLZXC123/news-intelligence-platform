"""The Stage 5 pipeline for one event: retrieve, rerank, attach outcomes, persist.

Network-free, but not seam-free: the real retrieval core runs against a recording fake session and
the real orchestrator runs against a scripted provider, so what is exercised here is the actual
wiring between item 2 and item 3 -- including the one thing the wiring exists to guarantee, which
is that the outcomes retrieval loaded reach the *result* without ever reaching the *prompt*.

Retrieval hands over five candidates and their hindsight. The reranker gets the candidates. The
final result gets the hindsight back, but only for the episodes the reranker kept.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from db.models.core import EventAnalogy
from packages.config.settings import Settings
from services.analogies.contracts import (
    NO_RELIABLE_ANALOGY,
    EventEmbeddingMissingError,
    EventNotFoundError,
)
from services.analogies.rerank import (
    RERANK_PROMPT_TEMPLATE_VERSION,
    RERANK_SCHEMA,
    RERANK_SCHEMA_VERSION,
    RegimeCaveatMissingError,
)
from services.analogies.service import AnalogyStatus, generate_event_analogies
from services.llm.adapters import LLMInvocationRequest
from services.llm.cache import InMemoryLLMPromptCache
from services.llm.fake_providers import CallableLLMProvider
from services.llm.orchestrator import LLMOrchestrator, LLMOrchestratorRequest
from services.llm.repository import InMemoryLLMRuntimeRepository

MODEL = "text-embedding-3-small"
VERSION = "current"
EMBEDDING_DIM_VECTOR: tuple[float, ...] = ()

EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
SVB_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTINENTAL_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
LTCM_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")

CURRENT_REGIME = ("post_QE", "post_dodd_frank")

# Every outcome string retrieval will load. None of them may appear in a prompt.
HINDSIGHT = {
    SVB_ID: "SENTINEL_the_bank_failed_and_entered_receivership",
    CONTINENTAL_ID: "SENTINEL_a_federal_bailout_followed",
    LTCM_ID: "SENTINEL_the_fund_was_recapitalized_privately",
}
RESOLUTION = "SENTINEL_RESOLUTION_fdic_systemic_risk_exception"


def _vector() -> tuple[float, ...]:
    from db.models.core import EMBEDDING_DIM

    return (0.1,) * EMBEDDING_DIM


def _ranking_row(episode_id: uuid.UUID, distance: float, **overrides: Any) -> SimpleNamespace:
    row = {
        "id": episode_id,
        "name": f"episode-{episode_id}",
        "episode_type": "banking_stress",
        "onset_date": datetime.date(2023, 3, 8),
        "peak_date": None,
        "end_date": None,
        "onset_summary": "Concentrated uninsured deposits face rapid withdrawals.",
        "onset_indicators": {"uninsured_deposit_pct": 94},
        "geography": "United States",
        "affected_industries": ["banking"],
        "regime_tags": ["post_QE", "post_dodd_frank"],
        "is_counterexample": False,
        "source_refs": {"refs": ["fdic"]},
        "parent_episode_id": None,
        "distance": distance,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _outcome_row(episode_id: uuid.UUID, outcomes: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=episode_id,
        outcome_summary=HINDSIGHT[episode_id],
        outcomes=outcomes,
        resolution_mechanism=RESOLUTION,
    )


class FakeSession:
    """Serves retrieval's three reads and persistence's one, and records every write."""

    def __init__(
        self,
        *,
        ranking: list[Any] | None = None,
        outcomes: list[Any] | None = None,
        parents: list[Any] | None = None,
        existing: list[EventAnalogy] | None = None,
        event: Any = None,
        vector: tuple[float, ...] | None = None,
    ) -> None:
        self.ranking = ranking or []
        self.outcomes = outcomes or []
        self.parents = parents or []
        self.existing = existing or []
        self.event = event
        self.vector = vector
        self.added: list[EventAnalogy] = []
        self.deleted: list[EventAnalogy] = []
        self.flushes = 0
        self.commits = 0

    def execute(self, statement: Any) -> Any:
        keys = set(statement.selected_columns.keys())
        if "distance" in keys:
            return _Rows(self.ranking)
        if "historical_episode_id" in keys:  # select(EventAnalogy) -- the durable set
            return _Rows(self.existing)
        if "outcome_summary" in keys:
            return _Rows(self.outcomes)
        return _Rows(self.parents)

    def scalars(self, _statement: Any) -> Any:
        return _Rows([self.vector] if self.vector is not None else [])

    def get(self, _model: Any, _pk: Any) -> Any:
        return self.event

    def add(self, row: EventAnalogy) -> None:
        self.added.append(row)

    def delete(self, row: EventAnalogy) -> None:
        self.deleted.append(row)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:  # pragma: no cover - the service must never commit
        self.commits += 1
        raise AssertionError("the service must not commit; the worker owns the transaction")


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> list[Any]:
        return self._rows


class FlushingRepository(InMemoryLLMRuntimeRepository):
    """An in-memory repository that stamps an id on save, the way a real flush does.

    Without this the run's id is None until the Session flushes it, and `event_analogies.llm_run_id`
    could never be asserted network-free.
    """

    def save_llm_run(self, run: Any) -> None:
        run.id = RUN_ID
        super().save_llm_run(run)


class Harness:
    """The real orchestrator over a scripted provider, recording every prompt it sends."""

    def __init__(self, decide: Any) -> None:
        self.repository = FlushingRepository()
        self.invocations: list[LLMInvocationRequest] = []
        self._decide = decide
        self.provider = CallableLLMProvider(self._respond)
        self._orchestrator = LLMOrchestrator(
            settings=Settings(),
            repository=self.repository,
            providers_by_tier={"T1": (self.provider,), "T2": (self.provider,)},
            cache=InMemoryLLMPromptCache(),
        )

    def _respond(self, request: LLMInvocationRequest) -> Any:
        self.invocations.append(request)
        return self._decide(request)

    def run(self, request: LLMOrchestratorRequest) -> Any:
        return self._orchestrator.run(request)

    @property
    def prompts(self) -> str:
        return "\n".join(invocation.prompt for invocation in self.invocations)


def _finding(
    episode_id: uuid.UUID,
    *,
    score: float = 80.0,
    caveats: tuple[str, ...] = (),
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


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        id=EVENT_ID,
        title="Regional lender discloses deposit outflows",
        summary="Uninsured depositors withdraw after a securities loss.",
        event_type="banking_stress",
        country="US",
        region="California",
        first_seen_at=datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC),
        severity_score=91.5,
        article_count=47,
        source_count=19,
    )


def _session(
    *,
    distances: dict[uuid.UUID, float] | None = None,
    outcomes: dict[uuid.UUID, list[str]] | None = None,
    existing: list[EventAnalogy] | None = None,
    counterexamples: frozenset[uuid.UUID] = frozenset(),
) -> FakeSession:
    distances = distances or {SVB_ID: 0.1, CONTINENTAL_ID: 0.2, LTCM_ID: 0.3}
    outcomes = outcomes or {
        SVB_ID: ["failure"],
        CONTINENTAL_ID: ["bailout"],
        LTCM_ID: ["contained"],
    }
    return FakeSession(
        event=_event(),
        vector=_vector(),
        ranking=[
            _ranking_row(
                episode_id, distance, is_counterexample=episode_id in counterexamples
            )
            for episode_id, distance in distances.items()
        ],
        outcomes=[
            _outcome_row(episode_id, tags)
            for episode_id, tags in outcomes.items()
            if episode_id in distances
        ],
        existing=existing or [],
    )


def _generate(session: FakeSession, harness: Harness, **kwargs: Any) -> Any:
    return generate_event_analogies(
        session,
        EVENT_ID,
        orchestrator=harness,
        regime_tags=CURRENT_REGIME,
        **kwargs,
    )


def _durable(session: FakeSession) -> dict[uuid.UUID, EventAnalogy]:
    return {row.historical_episode_id: row for row in session.added}


# --- the hindsight barrier -------------------------------------------------------------
def test_the_outcomes_retrieval_loaded_never_reach_the_prompt() -> None:
    """The whole point of the pipeline's ordering, proved end to end with sentinel strings."""
    session = _session()
    harness = Harness(lambda _r: _payload(_finding(SVB_ID)))

    result = _generate(session, harness)

    assert harness.invocations  # an LLM call really did happen
    for sentinel in (*HINDSIGHT.values(), RESOLUTION):
        assert sentinel not in harness.prompts
    # ...and yet the hindsight is right there in the result, attached after the match.
    assert result.matches[0].outcome.outcome_summary == HINDSIGHT[SVB_ID]
    assert result.matches[0].outcome.resolution_mechanism == RESOLUTION


# --- matched ---------------------------------------------------------------------------
def test_a_matched_run_persists_exactly_the_reranked_selection() -> None:
    session = _session()
    harness = Harness(
        lambda _r: _payload(_finding(SVB_ID, score=91.0), _finding(LTCM_ID, score=64.0))
    )

    result = _generate(session, harness)

    assert result.status is AnalogyStatus.MATCHED
    assert [match.episode_id for match in result.matches] == [SVB_ID, LTCM_ID]
    assert set(_durable(session)) == {SVB_ID, LTCM_ID}
    # The dropped vector candidate is not persisted, and never was.
    assert CONTINENTAL_ID not in _durable(session)
    assert (result.considered_count, result.candidate_count) == (3, 3)
    assert result.reconciliation.inserted == 2
    assert session.commits == 0


def test_the_persisted_rows_carry_the_run_id_and_both_scales() -> None:
    session = _session()
    harness = Harness(lambda _r: _payload(_finding(SVB_ID, score=91.0)))

    result = _generate(session, harness)
    row = _durable(session)[SVB_ID]

    assert result.llm_run_id == RUN_ID
    assert row.llm_run_id == RUN_ID
    assert row.similarity_score == 91.0  # 0-100, the LLM's structural score
    assert row.evidence_refs["vector_similarity"] == 0.9  # 0.0-1.0, item 2's cosine
    assert row.rationale == "Same deposit-run mechanism."
    assert row.shared_causes == [
        "episode_type:banking_stress",
        "regime:post_dodd_frank",
        "regime:post_qe",
    ]


def test_the_final_order_survives_into_the_result() -> None:
    session = _session()
    harness = Harness(
        lambda _r: _payload(
            _finding(LTCM_ID, score=70.0),
            _finding(SVB_ID, score=95.0),
            _finding(CONTINENTAL_ID, score=70.0),
        )
    )

    result = _generate(session, harness)

    # 95 first; then the two tied at 70, split by their vector similarity (0.8 vs 0.7).
    assert [match.episode_id for match in result.matches] == [SVB_ID, CONTINENTAL_ID, LTCM_ID]
    assert [match.similarity_score for match in result.matches] == [95.0, 70.0, 70.0]
    assert [match.vector_similarity for match in result.matches] == [0.9, 0.8, 0.7]


# --- the distribution is over the final set, not the dropped candidates -----------------
def test_the_distribution_counts_the_final_set_only() -> None:
    session = _session(counterexamples=frozenset({LTCM_ID}))
    # Retrieval found three (failure, bailout, contained). The rerank keeps two.
    harness = Harness(lambda _r: _payload(_finding(SVB_ID), _finding(LTCM_ID)))

    result = _generate(session, harness)

    assert result.distribution is not None
    assert result.distribution.matched_count == 2  # not the 3 the vector search returned
    tallies = {tally.outcome: tally.count for tally in result.distribution.tallies}
    assert tallies == {"contained": 1, "failure": 1}
    assert "bailout" not in tallies  # the dropped candidate's outcome is not a base rate here
    assert result.distribution.counterexample_count == 1


# --- the three ways "no reliable analogy" happens --------------------------------------
def test_a_retrieval_abstention_calls_no_llm_at_all() -> None:
    session = _session(distances={SVB_ID: 0.9})  # 0.1 similarity, far below 0.60
    harness = Harness(lambda _r: pytest.fail("the LLM must not be called with no candidates"))

    result = _generate(session, harness)

    assert result.status is AnalogyStatus.NO_RELIABLE_ANALOGY
    assert result.message == NO_RELIABLE_ANALOGY
    assert harness.invocations == []
    assert harness.repository.llm_runs == []
    assert result.matches == ()


def test_an_unsupported_event_type_abstains_without_a_vector_search() -> None:
    session = _session()
    session.event = SimpleNamespace(**{**vars(_event()), "event_type": "earthquake"})
    harness = Harness(lambda _r: pytest.fail("no LLM call for an unclassifiable event"))

    result = _generate(session, harness)

    assert result.status is AnalogyStatus.UNSUPPORTED_EVENT_TYPE
    assert NO_RELIABLE_ANALOGY in result.message
    assert harness.invocations == []


def test_an_llm_abstention_is_a_successful_no_match() -> None:
    session = _session()
    harness = Harness(lambda _r: _payload(no_finding_reason="nothing structurally comparable"))

    result = _generate(session, harness)

    assert result.status is AnalogyStatus.NO_RELIABLE_ANALOGY
    assert "nothing structurally comparable" in result.message
    assert result.matches == ()
    assert session.added == []
    assert result.llm_run_id == RUN_ID  # the abstention is still audited


def test_every_no_match_path_clears_the_events_previous_analogies() -> None:
    """A stale analogy served as current is worse than no analogy at all."""
    stale = EventAnalogy(
        event_id=EVENT_ID,
        historical_episode_id=CONTINENTAL_ID,
        similarity_score=88.0,
        rationale="from a previous run",
    )
    session = _session(existing=[stale])
    harness = Harness(lambda _r: _payload(no_finding_reason="nothing comparable"))

    result = _generate(session, harness)

    assert session.deleted == [stale]
    assert result.reconciliation.removed == 1


def test_a_rerun_reconciles_the_durable_set_rather_than_appending_to_it() -> None:
    kept = EventAnalogy(
        event_id=EVENT_ID,
        historical_episode_id=SVB_ID,
        similarity_score=10.0,
        rationale="stale rationale",
    )
    dropped = EventAnalogy(
        event_id=EVENT_ID,
        historical_episode_id=CONTINENTAL_ID,
        similarity_score=50.0,
        rationale="no longer selected",
    )
    session = _session(existing=[kept, dropped])
    harness = Harness(lambda _r: _payload(_finding(SVB_ID, score=93.0), _finding(LTCM_ID)))

    result = _generate(session, harness)

    assert (
        result.reconciliation.inserted,
        result.reconciliation.updated,
        result.reconciliation.removed,
    ) == (1, 1, 1)
    assert kept.similarity_score == 93.0  # updated in place
    assert kept.rationale == "Same deposit-run mechanism."
    assert session.deleted == [dropped]
    assert [row.historical_episode_id for row in session.added] == [LTCM_ID]


# --- failures stay failures ------------------------------------------------------------
def test_a_missing_event_stays_the_typed_failure_item_2_made_it() -> None:
    session = FakeSession(event=None)
    harness = Harness(lambda _r: _payload())

    with pytest.raises(EventNotFoundError):
        _generate(session, harness)


def test_an_unembedded_event_stays_a_precondition_failure_not_a_no_match() -> None:
    session = FakeSession(event=_event(), vector=None)
    harness = Harness(lambda _r: _payload())

    with pytest.raises(EventEmbeddingMissingError):
        _generate(session, harness)

    assert harness.invocations == []


def test_a_missing_regime_caveat_fails_the_run_and_writes_nothing() -> None:
    """The caveat rule is enforced on the way out of the model, before anything is persisted."""
    session = _session(distances={SVB_ID: 0.1})
    session.ranking[0].regime_tags = ["pre_QE"]  # shares none of the current regime's tags
    harness = Harness(lambda _r: _payload(_finding(SVB_ID)))  # ...and no caveat came back

    with pytest.raises(RegimeCaveatMissingError):
        _generate(session, harness)

    assert session.added == []
    assert session.deleted == []
    assert session.commits == 0


def test_the_result_is_serialization_safe_for_a_celery_payload() -> None:
    import json

    session = _session()
    harness = Harness(lambda _r: _payload(_finding(SVB_ID)))

    payload = _generate(session, harness).as_dict()

    # `analogy_status`, never `status`: the job contract owns that word, and a task merges the two.
    assert json.loads(json.dumps(payload))["analogy_status"] == "matched"
    assert "status" not in payload
    assert payload["matched_episode_ids"] == [str(SVB_ID)]
    assert payload["llm_run_id"] == str(RUN_ID)
    assert payload["analogies_inserted"] == 1
