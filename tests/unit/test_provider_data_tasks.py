"""Provider-data Celery task tests with fake DB sessions and no provider network."""

from __future__ import annotations

import datetime
from typing import Any

from db.models import (
    CountryIndicatorObservation,
    EnergyMarketSnapshot,
    EntityProfile,
    GeoIncident,
    HumanitarianReport,
    MacroObservation,
    MacroSeries,
    ProviderRun,
    RawIngestionItem,
    SanctionsEntity,
)
from packages.config import metrics
from packages.providers.fakes import (
    FakeCountryIndicatorProvider,
    FakeEnergyProvider,
    FakeEntityIdentityProvider,
    FakeFREDProvider,
    FakeGDELTProvider,
    FakeGeoIncidentProvider,
    FakeHumanitarianProvider,
    FakeSanctionsProvider,
    FakeSECEdgarProvider,
)
from workers import provider_data_tasks
from workers.celery_app import Stage1Task, celery_app


class FakeSession:
    def __init__(self, store: dict[Any, Any]) -> None:
        self.store = store
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def get(self, model, key):  # noqa: ANN001 - matches SQLAlchemy Session API
        return self.store.get(key)

    def add(self, obj) -> None:  # noqa: ANN001 - stores SQLAlchemy model instances
        self.store[obj.id] = obj

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        for item in self.find_all(model, **criteria):
            return item
        return None

    def find_all(self, model: type[Any], **criteria: Any) -> list[Any]:
        return [
            item
            for item in self.store.values()
            if isinstance(item, model)
            and all(getattr(item, key) == value for key, value in criteria.items())
        ]

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _install_fake_session(monkeypatch):
    store: dict[Any, Any] = {}
    sessions: list[FakeSession] = []

    def session_factory() -> FakeSession:
        session = FakeSession(store)
        sessions.append(session)
        return session

    monkeypatch.setattr(provider_data_tasks, "SessionLocal", session_factory)
    return store, sessions


def test_provider_data_tasks_are_registered_with_retry_defaults() -> None:
    names = {
        "workers.provider_data_tasks.run_country_indicator_ingestion",
        "workers.provider_data_tasks.run_energy_series_ingestion",
        "workers.provider_data_tasks.run_entity_identity_ingestion",
        "workers.provider_data_tasks.run_fred_macro_ingestion",
        "workers.provider_data_tasks.run_gdelt_raw_ingestion",
        "workers.provider_data_tasks.run_geo_incident_ingestion",
        "workers.provider_data_tasks.run_humanitarian_report_ingestion",
        "workers.provider_data_tasks.run_sanctions_ingestion",
        "workers.provider_data_tasks.run_sec_company_ingestion",
    }
    assert names <= set(celery_app.tasks)
    for name in names:
        assert isinstance(celery_app.tasks[name], Stage1Task)


def test_fred_task_records_config_skip_without_provider_network(monkeypatch) -> None:
    store, sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_fred_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="FRED_API_KEY is not configured",
        ),
    )
    metrics.reset()

    result = provider_data_tasks.run_fred_macro_ingestion(["GDP", "CPIAUCSL"])

    assert result["status"] == "ok"
    assert result["state"] == "succeeded"
    assert result["provider"] == "fred"
    assert result["run_type"] == "macro_observations"
    assert result["item_count"] == 0
    assert result["client_available"] is False
    assert result["mode"] == "configuration_skipped"
    assert result["network_called"] is False
    assert result["unavailable_reason"] == "FRED_API_KEY is not configured"
    assert len(store) == 1
    run = next(iter(store.values()))
    assert run.status == "succeeded"
    assert run.stats["mode"] == "configuration_skipped"
    assert run.stats["network_called"] is False
    assert sessions[-1].commits == 1
    assert sessions[-1].closed is True
    assert metrics.get(metrics.JOB_STARTS) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 1
    assert metrics.get(metrics.JOB_FAILURES) == 0


def test_fred_task_invokes_ingestion_service_with_fake_provider(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_fred_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeFREDProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )

    result = provider_data_tasks.run_fred_macro_ingestion(["GDP"])

    assert result["status"] == "ok"
    assert result["provider"] == "fred"
    assert result["mode"] == "test_fake_ingestion"
    assert result["network_called"] is False
    assert result["fetched"] == 3
    assert result["inserted"] == 3
    assert result["skipped"] == 0
    assert result["item_count"] == 3
    assert any(isinstance(item, ProviderRun) for item in store.values())
    assert any(isinstance(item, MacroSeries) for item in store.values())
    assert len([item for item in store.values() if isinstance(item, MacroObservation)]) == 3
    assert len([item for item in store.values() if isinstance(item, RawIngestionItem)]) == 3


def test_provider_task_rerun_is_idempotent(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_fred_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeFREDProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )

    first = provider_data_tasks.run_fred_macro_ingestion(["GDP", "CPIAUCSL"])
    second = provider_data_tasks.run_fred_macro_ingestion(["CPIAUCSL", "GDP"])

    assert first["run_id"] == second["run_id"]
    assert second["idempotent"] is True
    assert len([item for item in store.values() if isinstance(item, ProviderRun)]) == 1


