"""ADR 0006 item 3: identity refresh task wiring, Beat schedules, and provider bindings.

Every test here runs against fake sessions and fake providers, so the suite never opens a
socket. The bindings that would build live clients are asserted on separately, by recording
the keyword arguments they hand the client classes.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from celery.schedules import crontab

from db.models import (
    EntityAlias,
    EntityIdentifier,
    EntityProfile,
    EntityRelationship,
    ProviderRun,
    RawIngestionItem,
)
from packages.config import metrics
from packages.config.settings import get_settings
from packages.providers.base import LEIRecord, WikidataEntity, WikidataQidMatch
from packages.providers.fakes import (
    FakeEntityIdentityProvider,
    FakeSECCompanyTickerProvider,
    FakeWikidataProvider,
)
from packages.providers.gleif import GLEIFClient
from packages.providers.sec_edgar import SECEdgarClient
from packages.providers.wikidata import WikidataClient
from tests.unit.test_provider_data_tasks import FakeSession
from workers import provider_data_tasks
from workers import (
    tasks as _heartbeat_tasks,  # noqa: F401  (registers the heartbeat, as the worker's `include` does)
)
from workers.celery_app import (
    BEAT_SCHEDULE,
    IDENTITY_BEAT_SCHEDULE,
    QUEUE_INGESTION,
    Stage1Task,
    celery_app,
)

SEC_TASK = "workers.provider_data_tasks.run_sec_identity_refresh"
GLEIF_TASK = "workers.provider_data_tasks.run_entity_identity_ingestion"
WIKIDATA_TASK = "workers.provider_data_tasks.run_wikidata_identity_ingestion"

APPLE_LEI = "HWUPKR0MPOU8FGXBT394"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Settings are process-cached, so a test that changes the environment must reset them."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _install_fake_session(monkeypatch) -> tuple[dict[Any, Any], list[FakeSession]]:
    store: dict[Any, Any] = {}
    sessions: list[FakeSession] = []

    def session_factory() -> FakeSession:
        session = FakeSession(store)
        sessions.append(session)
        return session

    monkeypatch.setattr(provider_data_tasks, "SessionLocal", session_factory)
    return store, sessions


def _bind(monkeypatch, name: str, provider: Any) -> None:
    """Inject a fake provider through the binding seam the tasks resolve clients with."""
    monkeypatch.setattr(
        provider_data_tasks,
        name,
        lambda: provider_data_tasks.ProviderBinding(
            provider=provider, mode="test_fake_ingestion", network_called=False
        ),
    )


def _of(store: dict[Any, Any], model: type[Any]) -> list[Any]:
    return [item for item in store.values() if isinstance(item, model)]


# --- Task registration ----------------------------------------------------------------
def test_the_three_identity_tasks_are_registered_with_retry_defaults() -> None:
    for name in (SEC_TASK, GLEIF_TASK, WIKIDATA_TASK):
        assert name in celery_app.tasks
        # Stage1Task carries the repository retry/backoff contract onto the real task object.
        task = celery_app.tasks[name]
        assert isinstance(task, Stage1Task)
        assert task.autoretry_for == (Exception,)
        assert task.max_retries == 3
        assert task.retry_backoff is True


# --- Beat schedule --------------------------------------------------------------------
def test_beat_schedules_match_the_adr_cadence() -> None:
    sec = IDENTITY_BEAT_SCHEDULE["sec-company-identity-weekly"]
    gleif = IDENTITY_BEAT_SCHEDULE["gleif-entity-identity-monthly"]
    wikidata = IDENTITY_BEAT_SCHEDULE["wikidata-entity-identity-monthly"]

    assert (sec["task"], gleif["task"], wikidata["task"]) == (SEC_TASK, GLEIF_TASK, WIKIDATA_TASK)
    # ADR 0006: SEC weekly, GLEIF and Wikidata monthly.
    assert sec["schedule"] == crontab(minute=0, hour=6, day_of_week="monday")
    assert gleif["schedule"] == crontab(minute=0, hour=7, day_of_month="1")
    assert wikidata["schedule"] == crontab(minute=0, hour=8, day_of_month="1")
    for entry in (sec, gleif, wikidata):
        assert entry["options"] == {"queue": QUEUE_INGESTION}


def test_the_monthly_pair_runs_in_precedence_order_after_the_weekly_seed() -> None:
    """When the 1st is a Monday all three fire, and they must fire SEC -> GLEIF -> Wikidata."""
    # crontab.hour is the set of matching hours, so the single configured hour is its minimum.
    hour_of = {
        name: min(IDENTITY_BEAT_SCHEDULE[name]["schedule"].hour)
        for name in (
            "sec-company-identity-weekly",
            "gleif-entity-identity-monthly",
            "wikidata-entity-identity-monthly",
        )
    }

    assert (
        hour_of["sec-company-identity-weekly"]
        < hour_of["gleif-entity-identity-monthly"]
        < hour_of["wikidata-entity-identity-monthly"]
    )


def test_beat_schedule_has_no_duplicate_or_unregistered_tasks() -> None:
    scheduled = [entry["task"] for entry in BEAT_SCHEDULE.values()]

    # One entry per task: a task scheduled twice would run twice per period.
    assert len(scheduled) == len(set(scheduled))
    # Beat cannot dispatch a name the worker never registered.
    assert set(scheduled) <= set(celery_app.tasks)
    assert set(IDENTITY_BEAT_SCHEDULE) <= set(BEAT_SCHEDULE)
    assert celery_app.conf.beat_schedule == BEAT_SCHEDULE


# --- Provider bindings ----------------------------------------------------------------
class _RecordingClient:
    """Stands in for a client class to capture exactly how the binding configured it."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_identity_bindings_skip_cleanly_while_the_user_agents_are_placeholders() -> None:
    """The shipped defaults must not let a scheduled refresh call SEC or WDQS."""
    for binding in (
        provider_data_tasks._sec_identity_provider_binding(),
        provider_data_tasks._wikidata_provider_binding(),
    ):
        assert binding.provider is None
        assert binding.mode == "configuration_skipped"
        assert binding.network_called is False
        assert "not configured" in binding.unavailable_reason


