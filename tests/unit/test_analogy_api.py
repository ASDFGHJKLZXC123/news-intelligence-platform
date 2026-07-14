"""The durable-analogy read endpoint, with the repository overridden (no database, no network).

The endpoint is a read of rows the worker already decided, so these tests are about what reaches
the wire: the two similarity scales kept on their own scales, hindsight kept under its own
explicitly named object, a deterministic order, and an honest status when there is nothing to
serve rather than a bare empty list.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from apps.api.analogies import AnalogyReadRepository, get_analogy_repository
from apps.api.main import app
from services.analogies.contracts import MAX_TOP_K, NO_RELIABLE_ANALOGY
from services.analogies.rerank import analogy_order_key

EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
MISSING_EVENT_ID = uuid.UUID("00000000-0000-4000-8000-000000000000")
SVB_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTINENTAL_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
LTCM_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")

OUTCOME_TEXT = "The bank failed and was placed into receivership."
RESOLUTION_TEXT = "FDIC systemic risk exception."


def _episode(
    episode_id: uuid.UUID,
    *,
    outcomes: list[str] | None = None,
    is_counterexample: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=episode_id,
        name="2023 regional banking stress",
        episode_type="banking_stress",
        onset_date=datetime.date(2023, 3, 8),
        peak_date=datetime.date(2023, 3, 10),
        end_date=None,
        onset_summary="Concentrated uninsured deposits face rapid withdrawals.",
        onset_indicators={"uninsured_deposit_pct": 94},
        geography="United States",
        affected_industries=["banking"],
        regime_tags=["post_QE", "post_dodd_frank"],
        is_counterexample=is_counterexample,
        source_refs={"refs": ["fdic"]},
        parent_episode_id=None,
        outcome_summary=OUTCOME_TEXT,
        outcomes=["failure"] if outcomes is None else outcomes,
        resolution_mechanism=RESOLUTION_TEXT,
    )


def _analogy(
    episode_id: uuid.UUID,
    *,
    score: float = 87.5,
    vector_similarity: float | None = 0.82,
    caveats: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        event_id=EVENT_ID,
        historical_episode_id=episode_id,
        llm_run_id=RUN_ID,
        similarity_score=score,
        rationale="Same deposit-run mechanism and concentrated funding base.",
        regime_caveats=caveats if caveats is not None else [],
        limitations=["episode carries no regime tags; regime comparability is unverified"],
        shared_causes=["episode_type:banking_stress", "regime:post_qe"],
        evidence_refs={"vector_similarity": vector_similarity, "embedding_model": "x"},
    )


class FakeAnalogyRepository:
    def __init__(self, rows: list[tuple[Any, Any]] | None = None, *, known: bool = True) -> None:
        self.rows = rows or []
        self.known = known
        self.limits: list[int] = []

    def event_exists(self, event_id: uuid.UUID) -> bool:
        return self.known and event_id == EVENT_ID

    def list_analogies(self, event_id: uuid.UUID, *, limit: int) -> list[tuple[Any, Any]]:
        self.limits.append(limit)
        # Mirror the SQL's own ORDER BY, so the endpoint's tie-break is doing real work here.
        rows = sorted(
            self.rows,
            key=lambda row: (-float(row[0].similarity_score), str(row[1].id)),
        )
        return rows[:limit]


@pytest.fixture
def client() -> Iterator[TestClient]:
    def _override() -> FakeAnalogyRepository:
        return repository

    repository = FakeAnalogyRepository([(_analogy(SVB_ID), _episode(SVB_ID))])
    app.dependency_overrides[get_analogy_repository] = _override
    with TestClient(app) as test_client:
        test_client.repository = repository  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()


def _get(client: TestClient, event_id: uuid.UUID = EVENT_ID, **params: Any) -> Any:
    return client.get(f"/api/v1/events/{event_id}/analogies", params=params)


def _serve(client: TestClient, rows: list[tuple[Any, Any]], **params: Any) -> dict[str, Any]:
    client.repository.rows = rows  # type: ignore[attr-defined]
    response = _get(client, **params)
    assert response.status_code == 200
    return response.json()


# --- the two answers -------------------------------------------------------------------
def test_an_unknown_event_is_a_404(client: TestClient) -> None:
    client.repository.known = False  # type: ignore[attr-defined]

    response = _get(client, MISSING_EVENT_ID)

    assert response.status_code == 404
    assert str(MISSING_EVENT_ID) in response.json()["detail"]


def test_an_event_with_no_durable_analogies_says_so_explicitly(client: TestClient) -> None:
    """Not a 404 and not a bare empty list: the spec's explicit, allowed answer."""
    payload = _serve(client, [])

    assert payload["status"] == "no_reliable_analogy"
    assert payload["message"] == NO_RELIABLE_ANALOGY
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["distribution"] is None


