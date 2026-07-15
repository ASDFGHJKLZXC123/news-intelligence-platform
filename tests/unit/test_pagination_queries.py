"""Repository-level proof of the Stage 7 item-2 pagination foundation.

Fake-repo/TestClient tests (test_intelligence_api / test_provider_data_api) prove the wire
envelope; these tests drive the *real* ``IntelligenceRepository`` / ``ProviderDataRepository``
against a recording session double (no database) and inspect the emitted SQL to prove:

* ``total`` is a ``COUNT`` over the filtered relation *before* LIMIT/OFFSET (not ``len(items)``),
* the count and the page filter on identical predicates,
* the page applies the requested LIMIT and OFFSET,
* admin-sources latest health is one bounded ``DISTINCT ON ... IN (...)`` read, never per-source.

The Stage 7 item-4 dashboard block at the bottom drives the same recording session to prove the
expanded ``GET /dashboard`` reads are a fixed, bounded set (constant statement counts, no N+1) and
reuse the item-3 persisted event-risk semantics rather than a competing formula.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from apps.api.intelligence import IntelligenceRepository
from apps.api.provider_data import ProviderDataRepository


class _RecordingScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _RecordingResult:
    def __init__(self, session: _RecordingSession) -> None:
        self._session = session

    def scalar_one(self) -> Any:
        return self._session.scalar_returns.pop(0)

    def scalars(self) -> _RecordingScalars:
        return _RecordingScalars(self._session.row_returns.pop(0))

    def all(self) -> list[Any]:
        return list(self._session.row_returns.pop(0))


class _RecordingSession:
    """Captures every executed statement and hands back pre-primed results in call order."""

    def __init__(self, *, scalar_returns: list[Any], row_returns: list[list[Any]]) -> None:
        self.statements: list[Any] = []
        self.scalar_returns = list(scalar_returns)
        self.row_returns = list(row_returns)

    def execute(self, statement: Any) -> _RecordingResult:
        self.statements.append(statement)
        return _RecordingResult(self)


def _sql(statement: Any) -> str:
    """Compile a statement to concrete PostgreSQL text with literal values inlined."""
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()


def _company_bulk_row(event_id: Any) -> tuple[Any, Any]:
    """A recorded ``(EventCompany, Company)`` join row for the bulk company attachment."""
    link = SimpleNamespace(
        event_id=event_id,
        company_id=uuid.uuid4(),
        impact_direction="negative",
        impact_score=decimal.Decimal("70"),
        risk_score=decimal.Decimal("58"),
        confidence_score=decimal.Decimal("0.8"),
        exposure_explanation="supplier",
    )
    company = SimpleNamespace(
        display_name="Example Semiconductors",
        primary_ticker="EXSM",
        exchange="NASDAQ",
        industry="Semiconductors",
    )
    return (link, company)


def test_list_events_counts_before_paging_with_identical_filters() -> None:
    event = SimpleNamespace(id=uuid.uuid4())
    session = _RecordingSession(
        scalar_returns=[137],
        row_returns=[
            [(event, decimal.Decimal("60"), decimal.Decimal("0.8"), "developing")],  # page
            [_company_bulk_row(event.id)],  # bulk companies
            [SimpleNamespace(event_id=event.id)],  # bulk industries
            [SimpleNamespace(event_id=event.id)],  # bulk locations
        ],
    )
    repo = IntelligenceRepository(session)

    rows, total = repo.list_events(
        q="chip",
        country="us",
        event_type=None,
        risk_level="high",
        status="developing",
        company="EXSM",
        industry="semiconductors",
        limit=5,
        offset=15,
    )

    # total is the COUNT scalar, independent of the single-row page.
    assert total == 137
    assert len(rows) == 1
    assert rows[0].status == "developing"
    assert rows[0].companies[0].primary_ticker == "EXSM"
    # count + page + a fixed three bulk attachments (companies/industries/locations).
    assert len(session.statements) == 5
    count_sql, page_sql = _sql(session.statements[0]), _sql(session.statements[1])

    assert "count(" in count_sql
    # The count carries no *pagination* -- only the page applies the requested limit/offset.
    assert "limit 5" not in count_sql and "offset 15" not in count_sql
    assert "limit 5" in page_sql and "offset 15" in page_sql
    # Identical predicates on both: the country equality and a q/name ilike appear in each,
    # and every derived filter (risk from observations, status from alerts, company/industry
    # links) narrows the count exactly as it narrows the page.
    for sql in (count_sql, page_sql):
        assert "upper(events.country)" in sql
        assert "ilike" in sql
        assert "risk_score_observations" in sql
        assert "alerts" in sql
        assert "event_companies" in sql
        assert "event_industries" in sql
    # Deterministic ordering carries the unique id tie-breaker.
    assert "events.id" in page_sql
    # The companies attachment is one bulk join keyed by the page's event ids -- no per-event read.
    companies_sql = _sql(session.statements[2])
    assert "join companies" in companies_sql
    assert " in (" in companies_sql


def test_list_events_empty_page_issues_no_attachment_queries() -> None:
    # offset past the end: zero rows, but the pre-limit total is still surfaced, and no bulk
    # company/industry/location read fires for an empty page.
    session = _RecordingSession(scalar_returns=[9], row_returns=[[]])
    repo = IntelligenceRepository(session)

    rows, total = repo.list_events(
        q=None, country=None, event_type=None, limit=50, offset=1000
    )

    assert rows == []
    assert total == 9
    # Just the count and the (empty) page -- the three attachment reads are skipped.
    assert len(session.statements) == 2
    assert "offset 1000" in _sql(session.statements[1])


def test_list_events_attachment_query_count_is_constant_as_page_grows() -> None:
    # Three events on the page still cost the same count + page + three bulk reads: no N+1.
    events = [SimpleNamespace(id=uuid.uuid4()) for _ in range(3)]
    session = _RecordingSession(
        scalar_returns=[3],
        row_returns=[
            [(event, None, None, "new") for event in events],  # page
            [_company_bulk_row(events[0].id)],  # bulk companies (single join read)
            [SimpleNamespace(event_id=events[1].id)],  # bulk industries
            [SimpleNamespace(event_id=events[2].id)],  # bulk locations
        ],
    )
    repo = IntelligenceRepository(session)

    rows, total = repo.list_events(q=None, country=None, event_type=None, limit=50, offset=0)

    assert total == 3
    assert len(rows) == 3
    # Constant five statements regardless of item count.
    assert len(session.statements) == 5


def test_list_sources_merges_latest_health_in_one_bounded_query() -> None:
    source_a = SimpleNamespace(id=uuid.uuid4(), name="A")
    source_b = SimpleNamespace(id=uuid.uuid4(), name="B")
    health_b = SimpleNamespace(source_id=source_b.id, status="degraded")
    session = _RecordingSession(
        scalar_returns=[3],
        row_returns=[[source_a, source_b], [health_b]],
    )
    repo = IntelligenceRepository(session)

    rows, total = repo.list_sources(active=True, limit=10, offset=0)

    assert total == 3
    # Only source_b has a latest snapshot; source_a merges to None.
    assert rows == [(source_a, None), (source_b, health_b)]
    # count + page + exactly one bulk health read -- no per-source N+1.
    assert len(session.statements) == 3
    health_sql = _sql(session.statements[2])
    assert "distinct on" in health_sql
    assert " in (" in health_sql


def test_list_sources_empty_page_issues_no_health_query() -> None:
    session = _RecordingSession(scalar_returns=[0], row_returns=[[]])
    repo = IntelligenceRepository(session)

    rows, total = repo.list_sources(active=None, limit=10, offset=0)

    assert rows == []
    assert total == 0
    # No source ids => no health lookup at all: just the count and the (empty) page.
    assert len(session.statements) == 2


def test_count_and_list_sec_companies_share_filters_and_page() -> None:
    count_session = _RecordingSession(scalar_returns=[42], row_returns=[])
    total = ProviderDataRepository(count_session).count_sec_companies(
        cik=None, ticker="aapl", q=None
    )
    assert total == 42
    count_sql = _sql(count_session.statements[0])
    assert "count(" in count_sql
    assert "upper(sec_companies.ticker)" in count_sql

    page_session = _RecordingSession(scalar_returns=[], row_returns=[[SimpleNamespace(id="c1")]])
    companies = ProviderDataRepository(page_session).list_sec_companies(
        cik=None, ticker="aapl", q=None, limit=5, offset=20
    )
    assert len(companies) == 1
    page_sql = _sql(page_session.statements[0])
    assert "upper(sec_companies.ticker)" in page_sql
    assert "limit 5" in page_sql
    assert "offset 20" in page_sql
    # Deterministic ordering with the unique id tie-breaker.
    assert "sec_companies.id" in page_sql


# --- Stage 7 item-4 dashboard query bounds -------------------------------------------


def test_dashboard_metric_counts_issue_four_bounded_count_reads() -> None:
    session = _RecordingSession(scalar_returns=[12, 3, 5, 7], row_returns=[])
    repo = IntelligenceRepository(session)
    now = datetime.datetime(2026, 6, 20, 9, 30, tzinfo=datetime.UTC)

    counts = repo.dashboard_metric_counts(now=now)

    assert (
        counts.events_today,
        counts.high_risk_events,
        counts.affected_industries,
        counts.affected_companies,
    ) == (12, 3, 5, 7)
    # Exactly four COUNT reads -- no per-row work, no pagination.
    assert len(session.statements) == 4
    today_sql, high_sql, industries_sql, companies_sql = (
        _sql(statement) for statement in session.statements
    )
    assert "count(" in today_sql
    # UTC-day bounds on the coverage activity time.
    assert "coalesce" in today_sql
    assert "2026-06-20" in today_sql
    # High-risk reuses the item-3 persisted event-risk semantics, not a competing formula.
    assert "risk_score_observations" in high_sql
    assert "event_companies" in high_sql
    assert "event_industries" in high_sql
    assert "55" in high_sql
    # Distinct affected entities.
    assert "count(distinct" in industries_sql and "event_industries" in industries_sql
    assert "count(distinct" in companies_sql and "event_companies" in companies_sql


def test_upcoming_triggers_is_one_future_bounded_ordered_read() -> None:
    item = SimpleNamespace(id=uuid.uuid4())
    session = _RecordingSession(scalar_returns=[], row_returns=[[item]])
    repo = IntelligenceRepository(session)
    now = datetime.datetime(2026, 6, 20, tzinfo=datetime.UTC)

    items = repo.upcoming_triggers(now=now, limit=50)

    assert items == [item]
    # One read, future-only, ordered by (timestamp, id), bounded.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "event_timeline_items.timestamp >" in sql
    assert "order by event_timeline_items.timestamp, event_timeline_items.id" in sql
    assert "limit 50" in sql


def test_event_map_aggregates_projects_and_dedups_in_one_bounded_read() -> None:
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    lat, lon = decimal.Decimal("38.9"), decimal.Decimal("-77.0")
    rows = [
        SimpleNamespace(
            location_name="DC", latitude=lat, longitude=lon,
            event_id=e1, event_type="policy", risk_score=decimal.Decimal("70"),
        ),
        SimpleNamespace(
            location_name="DC", latitude=lat, longitude=lon,
            event_id=e2, event_type="policy", risk_score=decimal.Decimal("80"),
        ),
        # Same event as the first row (a second location row): must not double-count.
        SimpleNamespace(
            location_name="DC", latitude=lat, longitude=lon,
            event_id=e1, event_type="policy", risk_score=decimal.Decimal("70"),
        ),
    ]
    session = _RecordingSession(scalar_returns=[], row_returns=[rows])
    repo = IntelligenceRepository(session)

    points = repo.event_map(limit=500)

    # One aggregation read: no per-location or per-event follow-up query.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "join events" in sql
    assert "event_locations.latitude is not null" in sql
    assert "event_locations.longitude is not null" in sql
    assert "limit 500" in sql
    # The item-3 persisted event risk is carried as a column, not a competing score.
    assert "risk_score_observations" in sql

    assert len(points) == 1
    point = points[0]
    assert point.event_count == 2  # e1 deduped
    assert set(point.related_event_ids) == {str(e1), str(e2)}
    assert len(set(point.related_event_ids)) == len(point.related_event_ids)  # unique
    assert point.max_risk_score == decimal.Decimal("80")
    assert point.dominant_event_type == "policy"


def test_company_ranking_is_one_distinct_on_join_read() -> None:
    company = SimpleNamespace(id=uuid.uuid4())
    rollup = SimpleNamespace(company_id=company.id)
    session = _RecordingSession(scalar_returns=[], row_returns=[[(company, rollup)]])
    repo = IntelligenceRepository(session)

    rows = repo.company_ranking(limit=100)

    assert rows == [(company, rollup)]
    # One DISTINCT ON latest-per-company read joined to active companies, bounded.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "distinct on" in sql
    assert "companies.active" in sql
    assert "limit 100" in sql


def test_dashboard_industry_summary_dedups_latest_per_industry_in_one_read() -> None:
    rollup = SimpleNamespace(industry_id="semis")
    session = _RecordingSession(scalar_returns=[], row_returns=[[rollup]])
    repo = IntelligenceRepository(session)

    rows = repo.dashboard_industry_summary(limit=100)

    assert rows == [rollup]
    # One DISTINCT ON latest-per-industry read -- the accepted item-6 dedup, bounded.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "distinct on" in sql
    assert "industry_risk_rollups" in sql
    assert "limit 100" in sql


# --- Stage 7 item-5 risk-radar detail/history query bounds ----------------------------


def test_latest_risk_observation_is_one_ordered_limit_one_read() -> None:
    obs = SimpleNamespace(id=uuid.uuid4())
    session = _RecordingSession(scalar_returns=[], row_returns=[[obs]])
    repo = IntelligenceRepository(session)

    result = repo.latest_risk_observation(risk_type="sovereign")

    assert result is obs
    # A single ordered, limit-1 read filtered by risk_type -- the deterministic spine.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "risk_score_observations.risk_type = 'sovereign'" in sql
    assert "order by risk_score_observations.as_of desc" in sql
    assert "risk_score_observations.id desc" in sql
    assert "limit 1" in sql


def test_latest_crisis_prediction_never_crosses_risk_type_and_prefers_target() -> None:
    pred = SimpleNamespace(id=uuid.uuid4())
    session = _RecordingSession(scalar_returns=[], row_returns=[[pred]])
    repo = IntelligenceRepository(session)

    result = repo.latest_crisis_prediction(
        risk_type="sovereign", target_type="country", target_id="US"
    )

    assert result is pred
    # One query. risk_type is an absolute predicate; the target match is only an ORDER BY
    # preference (a CASE), then recency -- so it never returns a different risk family.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "crisis_predictions.risk_type = 'sovereign'" in sql
    assert "case" in sql
    assert "crisis_predictions.target_type = 'country'" in sql
    # _sql lowercases literal binds, so 'US' -> 'us'.
    assert "crisis_predictions.target_id = 'us'" in sql
    assert "crisis_predictions.as_of_date desc" in sql
    assert "limit 1" in sql


def test_latest_crisis_prediction_without_target_has_no_case_branch() -> None:
    session = _RecordingSession(scalar_returns=[], row_returns=[[None]])
    repo = IntelligenceRepository(session)

    repo.latest_crisis_prediction(risk_type="banking", target_type=None, target_id=None)

    sql = _sql(session.statements[0])
    # No target to match: pure recency ordering, still absolute on risk_type.
    assert "crisis_predictions.risk_type = 'banking'" in sql
    assert "case" not in sql
    assert "limit 1" in sql


def test_related_risk_targets_is_one_distinct_bounded_ordered_read() -> None:
    rows = [
        SimpleNamespace(target_type="company", target_id="c1"),
        SimpleNamespace(target_type="event", target_id="e1"),
    ]
    session = _RecordingSession(scalar_returns=[], row_returns=[rows])
    repo = IntelligenceRepository(session)

    result = repo.related_risk_targets(risk_type="sovereign", limit=500)

    assert result == [("company", "c1"), ("event", "e1")]
    # One DISTINCT read -- no per-target follow-up (no N+1), deterministically ordered and bounded.
    assert len(session.statements) == 1
    sql = _sql(session.statements[0])
    assert "distinct" in sql
    assert "risk_score_observations.risk_type = 'sovereign'" in sql
    assert (
        "order by risk_score_observations.target_type, risk_score_observations.target_id" in sql
    )
    assert "limit 500" in sql


def test_risk_history_counts_before_paging_with_identical_cutoff_and_risk_type() -> None:
    obs = SimpleNamespace(id=uuid.uuid4())
    cutoff = datetime.datetime(2026, 5, 21, tzinfo=datetime.UTC)
    session = _RecordingSession(scalar_returns=[137], row_returns=[[obs]])
    repo = IntelligenceRepository(session)

    rows, total = repo.risk_observation_history(
        risk_type="sovereign", cutoff=cutoff, limit=7, offset=3
    )

    assert total == 137
    assert rows == [obs]
    # count + page: total is the pre-limit COUNT scalar, not len(items).
    assert len(session.statements) == 2
    count_sql, page_sql = _sql(session.statements[0]), _sql(session.statements[1])
    assert "count(" in count_sql
    # The count carries no pagination; only the page applies limit/offset.
    assert "limit 7" not in count_sql and "offset 3" not in count_sql
    assert "limit 7" in page_sql and "offset 3" in page_sql
    # Identical predicates on both: risk_type and the as_of cutoff narrow the count and the page.
    for sql in (count_sql, page_sql):
        assert "risk_score_observations.risk_type = 'sovereign'" in sql
        assert "risk_score_observations.as_of >= '2026-05-21" in sql
    # Chronological (ascending as_of) with the unique id tie-breaker for the UI series.
    assert "order by risk_score_observations.as_of, risk_score_observations.id" in page_sql


# --- Accepted-endpoint audit: latest-industry rollup dedup (shape gap 6) --------------


def test_list_industries_dedups_latest_per_industry_under_item2_envelope() -> None:
    # Read-only audit of the accepted `/api/v1/industries` dedup: one latest rollup per industry,
    # with the item-2 `total` counting distinct industries (the deduped relation), not snapshots.
    rollup = SimpleNamespace(industry_id="semis")
    session = _RecordingSession(scalar_returns=[4], row_returns=[[rollup]])
    repo = IntelligenceRepository(session)

    rows, total = repo.list_industries(limit=50, offset=5)

    assert rows == [rollup]
    assert total == 4
    # count + page, both over the DISTINCT ON (industry_id) latest-per-industry subquery.
    assert len(session.statements) == 2
    count_sql, page_sql = _sql(session.statements[0]), _sql(session.statements[1])
    assert "count(" in count_sql
    assert "distinct on (industry_risk_rollups.industry_id)" in count_sql
    assert "distinct on (industry_risk_rollups.industry_id)" in page_sql
    assert "limit 50" in page_sql and "offset 5" in page_sql