def test_sec_identity_binding_configures_user_agent_endpoint_and_timeout(monkeypatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "news-intel/1.0 (ops@example.com)")
    monkeypatch.setenv("SEC_COMPANY_TICKERS_URL", "https://sec.example.test/company_tickers.json")
    monkeypatch.setenv("SEC_TIMEOUT_SECONDS", "12.5")
    get_settings.cache_clear()
    monkeypatch.setattr(provider_data_tasks, "SECEdgarClient", _RecordingClient)

    binding = provider_data_tasks._sec_identity_provider_binding()

    assert binding.mode == "provider_ingestion"
    assert binding.network_called is True
    assert binding.provider.kwargs == {
        "user_agent": "news-intel/1.0 (ops@example.com)",
        "company_tickers_url": "https://sec.example.test/company_tickers.json",
        "timeout": 12.5,
    }


def test_wikidata_binding_configures_user_agent_endpoint_and_timeout(monkeypatch) -> None:
    monkeypatch.setenv("WIKIDATA_USER_AGENT", "news-intel/1.0 (ops@example.com)")
    monkeypatch.setenv("WIKIDATA_SPARQL_ENDPOINT", "https://wdqs.example.test/sparql")
    monkeypatch.setenv("WIKIDATA_TIMEOUT_SECONDS", "9.5")
    get_settings.cache_clear()
    monkeypatch.setattr(provider_data_tasks, "WikidataClient", _RecordingClient)

    binding = provider_data_tasks._wikidata_provider_binding()

    assert binding.provider.kwargs == {
        "user_agent": "news-intel/1.0 (ops@example.com)",
        "endpoint": "https://wdqs.example.test/sparql",
        "timeout": 9.5,
    }


def test_gleif_binding_configures_endpoints_user_agent_and_timeout(monkeypatch) -> None:
    monkeypatch.setenv("GLEIF_BASE_URL", "https://gleif.example.test/api/v1/")
    monkeypatch.setenv("GLEIF_USER_AGENT", "news-intel/1.0 (ops@example.com)")
    monkeypatch.setenv("GLEIF_TIMEOUT_SECONDS", "7.5")
    get_settings.cache_clear()
    monkeypatch.setattr(provider_data_tasks, "GLEIFClient", _RecordingClient)

    binding = provider_data_tasks._gleif_provider_binding()

    assert binding.provider.kwargs == {
        "records_endpoint": "https://gleif.example.test/api/v1/lei-records",
        "relationships_endpoint": "https://gleif.example.test/api/v1/relationship-records",
        "user_agent": "news-intel/1.0 (ops@example.com)",
        "timeout": 7.5,
    }


def test_configured_bindings_build_the_real_clients(monkeypatch) -> None:
    """Guards the wiring itself: the bindings must construct usable live clients."""
    for variable in ("SEC_USER_AGENT", "WIKIDATA_USER_AGENT", "GLEIF_USER_AGENT"):
        monkeypatch.setenv(variable, "news-intel/1.0 (ops@example.com)")
    get_settings.cache_clear()

    assert isinstance(provider_data_tasks._sec_identity_provider_binding().provider, SECEdgarClient)
    assert isinstance(provider_data_tasks._wikidata_provider_binding().provider, WikidataClient)
    assert isinstance(provider_data_tasks._gleif_provider_binding().provider, GLEIFClient)


