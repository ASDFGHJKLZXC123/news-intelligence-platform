"""The retrieval core, against a recording fake session (no database, no network).

The load-bearing assertions are the ones about the *statements*. The spec's look-ahead-bias rule
is a claim about SQL -- that no outcome column is fetched, predicated on, or ordered by while
episodes are being ranked -- so these tests compile the statement the service actually executed
and read it. A test that only checked the returned objects would still pass if the ranking query
selected ``outcome_summary`` and the mapper happened to drop it.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from db.models.core import EMBEDDING_DIM
from services.analogies.contracts import (
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_TOP_K,
    MAX_FILTER_VALUES,
    MAX_TOP_K,
    NO_RELIABLE_ANALOGY,
    AnalogyQuery,
    AnalogyRequestError,
    EpisodeCandidate,
    EventEmbeddingMissingError,
    EventNotFoundError,
    RetrievalStatus,
)
from services.analogies.retrieval import (
    retrieve_analogies_for_event,
    retrieve_analogy_candidates,
)

MODEL = "text-embedding-3-small"
VERSION = "current"
VECTOR = (0.1,) * EMBEDDING_DIM
ONSET = datetime.date(2023, 3, 8)
EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
OUTCOME_COLUMNS = ("outcome_summary", "outcomes", "resolution_mechanism")


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Records every statement, and answers each one by the columns it selects."""

    def __init__(
        self,
        *,
        ranking: list[Any] | None = None,
        outcomes: list[Any] | None = None,
        parents: list[Any] | None = None,
        event: Any = None,
        vector: tuple[float, ...] | None = None,
    ) -> None:
        self.ranking = ranking or []
        self.outcomes = outcomes or []
        self.parents = parents or []
        self.event = event
        self.vector = vector
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        keys = set(statement.selected_columns.keys())
        if "distance" in keys:
            return FakeResult(self.ranking)
        if "outcome_summary" in keys:
            return FakeResult(self.outcomes)
        return FakeResult(self.parents)

    def scalars(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult([self.vector] if self.vector is not None else [])

    def get(self, _model: Any, _pk: Any) -> Any:
        return self.event


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _row(distance: float, **overrides: Any) -> SimpleNamespace:
    row = {
        "id": uuid.uuid4(),
        "name": "2023 regional banking stress",
        "episode_type": "banking_stress",
        "onset_date": ONSET,
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


def _outcome_row(episode_id: uuid.UUID, **overrides: Any) -> SimpleNamespace:
    row = {
        "id": episode_id,
        "outcome_summary": "The bank failed and was placed into receivership.",
        "outcomes": ["failure", "bailout"],
        "resolution_mechanism": "FDIC systemic risk exception",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _query(**overrides: Any) -> AnalogyQuery:
    fields: dict[str, Any] = {
        "vector": VECTOR,
        "episode_types": frozenset({"banking_stress"}),
        "model": MODEL,
        "model_version": VERSION,
    }
    fields.update(overrides)
    return AnalogyQuery(**fields)


# --- defaults and validation ----------------------------------------------------------
def test_the_spec_defaults_are_k_five_and_zero_point_six() -> None:
    assert (DEFAULT_TOP_K, DEFAULT_MIN_SIMILARITY) == (5, 0.60)
    query = _query()
    assert (query.top_k, query.min_similarity) == (5, 0.60)


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, 1.5, True])
def test_k_must_be_an_integer_inside_a_finite_cap(top_k: Any) -> None:
    with pytest.raises(AnalogyRequestError, match="top_k"):
        _query(top_k=top_k)


@pytest.mark.parametrize("top_k", [1, DEFAULT_TOP_K, MAX_TOP_K])
def test_k_at_the_bounds_is_accepted(top_k: int) -> None:
    assert _query(top_k=top_k).top_k == top_k


@pytest.mark.parametrize("threshold", [-0.01, -1.0, 1.01, 2.0])
def test_the_threshold_is_bounded_to_a_usable_cosine_range(threshold: float) -> None:
    with pytest.raises(AnalogyRequestError, match="min_similarity"):
        _query(min_similarity=threshold)


@pytest.mark.parametrize("threshold", [0.0, 0.6, 1.0])
def test_the_threshold_bounds_themselves_are_accepted(threshold: float) -> None:
    assert _query(min_similarity=threshold).min_similarity == threshold


def test_a_vector_of_the_wrong_width_is_refused() -> None:
    with pytest.raises(AnalogyRequestError, match="EMBEDDING_DIM"):
        _query(vector=(0.1,) * 8)


def test_a_zero_vector_is_refused_because_cosine_would_tie_everything() -> None:
    with pytest.raises(AnalogyRequestError, match="all zeros"):
        _query(vector=(0.0,) * EMBEDDING_DIM)


def test_an_unknown_episode_type_raises_rather_than_searching_all_history() -> None:
    with pytest.raises(AnalogyRequestError, match="unknown episode types: earthquake"):
        _query(episode_types=frozenset({"banking_stress", "earthquake"}))


def test_a_query_with_no_episode_type_is_refused() -> None:
    with pytest.raises(AnalogyRequestError, match="never searches all history"):
        _query(episode_types=frozenset())


def test_episode_types_are_canonicalized_on_the_way_in() -> None:
    assert _query(episode_types=frozenset({"Supply-Chain"})).episode_types == frozenset(
        {"supply_shock"}
    )


@pytest.mark.parametrize("field", ["geographies", "industries"])
def test_a_supplied_but_empty_filter_is_a_bug_not_a_match_everything(field: str) -> None:
    with pytest.raises(AnalogyRequestError, match="supplied but empty"):
        _query(**{field: frozenset()})


@pytest.mark.parametrize("field", ["geographies", "industries"])
def test_optional_filters_are_bounded(field: str) -> None:
    values = frozenset(f"value-{index}" for index in range(MAX_FILTER_VALUES + 1))

    with pytest.raises(AnalogyRequestError, match="the cap is"):
        _query(**{field: values})


@pytest.mark.parametrize("field", ["geographies", "industries"])
def test_omitting_a_filter_leaves_it_unfiltered(field: str) -> None:
    assert getattr(_query(), field) is None


def test_a_query_must_pin_one_model_space() -> None:
    with pytest.raises(AnalogyRequestError, match="one embedding model space"):
        _query(model_version="")


# --- the ranking statement ------------------------------------------------------------
def _ranking_sql(query: AnalogyQuery) -> str:
    session = FakeSession()
    retrieve_analogy_candidates(session, query)
    return _sql(session.statements[0])


def test_the_ranking_statement_cannot_see_any_outcome_column() -> None:
    """The look-ahead-bias rule, as a property of the SQL: outcome text cannot rank anything."""
    sql = _ranking_sql(_query())

    for column in OUTCOME_COLUMNS:
        assert column not in sql
    assert "onset_summary" in sql and "onset_indicators" in sql


def test_ranking_orders_by_pgvector_cosine_distance_with_a_deterministic_tie_break() -> None:
    sql = _ranking_sql(_query())

    assert (
        "historical_episode_embeddings.onset_embedding <=> " in sql
    )  # cosine distance operator, no manual normalization
    assert (
        "ORDER BY distance ASC, historical_episodes.onset_date ASC, historical_episodes.id ASC"
        in sql
    )
    assert "LIMIT 5" in sql


def test_hard_filters_run_before_the_nearest_neighbour_ordering() -> None:
    sql = _ranking_sql(_query())
    where, _, order_by = sql.partition("ORDER BY")

    assert "historical_episodes.episode_type IN ('banking_stress')" in where
    assert f"historical_episode_embeddings.model = '{MODEL}'" in where
    assert f"historical_episode_embeddings.model_version = '{VERSION}'" in where
    assert "historical_episode_embeddings.episode_version = historical_episodes.version" in where
    assert "historical_episode_embeddings.historical_episode_id = historical_episodes.id" in where
    assert f"historical_episodes.model = '{MODEL}'" not in where
    assert "distance" in order_by


def test_parents_are_excluded_from_ranking_while_children_and_standalones_stay() -> None:
    sql = _ranking_sql(_query())

    assert "NOT (EXISTS (SELECT historical_episodes_1.id" in sql
    assert "historical_episodes_1.parent_episode_id = historical_episodes.id" in sql


def test_the_geography_filter_matches_case_insensitively_when_supplied() -> None:
    sql = _ranking_sql(_query(geographies=frozenset({"United States"})))

    assert "lower(historical_episodes.geography) IN ('united states')" in sql


def test_the_industry_filter_overlaps_the_episode_array_when_supplied() -> None:
    sql = _ranking_sql(_query(industries=frozenset({"Banking", "insurance"})))

    assert "unnest(historical_episodes.affected_industries)" in sql
    assert "IN ('banking', 'insurance')" in sql


def test_unsupplied_filters_are_absent_from_the_statement() -> None:
    """Optional means optional: neither filter may become mandatory by being left out."""
    sql = _ranking_sql(_query())

    assert "lower(historical_episodes.geography)" not in sql  # selected, never predicated on
    assert "unnest" not in sql


def test_regime_is_never_a_hard_filter() -> None:
    """The spec caveats other-regime episodes; excluding them would hide them."""
    sql = _ranking_sql(_query(regime_tags=frozenset({"post_QE"})))

    assert "regime_tags IN" not in sql
    assert "&&" not in sql
    assert "historical_episodes.regime_tags" in sql  # selected, so the gate can flag it


# --- scoring, the threshold, and abstention -------------------------------------------
def test_similarity_is_one_minus_cosine_distance_on_the_zero_to_one_scale() -> None:
    session = FakeSession(ranking=[_row(0.13)])
    session.outcomes = [_outcome_row(session.ranking[0].id)]

    result = retrieve_analogy_candidates(session, _query())

    assert result.matches[0].candidate.similarity == 0.87


def test_the_threshold_is_inclusive_at_exactly_zero_point_six() -> None:
    session = FakeSession(ranking=[_row(0.4)])  # 1 - 0.4 == 0.6 in float64 only after rounding
    session.outcomes = [_outcome_row(session.ranking[0].id)]

    result = retrieve_analogy_candidates(session, _query())

    assert result.status is RetrievalStatus.MATCHED
    assert result.matches[0].candidate.similarity == 0.6


def test_a_candidate_just_below_the_threshold_is_not_returned() -> None:
    session = FakeSession(ranking=[_row(0.400001)])

    result = retrieve_analogy_candidates(session, _query())

    assert result.status is RetrievalStatus.NO_RELIABLE_ANALOGY
    assert result.matches == ()


def test_below_the_threshold_the_answer_is_no_reliable_analogy_not_an_exception() -> None:
    session = FakeSession(ranking=[_row(0.5), _row(0.9)])

    result = retrieve_analogy_candidates(session, _query())

    assert result.status is RetrievalStatus.NO_RELIABLE_ANALOGY
    assert result.message == NO_RELIABLE_ANALOGY == "no reliable analogy"
    assert result.matches == ()
    assert result.distribution is None
    # The near-misses are reported as a number for recalibration, never as candidates.
    assert (result.considered_count, result.best_similarity) == (2, 0.5)
    assert len(session.statements) == 1  # no outcome query was ever issued


def test_an_empty_corpus_abstains_without_inventing_a_best_similarity() -> None:
    session = FakeSession(ranking=[])

    result = retrieve_analogy_candidates(session, _query())

    assert result.status is RetrievalStatus.NO_RELIABLE_ANALOGY
    assert (result.considered_count, result.best_similarity) == (0, None)


def test_only_the_candidates_over_the_threshold_survive_a_mixed_neighbourhood() -> None:
    over, under = _row(0.2), _row(0.7)
    session = FakeSession(ranking=[over, under], outcomes=[_outcome_row(over.id)])

    result = retrieve_analogy_candidates(session, _query())

    assert [match.candidate.episode_id for match in result.matches] == [over.id]
    assert result.considered_count == 2


def test_the_sql_ordering_is_the_returned_ordering() -> None:
    rows = [_row(0.1), _row(0.2), _row(0.3)]
    session = FakeSession(ranking=rows, outcomes=[_outcome_row(row.id) for row in rows])

    result = retrieve_analogy_candidates(session, _query())

    assert [match.candidate.episode_id for match in result.matches] == [row.id for row in rows]
    assert [match.candidate.similarity for match in result.matches] == [0.9, 0.8, 0.7]


# --- the regime gate on candidates ----------------------------------------------------
def test_an_episode_from_another_regime_is_returned_flagged_not_dropped() -> None:
    row = _row(0.2, regime_tags=["pre_QE"])
    session = FakeSession(ranking=[row], outcomes=[_outcome_row(row.id)])

    result = retrieve_analogy_candidates(session, _query(regime_tags=frozenset({"post_QE"})))

    candidate = result.matches[0].candidate
    assert candidate.regime_caveats_required is True
    assert candidate.regime_caveat_reasons  # deterministic reasons for item 3 to surface


def test_an_episode_sharing_the_current_regime_needs_no_caveat() -> None:
    row = _row(0.2, regime_tags=["post_QE", "pre_social_media"])
    session = FakeSession(ranking=[row], outcomes=[_outcome_row(row.id)])

    result = retrieve_analogy_candidates(session, _query(regime_tags=frozenset({"post_QE"})))

    assert result.matches[0].candidate.regime_caveats_required is False
    assert result.matches[0].candidate.regime_caveat_reasons == ()


def test_without_current_regime_tags_nothing_is_caveated() -> None:
    row = _row(0.2, regime_tags=["pre_fiat"])
    session = FakeSession(ranking=[row], outcomes=[_outcome_row(row.id)])

    result = retrieve_analogy_candidates(session, _query())

    assert result.matches[0].candidate.regime_caveats_required is False


# --- outcomes attach after matching, never before -------------------------------------
def test_outcomes_are_read_by_a_second_statement_scoped_to_the_matched_episodes() -> None:
    over, under = _row(0.2), _row(0.9)
    session = FakeSession(ranking=[over, under], outcomes=[_outcome_row(over.id)])

    retrieve_analogy_candidates(session, _query())

    assert len(session.statements) == 2
    outcome_sql = _sql(session.statements[1])
    for column in OUTCOME_COLUMNS:
        assert f"historical_episodes.{column}" in outcome_sql
    assert str(over.id) in outcome_sql
    assert str(under.id) not in outcome_sql  # the near-miss's outcome is never read
    assert "<=>" not in outcome_sql


def test_the_ranked_candidate_type_has_nowhere_to_put_an_outcome() -> None:
    row = _row(0.2)
    session = FakeSession(ranking=[row], outcomes=[_outcome_row(row.id)])

    match = retrieve_analogy_candidates(session, _query()).matches[0]

    assert isinstance(match.candidate, EpisodeCandidate)
    for column in OUTCOME_COLUMNS:
        assert not hasattr(match.candidate, column)
    # Hindsight lives on the labelled outcome object, joined in only after the match.
    assert match.outcome.outcomes == ("failure", "bailout")
    assert match.outcome.resolution_mechanism == "FDIC systemic risk exception"
    assert match.outcome.episode_id == row.id


def test_a_matched_candidate_carries_everything_the_reranker_needs() -> None:
    row = _row(0.2, parent_episode_id=None)
    session = FakeSession(ranking=[row], outcomes=[_outcome_row(row.id)])

    candidate = retrieve_analogy_candidates(session, _query()).matches[0].candidate

    assert candidate.episode_id == row.id
    assert candidate.name == row.name
    assert candidate.episode_type == "banking_stress"
    assert (candidate.onset_date, candidate.peak_date, candidate.end_date) == (ONSET, None, None)
    assert candidate.onset_summary == row.onset_summary
    assert candidate.onset_indicators == {"uninsured_deposit_pct": 94}
    assert candidate.geography == "United States"
    assert candidate.affected_industries == ("banking",)
    assert candidate.regime_tags == ("post_QE", "post_dodd_frank")
    assert candidate.is_counterexample is False
    assert candidate.source_refs == {"refs": ["fdic"]}
    assert candidate.similarity == 0.8


def test_the_parent_arc_is_loaded_as_context_only_for_matched_children() -> None:
    parent_id = uuid.uuid4()
    row = _row(0.2, parent_episode_id=parent_id)
    session = FakeSession(
        ranking=[row],
        outcomes=[_outcome_row(row.id)],
        parents=[
            SimpleNamespace(id=parent_id, name="2023 banking stress", onset_summary="Arc onset.")
        ],
    )

    match = retrieve_analogy_candidates(session, _query()).matches[0]

    assert len(session.statements) == 3
    parent_sql = _sql(session.statements[2])
    for column in OUTCOME_COLUMNS:
        assert column not in parent_sql  # a parent is context, not a spoiler
    assert match.candidate.parent_episode_id == parent_id
    assert match.parent is not None
    assert (match.parent.episode_id, match.parent.name) == (parent_id, "2023 banking stress")


def test_no_parent_statement_is_issued_when_no_match_has_a_parent() -> None:
    row = _row(0.2)
    session = FakeSession(ranking=[row], outcomes=[_outcome_row(row.id)])

    result = retrieve_analogy_candidates(session, _query())

    assert len(session.statements) == 2
    assert result.matches[0].parent is None


def test_the_distribution_is_computed_over_the_matched_episodes() -> None:
    rows = [_row(0.2), _row(0.3, is_counterexample=True)]
    session = FakeSession(
        ranking=rows,
        outcomes=[
            _outcome_row(rows[0].id, outcomes=["failure"]),
            _outcome_row(rows[1].id, outcomes=["contained"]),
        ],
    )

    result = retrieve_analogy_candidates(session, _query())

    assert result.distribution is not None
    assert result.distribution.matched_count == 2
    assert result.distribution.counterexample_count == 1


# --- the event entry point ------------------------------------------------------------
def _event(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {"id": EVENT_ID, "event_type": "banking_stress"}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_a_missing_event_is_an_invalid_request_not_a_no_match() -> None:
    session = FakeSession(event=None)

    with pytest.raises(EventNotFoundError, match=str(EVENT_ID)):
        retrieve_analogies_for_event(session, EVENT_ID)

    assert session.statements == []


def test_an_event_without_a_vector_in_the_configured_space_is_a_precondition_failure() -> None:
    session = FakeSession(event=_event(), vector=None)

    with pytest.raises(EventEmbeddingMissingError, match=f"{MODEL}@{VERSION}"):
        retrieve_analogies_for_event(session, EVENT_ID)

    # It asked the one model space and stopped; it never fell back to embedding on the fly.
    embedding_sql = _sql(session.statements[0])
    assert f"event_embeddings.model = '{MODEL}'" in embedding_sql
    assert f"event_embeddings.model_version = '{VERSION}'" in embedding_sql
    assert len(session.statements) == 1


def test_an_unclassifiable_event_abstains_instead_of_ranking_all_of_history() -> None:
    session = FakeSession(event=_event(event_type="earthquake"), vector=VECTOR)

    result = retrieve_analogies_for_event(session, EVENT_ID)

    assert result.status is RetrievalStatus.UNSUPPORTED_EVENT_TYPE
    assert NO_RELIABLE_ANALOGY in result.message
    assert result.matches == ()
    assert result.episode_types == ()
    assert session.statements == []  # nothing was searched


def test_an_event_with_no_type_at_all_abstains_the_same_way() -> None:
    session = FakeSession(event=_event(event_type=None), vector=VECTOR)

    assert (
        retrieve_analogies_for_event(session, EVENT_ID).status
        is RetrievalStatus.UNSUPPORTED_EVENT_TYPE
    )


def test_an_event_is_matched_against_its_own_family_in_the_configured_model_space() -> None:
    row = _row(0.2)
    session = FakeSession(
        event=_event(event_type="Banking Stress"),
        vector=VECTOR,
        ranking=[row],
        outcomes=[_outcome_row(row.id)],
    )

    result = retrieve_analogies_for_event(
        session, EVENT_ID, regime_tags=["post_QE"], geographies=["United States"]
    )

    assert result.status is RetrievalStatus.MATCHED
    assert result.episode_types == ("banking_stress",)
    assert (result.model, result.model_version) == (MODEL, VERSION)
    ranking_sql = _sql(session.statements[1])
    assert "historical_episodes.episode_type IN ('banking_stress')" in ranking_sql
    assert "lower(historical_episodes.geography) IN ('united states')" in ranking_sql
    assert result.matches[0].candidate.regime_caveats_required is False


def test_an_explicit_unknown_episode_type_override_raises() -> None:
    session = FakeSession(event=_event(), vector=VECTOR)

    with pytest.raises(AnalogyRequestError, match="unknown episode types"):
        retrieve_analogies_for_event(session, EVENT_ID, episode_types=["earthquake"])


def test_an_explicitly_empty_episode_type_override_is_a_bug_not_an_abstention() -> None:
    """Asking for nothing is invalid input; only an unclassifiable *event* abstains."""
    session = FakeSession(event=_event(), vector=VECTOR)

    with pytest.raises(AnalogyRequestError, match="supplied but empty"):
        retrieve_analogies_for_event(session, EVENT_ID, episode_types=[])


def test_an_episode_type_override_cannot_widen_the_search_to_another_family() -> None:
    """The override narrows within the event's own family. It is not a way to search a foreign one.

    Compatibility is the rule the module is built on: an event is only ever matched against its own
    episode family. A caller free to name any family would walk straight around it and rank a bank
    run against pandemics -- returning the nearest vectors in an unrelated corpus and presenting
    them as history's verdict. That is a caller bug, so it raises; it is emphatically not a
    no-match, which the caller must be able to tell apart from it.
    """
    session = FakeSession(event=_event(event_type="banking_stress"), vector=VECTOR)

    with pytest.raises(AnalogyRequestError, match="not compatible with event type"):
        retrieve_analogies_for_event(session, EVENT_ID, episode_types=["pandemic"])


def test_an_episode_type_override_may_narrow_within_the_events_own_family() -> None:
    session = FakeSession(event=_event(event_type="banking_stress"), vector=VECTOR, ranking=[])

    result = retrieve_analogies_for_event(session, EVENT_ID, episode_types=["banking_stress"])

    assert result.episode_types == ("banking_stress",)


def test_an_override_cannot_revive_a_search_the_event_type_does_not_license() -> None:
    """An unclassifiable event abstains, and no argument unlocks it: the gate runs first."""
    session = FakeSession(event=_event(event_type="celebrity_gossip"), vector=VECTOR)

    result = retrieve_analogies_for_event(session, EVENT_ID, episode_types=["banking_stress"])

    assert result.status is RetrievalStatus.UNSUPPORTED_EVENT_TYPE
    assert result.episode_types == ()


def test_the_event_entry_point_keeps_the_spec_defaults() -> None:
    session = FakeSession(event=_event(), vector=VECTOR, ranking=[])

    result = retrieve_analogies_for_event(session, EVENT_ID)

    assert (result.top_k, result.min_similarity) == (DEFAULT_TOP_K, DEFAULT_MIN_SIMILARITY)


# --- non-finite query vectors ----------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_query_vector_is_rejected(bad: float) -> None:
    """A NaN in the query would abstain forever while looking like an honest no-match.

    pgvector's cosine distance against a NaN component is NaN, so `similarity >= min_similarity`
    is False for every episode in the corpus. The search would return "no reliable analogy" --
    the spec's legitimate answer -- for a reason that has nothing to do with history.
    """
    vector = (bad,) + (0.1,) * (EMBEDDING_DIM - 1)

    with pytest.raises(AnalogyRequestError, match="not finite"):
        _query(vector=vector)


def test_an_all_nan_vector_does_not_sail_past_the_all_zeros_check() -> None:
    """NaN is truthy, so `any(values)` is True for a vector of nothing but NaN."""
    with pytest.raises(AnalogyRequestError, match="not finite"):
        _query(vector=(float("nan"),) * EMBEDDING_DIM)