def test_sec_and_gdelt_tasks_record_provider_identity_with_fake_providers(monkeypatch) -> None:
    _store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_sec_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeSECEdgarProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )
    monkeypatch.setattr(
        provider_data_tasks,
        "_gdelt_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeGDELTProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )

    sec_result = provider_data_tasks.run_sec_company_ingestion(["320193"])
    gdelt_result = provider_data_tasks.run_gdelt_raw_ingestion(query="bank stress", lookback_days=3)

    assert sec_result["provider"] == "sec-edgar"
    assert sec_result["run_type"] == "company_filings"
    assert sec_result["network_called"] is False
    assert sec_result["inserted"] == 2
    assert gdelt_result["provider"] == "gdelt"
    assert gdelt_result["run_type"] == "raw_items"
    assert gdelt_result["network_called"] is False
    assert gdelt_result["inserted"] == 3


def test_sanctions_task_invokes_ingestion_service_with_fake_provider(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_ofac_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeSanctionsProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )

    result = provider_data_tasks.run_sanctions_ingestion(program="CYBER2")

    assert result["status"] == "ok"
    assert result["provider"] == "ofac"
    assert result["run_type"] == "sanctions_entities"
    assert result["mode"] == "test_fake_ingestion"
    assert result["network_called"] is False
    assert result["fetched"] == 1
    assert result["inserted"] == 1
    assert len([item for item in store.values() if isinstance(item, SanctionsEntity)]) == 1
    assert len([item for item in store.values() if isinstance(item, RawIngestionItem)]) == 1


def test_entity_identity_task_invokes_ingestion_service_with_fake_provider(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_gleif_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeEntityIdentityProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )

    result = provider_data_tasks.run_entity_identity_ingestion(
        curated_watchlist=["Example Financial"],
        country_code="US",
    )

    assert result["status"] == "ok"
    assert result["provider"] == "gleif"
    assert result["run_type"] == "entity_identity"
    assert result["network_called"] is False
    assert result["fetched"] == 1
    assert result["inserted"] == 1
    # The curated name resolves to one profile; the unresolved parent LEI mints nothing.
    assert len([item for item in store.values() if isinstance(item, EntityProfile)]) == 1
    assert len([item for item in store.values() if isinstance(item, RawIngestionItem)]) == 2


def test_country_indicator_task_invokes_ingestion_service_with_fake_provider(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_world_bank_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeCountryIndicatorProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )

    result = provider_data_tasks.run_country_indicator_ingestion(
        country_codes=["USA"],
        indicator_ids=["NY.GDP.MKTP.CD"],
        start_year=2025,
        limit=2,
    )

    assert result["status"] == "ok"
    assert result["provider"] == "world-bank"
    assert result["run_type"] == "country_indicators"
    assert result["network_called"] is False
    assert result["fetched"] == 2
    assert result["inserted"] == 2
    observations = [item for item in store.values() if isinstance(item, CountryIndicatorObservation)]
    assert len(observations) == 2


def test_humanitarian_geo_and_energy_tasks_invoke_fake_providers(monkeypatch) -> None:
    store, _sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_reliefweb_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeHumanitarianProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )
    monkeypatch.setattr(
        provider_data_tasks,
        "_geo_provider_binding",
        lambda source: provider_data_tasks.ProviderBinding(
            provider=FakeGeoIncidentProvider(),
            mode=f"test_fake_ingestion:{source}",
            network_called=False,
        ),
    )
    monkeypatch.setattr(
        provider_data_tasks,
        "_eia_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=FakeEnergyProvider(),
            mode="test_fake_ingestion",
            network_called=False,
        ),
    )
    monkeypatch.setattr(
        provider_data_tasks,
        "_utc_now",
        lambda: datetime.datetime(2026, 1, 8, tzinfo=datetime.UTC),
    )

    humanitarian = provider_data_tasks.run_humanitarian_report_ingestion(
        query="flood",
        country_code="USA",
    )
    geo = provider_data_tasks.run_geo_incident_ingestion(region="US", lookback_days=7)
    energy = provider_data_tasks.run_energy_series_ingestion(series_ids=["PET.WCRSTUS1.W"])

    assert humanitarian["provider"] == "reliefweb"
    assert humanitarian["run_type"] == "humanitarian_reports"
    assert humanitarian["network_called"] is False
    assert humanitarian["inserted"] == 1
    assert geo["provider"] == "usgs"
    assert geo["run_type"] == "geo_incidents"
    assert geo["network_called"] is False
    assert geo["inserted"] == 1
    assert energy["provider"] == "eia"
    assert energy["run_type"] == "energy_series"
    assert energy["network_called"] is False
    assert energy["inserted"] == 3
    assert len([item for item in store.values() if isinstance(item, HumanitarianReport)]) == 1
    assert len([item for item in store.values() if isinstance(item, GeoIncident)]) == 1
    assert len([item for item in store.values() if isinstance(item, EnergyMarketSnapshot)]) == 3


def test_energy_task_records_config_skip_without_provider_network(monkeypatch) -> None:
    store, sessions = _install_fake_session(monkeypatch)
    monkeypatch.setattr(
        provider_data_tasks,
        "_eia_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="EIA_API_KEY is not configured",
        ),
    )

    result = provider_data_tasks.run_energy_series_ingestion(["PET.WCRSTUS1.W"])

    assert result["status"] == "ok"
    assert result["state"] == "succeeded"
    assert result["provider"] == "eia"
    assert result["run_type"] == "energy_series"
    assert result["item_count"] == 0
    assert result["client_available"] is False
    assert result["mode"] == "configuration_skipped"
    assert result["network_called"] is False
    assert result["unavailable_reason"] == "EIA_API_KEY is not configured"
    assert len(store) == 1
    assert sessions[-1].commits == 1
    assert sessions[-1].closed is True