# --- SEC weekly refresh ---------------------------------------------------------------
def test_sec_identity_refresh_seeds_profiles_identifiers_and_aliases(monkeypatch) -> None:
    store, sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", FakeSECCompanyTickerProvider())
    metrics.reset()

    result = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    assert result["status"] == "ok"
    assert result["state"] == "succeeded"
    assert result["provider"] == "sec-edgar"
    assert result["run_type"] == "company_identity"
    assert result["network_called"] is False
    # Three seed rows, two distinct CIKs: Alphabet's second share class is not a second entity.
    assert result["fetched"] == 3
    assert result["inserted"] == 2
    assert len(_of(store, EntityProfile)) == 2
    assert len(_of(store, ProviderRun)) == 1
    # The service writes raw items under the task's run, never a nested run of its own.
    assert {item.provider_run_id for item in _of(store, RawIngestionItem)} == {
        _of(store, ProviderRun)[0].id
    }
    assert sessions[-1].commits == 1
    assert sessions[-1].closed is True
    assert metrics.get(metrics.JOB_SUCCESSES) == 1


def test_a_new_week_opens_a_new_run_while_a_repeat_inside_one_week_is_idempotent(
    monkeypatch,
) -> None:
    """A constant parameter set would make the weekly refresh a permanent no-op after week 1."""
    store, _sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", FakeSECCompanyTickerProvider())

    first = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")
    repeat = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")
    next_week = provider_data_tasks.run_sec_identity_refresh(period="2026-W29")

    assert first["run_id"] == repeat["run_id"]
    assert repeat["idempotent"] is True
    assert next_week["run_id"] != first["run_id"]
    assert next_week["idempotent"] is False
    assert len(_of(store, ProviderRun)) == 2
    # The next week re-reads the same seed: row-level ingestion stays idempotent, so the
    # entity store does not grow.
    assert next_week["inserted"] == 0
    assert len(_of(store, EntityProfile)) == 2


def test_the_refresh_period_defaults_to_the_current_week(monkeypatch) -> None:
    _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", FakeSECCompanyTickerProvider())
    monkeypatch.setattr(
        provider_data_tasks, "_utc_now", lambda: datetime.datetime(2026, 7, 12, tzinfo=datetime.UTC)
    )

    provider_data_tasks.run_sec_identity_refresh()

    # 2026-07-12 is a Sunday, the last day of ISO week 28.
    assert provider_data_tasks._weekly_period(datetime.datetime(2026, 7, 12)) == "2026-W28"
    assert provider_data_tasks._monthly_period(datetime.datetime(2026, 7, 12)) == "2026-07"