def test_a_matched_event_serves_its_durable_set(client: TestClient) -> None:
    payload = _serve(client, [(_analogy(SVB_ID), _episode(SVB_ID))])

    assert payload["status"] == "matched"
    assert payload["count"] == 1
    assert payload["event_id"] == str(EVENT_ID)


# --- what reaches the wire -------------------------------------------------------------
def test_the_scores_are_served_on_their_own_scales(client: TestClient) -> None:
    payload = _serve(
        client, [(_analogy(SVB_ID, score=91.0, vector_similarity=0.61), _episode(SVB_ID))]
    )
    item = payload["items"][0]

    assert item["similarity_score"] == 91.0  # 0-100, the reranker's structural score
    assert item["vector_similarity"] == 0.61  # 0.0-1.0, item 2's embedding cosine
    assert item["llm_run_id"] == str(RUN_ID)


def test_an_item_carries_its_rationale_caveats_and_onset_fields(client: TestClient) -> None:
    payload = _serve(
        client,
        [(_analogy(SVB_ID, caveats=["Pre-QE: no central-bank backstop."]), _episode(SVB_ID))],
    )
    item = payload["items"][0]
    episode = item["episode"]

    assert item["rationale"].startswith("Same deposit-run mechanism")
    assert item["regime_caveats"] == ["Pre-QE: no central-bank backstop."]
    assert item["limitations"] and item["shared_causes"]
    assert episode["episode_id"] == str(SVB_ID)
    assert episode["onset_summary"].startswith("Concentrated uninsured deposits")
    assert episode["onset_indicators"] == {"uninsured_deposit_pct": 94}
    assert episode["onset_date"] == "2023-03-08"
    assert episode["geography"] == "United States"
    assert episode["regime_tags"] == ["post_QE", "post_dodd_frank"]


def test_hindsight_is_served_only_under_an_explicitly_named_historical_outcome(
    client: TestClient,
) -> None:
    """Outcome text must never appear anywhere a reader could mistake it for the current event."""
    payload = _serve(client, [(_analogy(SVB_ID), _episode(SVB_ID))])
    item = payload["items"][0]
    episode = item["episode"]

    assert episode["historical_outcome"] == {
        "outcome_summary": OUTCOME_TEXT,
        "outcomes": ["failure"],
        "resolution_mechanism": RESOLUTION_TEXT,
    }
    # And nowhere else: not on the analogy, and not among the episode's onset fields.
    assert "outcome_summary" not in item
    assert "outcome_summary" not in episode
    assert "resolution_mechanism" not in episode
    assert OUTCOME_TEXT not in str(item["rationale"])


def test_the_served_order_is_llm_score_then_vector_similarity_then_id(
    client: TestClient,
) -> None:
    payload = _serve(
        client,
        [
            (_analogy(SVB_ID, score=80.0, vector_similarity=0.70), _episode(SVB_ID)),
            (_analogy(CONTINENTAL_ID, score=80.0, vector_similarity=0.95), _episode(CONTINENTAL_ID)),
            (_analogy(LTCM_ID, score=90.0, vector_similarity=0.61), _episode(LTCM_ID)),
        ],
    )

    assert [item["episode"]["episode_id"] for item in payload["items"]] == [
        str(LTCM_ID),  # highest structural score, worst vector prior -- still first
        str(CONTINENTAL_ID),  # tied score, better vector prior
        str(SVB_ID),
    ]


def test_a_row_without_a_stored_vector_similarity_still_orders_deterministically(
    client: TestClient,
) -> None:
    payload = _serve(
        client,
        [
            (_analogy(SVB_ID, score=80.0, vector_similarity=None), _episode(SVB_ID)),
            (_analogy(CONTINENTAL_ID, score=80.0, vector_similarity=0.5), _episode(CONTINENTAL_ID)),
        ],
    )

    assert [item["episode"]["episode_id"] for item in payload["items"]] == [
        str(CONTINENTAL_ID),
        str(SVB_ID),
    ]
    assert payload["items"][1]["vector_similarity"] is None