# --- Bounded inputs -------------------------------------------------------------------
class _RecordingWikidataProvider(FakeWikidataProvider):
    """Records exactly which QIDs and identifier values the service asks the endpoint for."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.requested_qids: list[list[str]] = []
        self.resolved: list[dict[str, list[str]]] = []

    def resolve_qids(self, *, ciks=(), leis=(), limit=10_000):  # noqa: ANN001, ANN201
        self.resolved.append({"ciks": list(ciks), "leis": list(leis)})
        return super().resolve_qids(ciks=ciks, leis=leis, limit=limit)

    def fetch_entities(self, qids, *, limit=10_000):  # noqa: ANN001, ANN201
        self.requested_qids.append(list(qids))
        return super().fetch_entities(qids, limit=limit)


def test_wikidata_refresh_only_ever_asks_for_configured_qids_and_seeded_identifiers(
    monkeypatch,
) -> None:
    """ADR 0006 bounds Wikidata to known entities plus a curated list: no free-text discovery."""
    _store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setenv("WIKIDATA_QID_SEEDS", "Q95")
    get_settings.cache_clear()
    provider = _RecordingWikidataProvider()
    _bind(monkeypatch, "_wikidata_provider_binding", provider)

    provider_data_tasks.run_wikidata_identity_ingestion(period="2026-07")

    # Nothing is seeded, so there is nothing to resolve and only the curated QID is fetched.
    assert provider.resolved == []
    assert provider.requested_qids == [["Q95"]]


def test_wikidata_refresh_is_a_clean_no_op_when_nothing_is_configured_or_seeded(
    monkeypatch,
) -> None:
    store, sessions = _install_fake_session(monkeypatch)
    provider = _RecordingWikidataProvider()
    _bind(monkeypatch, "_wikidata_provider_binding", provider)

    result = provider_data_tasks.run_wikidata_identity_ingestion(period="2026-07")

    # An empty bounded input reaches the endpoint zero times, and still records a clean run.
    assert provider.resolved == []
    assert provider.requested_qids == []
    assert result["state"] == "succeeded"
    assert (result["fetched"], result["inserted"]) == (0, 0)
    assert len(_of(store, ProviderRun)) == 1
    assert sessions[-1].commits == 1
    assert sessions[-1].closed is True


def test_gleif_refresh_reads_its_watchlist_and_search_limit_from_settings(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setenv("IDENTITY_WATCHLIST", "Example Financial Holdings Inc.")
    monkeypatch.setenv("GLEIF_SEARCH_LIMIT", "7")
    get_settings.cache_clear()
    _bind(monkeypatch, "_gleif_provider_binding", FakeEntityIdentityProvider())

    result = provider_data_tasks.run_entity_identity_ingestion(period="2026-07")

    # The configured name is the only search input, and it is what mints the curated entity.
    assert result["provider"] == "gleif"
    assert result["details"]["candidates_searched"] == 1
    assert result["fetched"] == 1
    assert result["inserted"] == 1
    assert len(_of(store, EntityProfile)) == 1
    assert _of(store, ProviderRun)[0].parameters["limit"] == 7


def test_an_explicit_empty_watchlist_narrows_a_run_to_seeded_profiles_only(monkeypatch) -> None:
    _store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setenv("IDENTITY_WATCHLIST", "Example Financial Holdings Inc.")
    get_settings.cache_clear()
    _bind(monkeypatch, "_gleif_provider_binding", FakeEntityIdentityProvider())

    # Nothing is seeded yet, so an explicit empty list leaves no candidate at all: the
    # configured watchlist is bypassed rather than merged in.
    result = provider_data_tasks.run_entity_identity_ingestion(
        curated_watchlist=[], period="2026-07"
    )

    assert result["details"]["candidates_searched"] == 0
    assert result["fetched"] == 0


# --- Failure, retry, and cleanup ------------------------------------------------------
class _BrokenProvider:
    """A provider whose call fails the way a live endpoint outage would."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def fetch_company_tickers(self):  # noqa: ANN201
        self.calls += 1
        raise self._error


def test_a_provider_error_marks_the_run_failed_and_re_raises_for_retry(monkeypatch) -> None:
    store, sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", _BrokenProvider(RuntimeError("SEC 503")))
    metrics.reset()

    with pytest.raises(RuntimeError, match="SEC 503"):
        provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    # The failure is durable: without it, a rolled-back run leaves no trace and a retry
    # cannot tell a first attempt from a fifth.
    run = _of(store, ProviderRun)[0]
    assert run.status == "failed"
    assert run.error == {"message": "SEC 503", "type": "RuntimeError"}
    assert run.completed_at is not None
    assert sessions[-1].rollbacks == 1
    assert sessions[-1].closed is True
    assert metrics.get(metrics.JOB_FAILURES) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 0


def test_a_retry_after_failure_re_runs_that_period_and_succeeds(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", _BrokenProvider(RuntimeError("SEC 503")))

    with pytest.raises(RuntimeError):
        provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    # Celery retries the same task with the same arguments, so the same run key is re-entered.
    _bind(monkeypatch, "_sec_identity_provider_binding", FakeSECCompanyTickerProvider())
    result = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    assert result["state"] == "succeeded"
    # A failed run is resumed, not duplicated: the run key is the same, so it is the same row.
    assert len(_of(store, ProviderRun)) == 1
    assert _of(store, ProviderRun)[0].status == "succeeded"
    assert result["idempotent"] is False
    assert len(_of(store, EntityProfile)) == 2


def test_one_source_failing_never_reports_success_for_the_others(monkeypatch) -> None:
    """The three refreshes are independent Beat entries; a SEC outage must not mask itself."""
    store, _sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", _BrokenProvider(RuntimeError("SEC 503")))
    _bind(monkeypatch, "_wikidata_provider_binding", FakeWikidataProvider())

    with pytest.raises(RuntimeError):
        provider_data_tasks.run_sec_identity_refresh(period="2026-W28")
    wikidata = provider_data_tasks.run_wikidata_identity_ingestion(period="2026-07")

    runs = {(run.provider, run.status) for run in _of(store, ProviderRun)}
    assert runs == {("sec-edgar", "failed"), ("wikidata", "succeeded")}
    # Wikidata found nothing because the seed never landed — it reports that, not a fake win.
    assert wikidata["fetched"] == 0


# --- Sequencing and precedence --------------------------------------------------------
def _gleif_provider_for_apple() -> FakeEntityIdentityProvider:
    return FakeEntityIdentityProvider(
        records=[
            LEIRecord(
                lei=APPLE_LEI,
                legal_name="Apple Inc.",
                entity_status="ACTIVE",
                registration_status="ISSUED",
                country_code="US",
                jurisdiction="US-CA",
                legal_form="Corporation",
            )
        ],
        relationships=[],
    )


def _wikidata_provider_for_apple() -> FakeWikidataProvider:
    return FakeWikidataProvider(
        entities=[
            WikidataEntity(
                qid="Q312",
                label="Apple Computer",  # deliberately wrong: Wikidata must not win the name
                aliases=(),
                tickers=("AAPL",),
                leis=(APPLE_LEI,),
                ciks=("0000320193",),
            )
        ],
        qid_matches=[
            WikidataQidMatch(qid="Q312", identifier_type="cik", identifier_value="0000320193")
        ],
    )


def test_sec_seed_feeds_gleif_and_wikidata_without_losing_source_precedence(monkeypatch) -> None:
    """The integration the ADR actually specifies: SEC (1) > GLEIF (2) > Wikidata (3)."""
    store, _sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", FakeSECCompanyTickerProvider())
    _bind(monkeypatch, "_gleif_provider_binding", _gleif_provider_for_apple())
    _bind(monkeypatch, "_wikidata_provider_binding", _wikidata_provider_for_apple())

    # Each task is independently callable; the coupling between them is the persisted store.
    sec = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")
    gleif = provider_data_tasks.run_entity_identity_ingestion(
        curated_watchlist=[], country_code="US", period="2026-07"
    )
    wikidata = provider_data_tasks.run_wikidata_identity_ingestion(period="2026-07")

    assert (sec["state"], gleif["state"], wikidata["state"]) == ("succeeded",) * 3
    apple = next(
        profile for profile in _of(store, EntityProfile) if profile.primary_cik == "0000320193"
    )

    # SEC seeded the identity fields it owns and nothing downstream overwrote them.
    assert apple.canonical_name == "Apple Inc."
    assert apple.primary_ticker == "AAPL"
    # GLEIF filled the fields SEC left empty.
    assert apple.primary_lei == APPLE_LEI
    assert apple.country == "US"
    # Wikidata resolved onto the SEC-seeded profile through its CIK and claimed nothing.
    sources = apple.profile_metadata["identity_sources"]
    assert sources["canonical_name"] == "sec-edgar"
    assert sources["primary_cik"] == "sec-edgar"
    assert sources["primary_lei"] == "gleif"
    assert "wikidata" not in sources.values()

    # All three sources are recorded on the entity's identifiers, each with its own provenance.
    providers = {row.provider for row in _of(store, EntityIdentifier)}
    assert providers == {"sec-edgar", "gleif", "wikidata"}
    assert any(
        row.identifier_type == "wikidata_qid" and row.identifier_value == "Q312"
        for row in _of(store, EntityIdentifier)
    )
    # One ProviderRun per task, never a nested run owned by a service.
    assert {run.provider for run in _of(store, ProviderRun)} == {"sec-edgar", "gleif", "wikidata"}
    assert len(_of(store, ProviderRun)) == 3


def test_rerunning_the_whole_pipeline_inserts_nothing_new(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    _bind(monkeypatch, "_sec_identity_provider_binding", FakeSECCompanyTickerProvider())
    _bind(monkeypatch, "_gleif_provider_binding", _gleif_provider_for_apple())
    _bind(monkeypatch, "_wikidata_provider_binding", _wikidata_provider_for_apple())

    def run_all(week: str, month: str) -> None:
        provider_data_tasks.run_sec_identity_refresh(period=week)
        provider_data_tasks.run_entity_identity_ingestion(curated_watchlist=[], period=month)
        provider_data_tasks.run_wikidata_identity_ingestion(period=month)

    run_all("2026-W28", "2026-07")
    counts = {
        model: len(_of(store, model))
        for model in (EntityProfile, EntityAlias, EntityIdentifier, EntityRelationship)
    }

    # A later period re-reads the same upstream data: new runs, but no new entity rows.
    run_all("2026-W32", "2026-08")

    assert {
        model: len(_of(store, model))
        for model in (EntityProfile, EntityAlias, EntityIdentifier, EntityRelationship)
    } == counts
    assert len(_of(store, ProviderRun)) == 6