# --- the distribution ------------------------------------------------------------------
def test_the_distribution_is_computed_over_the_set_actually_served(client: TestClient) -> None:
    payload = _serve(
        client,
        [
            (_analogy(SVB_ID), _episode(SVB_ID, outcomes=["failure", "bailout"])),
            (
                _analogy(CONTINENTAL_ID, score=70.0),
                _episode(CONTINENTAL_ID, outcomes=["contained"], is_counterexample=True),
            ),
        ],
    )
    distribution = payload["distribution"]

    assert distribution["matched_count"] == 2
    tallies = {tally["outcome"]: tally["count"] for tally in distribution["tallies"]}
    assert tallies == {"failure": 1, "bailout": 1, "contained": 1}
    assert distribution["multi_tagged_count"] == 1  # conflicting outcomes, reported as such
    assert distribution["counterexample_count"] == 1


def test_the_distribution_shrinks_with_the_limit_it_served(client: TestClient) -> None:
    """The base rate describes the rows on this page, never rows that were not returned."""
    payload = _serve(
        client,
        [
            (_analogy(SVB_ID, score=90.0), _episode(SVB_ID, outcomes=["failure"])),
            (_analogy(CONTINENTAL_ID, score=70.0), _episode(CONTINENTAL_ID, outcomes=["bailout"])),
        ],
        limit=1,
    )

    assert payload["count"] == 1
    assert payload["distribution"]["matched_count"] == 1
    assert [tally["outcome"] for tally in payload["distribution"]["tallies"]] == ["failure"]


# --- query bounds ----------------------------------------------------------------------
@pytest.mark.parametrize("limit", [0, -1, MAX_TOP_K + 1])
def test_the_limit_is_bounded(client: TestClient, limit: int) -> None:
    assert _get(client, limit=limit).status_code == 422


def test_the_default_limit_serves_the_whole_durable_set(client: TestClient) -> None:
    _serve(client, [(_analogy(SVB_ID), _episode(SVB_ID))])

    assert client.repository.limits[-1] == MAX_TOP_K  # type: ignore[attr-defined]


# --- the ordering that the LIMIT is applied to -----------------------------------------
def test_the_sql_orders_by_the_full_tie_break_before_the_limit_cuts() -> None:
    """LIMIT is applied to whatever order the database produced, so the order must be complete.

    Re-sorting in Python cannot recover a row that was never fetched. With an ORDER BY that stopped
    at the 0-100 score, two analogies tied at 88.0 would be separated by episode id, and `limit=1`
    would return whichever had the smaller id -- discarding the one with the far better vector
    prior. The tie-break has to be *in the statement*, which means reaching into the JSONB where
    the vector similarity actually lives.
    """
    statement = _compiled_list_analogies(limit=1)

    order_by = statement.split("ORDER BY", 1)[1]
    score = order_by.index("event_analogies.similarity_score DESC")
    vector = order_by.index("vector_similarity")
    episode = order_by.index("event_analogies.historical_episode_id ASC")

    assert score < vector < episode, "the three keys must order in the reranker's own precedence"
    assert "LIMIT" in order_by  # and the cut comes after all three
    # A missing key and a JSON null both coalesce to 0.0, exactly as `_vector_similarity` does in
    # Python, so the database's order and the final in-process sort cannot disagree.
    assert "coalesce" in order_by.lower()


def test_the_sql_order_agrees_with_the_key_the_reranker_persisted_by() -> None:
    """One ordering rule, shared. If these drift, the served order stops matching the durable one."""
    rows = [
        (_analogy(SVB_ID, score=88.0, vector_similarity=0.61), _episode(SVB_ID)),
        (_analogy(CONTINENTAL_ID, score=88.0, vector_similarity=0.95), _episode(CONTINENTAL_ID)),
    ]
    ordered = sorted(
        rows,
        key=lambda row: analogy_order_key(
            similarity_score=float(row[0].similarity_score),
            vector_similarity=row[0].evidence_refs["vector_similarity"],
            episode_id=row[1].id,
        ),
    )

    # CONTINENTAL_ID has the larger id but the better vector prior, so it must come first -- which
    # is precisely the row an id-only tie-break plus `limit=1` would have thrown away.
    assert CONTINENTAL_ID > SVB_ID
    assert [episode.id for _analogy_row, episode in ordered] == [CONTINENTAL_ID, SVB_ID]


def _compiled_list_analogies(*, limit: int) -> str:
    """The statement `list_analogies` actually issues, compiled for PostgreSQL."""
    captured: list[Any] = []

    class _CapturingSession:
        def execute(self, statement: Any) -> Any:
            captured.append(statement)
            return SimpleNamespace(all=list)

    AnalogyReadRepository(_CapturingSession()).list_analogies(EVENT_ID, limit=limit)
    return str(
        captured[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
