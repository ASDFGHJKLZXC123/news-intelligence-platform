"""Provider expansion contract, fake, and stdlib client tests."""

from __future__ import annotations

import datetime
import json
from types import MappingProxyType, TracebackType
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from packages.providers.base import (
    CountryIndicatorProvider,
    EnergyObservation,
    EnergyProvider,
    EntityIdentityProvider,
    GeoIncident,
    GeoIncidentProvider,
    HumanitarianProvider,
    SanctionsAlias,
    SanctionsEntity,
    SanctionsProvider,
)
from packages.providers.eia import EIAClient
from packages.providers.fakes import (
    FakeCountryIndicatorProvider,
    FakeEnergyProvider,
    FakeEntityIdentityProvider,
    FakeGeoIncidentProvider,
    FakeHumanitarianProvider,
    FakeSanctionsProvider,
)
from packages.providers.gleif import GLEIFClient
from packages.providers.nasa_firms import NASAFIRMSClient
from packages.providers.ofac import OFACClient
from packages.providers.reliefweb import ReliefWebClient
from packages.providers.usgs import USGSEarthquakeClient
from packages.providers.world_bank import WorldBankClient


class BytesResponse:
    def __init__(self, payload: Any) -> None:
        if isinstance(payload, bytes):
            self._body = payload
        elif isinstance(payload, str):
            self._body = payload.encode("utf-8")
        else:
            self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> BytesResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class RecordingTransport:
    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.targets: list[str | Request] = []

    def __call__(self, target: str | Request) -> BytesResponse:
        self.targets.append(target)
        return BytesResponse(self.payloads.pop(0))


def _target_url(target: str | Request) -> str:
    return target.full_url if isinstance(target, Request) else str(target)


def test_expansion_fakes_and_clients_satisfy_protocols() -> None:
    assert isinstance(FakeSanctionsProvider(), SanctionsProvider)
    assert isinstance(FakeEntityIdentityProvider(), EntityIdentityProvider)
    assert isinstance(FakeCountryIndicatorProvider(), CountryIndicatorProvider)
    assert isinstance(FakeHumanitarianProvider(), HumanitarianProvider)
    assert isinstance(FakeGeoIncidentProvider(), GeoIncidentProvider)
    assert isinstance(FakeEnergyProvider(), EnergyProvider)

    assert isinstance(OFACClient(urlopen=RecordingTransport({"entities": []})), SanctionsProvider)
    assert isinstance(GLEIFClient(urlopen=RecordingTransport({"data": []})), EntityIdentityProvider)
    assert isinstance(WorldBankClient(urlopen=RecordingTransport([{}, []])), CountryIndicatorProvider)
    assert isinstance(ReliefWebClient(urlopen=RecordingTransport({"data": []})), HumanitarianProvider)
    assert isinstance(USGSEarthquakeClient(urlopen=RecordingTransport({"features": []})), GeoIncidentProvider)
    assert isinstance(NASAFIRMSClient(urlopen=RecordingTransport([])), GeoIncidentProvider)
    assert isinstance(EIAClient(api_key="test-key", urlopen=RecordingTransport({})), EnergyProvider)


def test_expansion_dtos_freeze_metadata_and_normalize_utc() -> None:
    alias_metadata = {"nested": {"tags": ["sanctions"]}}
    entity_metadata = {"source": {"rows": [1]}}
    entity = SanctionsEntity(
        entity_id="ofac:1",
        name="Blocked Co",
        programs=["CYBER2"],
        aliases=[SanctionsAlias(name="Blocked Alias", metadata=alias_metadata)],
        updated_at=datetime.datetime(2026, 1, 4, tzinfo=datetime.UTC),
        metadata=entity_metadata,
    )

    alias_metadata["nested"]["tags"].append("mutated")
    entity_metadata["source"]["rows"].append(2)

    assert entity.programs == ("CYBER2",)
    assert entity.updated_at == datetime.datetime(2026, 1, 4, tzinfo=datetime.UTC)
    assert entity.schema_version == "sanctions-entity.v1"
    assert isinstance(entity.metadata, MappingProxyType)
    assert entity.metadata["source"]["rows"] == (1,)
    assert entity.aliases[0].metadata["nested"]["tags"] == ("sanctions",)
    with pytest.raises(TypeError):
        entity.metadata["source"]["rows"] += (2,)  # type: ignore[index,operator]
    with pytest.raises(ValueError):
        GeoIncident(
            incident_id="bad",
            incident_type="earthquake",
            title="Bad timezone",
            occurred_at=datetime.datetime(2026, 1, 1),
            latitude=1,
            longitude=2,
        )
    with pytest.raises(ValueError):
        EnergyObservation(
            series_id="bad",
            observed_at=datetime.datetime(
                2026, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
            ),
            value=1,
        )


def test_expansion_fakes_are_deterministic_and_filterable() -> None:
    sanctions = FakeSanctionsProvider()
    assert sanctions.fetch_entities() == sanctions.fetch_entities()
    assert sanctions.fetch_deltas() == sanctions.fetch_deltas()
    assert sanctions.fetch_entities(program="CYBER2")
    assert sanctions.fetch_entities(program="NOPE") == []

    identity = FakeEntityIdentityProvider()
    assert identity.search_records("bank") == identity.search_records("other")
    assert identity.fetch_relationships("5493001KJTIIGC8Y1R12")

    indicators = FakeCountryIndicatorProvider()
    observations = indicators.fetch_indicator_observations(
        "USA", "NY.GDP.MKTP.CD", start_year=2025, limit=1
    )
    assert observations[0].date.year == 2025
    assert indicators.fetch_indicator_metadata("NY.GDP.MKTP.CD") is not None

    humanitarian = FakeHumanitarianProvider()
    assert humanitarian.search_reports("flood") == humanitarian.search_reports("conflict")
    assert humanitarian.search_reports("flood", country_code="USA")

    incidents = FakeGeoIncidentProvider()
    assert incidents.search_incidents(region="US") == incidents.search_incidents(region="US")

    energy = FakeEnergyProvider()
    assert energy.fetch_series("PET.WCRSTUS1.W") == energy.fetch_series("PET.WCRSTUS1.W")
    assert energy.fetch_series_observations("PET.WCRSTUS1.W", limit=1)[0].value == 420000.0


def test_ofac_client_builds_query_and_parses_json_entities() -> None:
    transport = RecordingTransport(
        {
            "sdnList": {
                "sdnEntry": [
                    {
                        "uid": "1001",
                        "sdnName": "Blocked Co",
                        "sdnType": "Entity",
                        "programList": {"program": ["CYBER2"]},
                        "akaList": {"aka": [{"name": "Blocked Alias", "quality": "strong"}]},
                        "idList": {
                            "id": [
                                {
                                    "idType": "Tax ID",
                                    "idNumber": "12-3456789",
                                    "idCountry": "US",
                                }
                            ]
                        },
                        "addressList": {"address": [{"country": "US"}]},
                        "lastUpdated": "2026-01-04T08:00:00Z",
                    }
                ]
            }
        }
    )
    client = OFACClient(urlopen=transport, entities_endpoint="https://example.com/ofac")

    entities = client.fetch_entities(
        program="CYBER2",
        updated_since=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        limit=5,
    )

    query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    assert query["program"] == ["CYBER2"]
    assert query["limit"] == ["5"]
    assert query["updated_since"] == ["2026-01-01T00:00:00+00:00"]
    assert entities[0].entity_id == "1001"
    assert entities[0].programs == ("CYBER2",)
    assert entities[0].aliases[0].name == "Blocked Alias"
    assert entities[0].identifiers[0].value == "12-3456789"
    assert entities[0].countries == ("US",)


def test_ofac_client_parses_simple_xml_entities() -> None:
    transport = RecordingTransport(
        """
        <sdnList>
          <sdnEntry>
            <uid>2002</uid>
            <sdnName>XML Blocked Co</sdnName>
            <sdnType>Entity</sdnType>
            <programList><program>UKRAINE-EO13662</program></programList>
          </sdnEntry>
        </sdnList>
        """
    )
    entities = OFACClient(urlopen=transport).fetch_entities()
    assert entities[0].entity_id == "2002"
    assert entities[0].programs == ("UKRAINE-EO13662",)


def test_gleif_client_builds_queries_and_parses_records_and_relationships() -> None:
    transport = RecordingTransport(
        {
            "data": [
                {
                    "id": "5493001KJTIIGC8Y1R12",
                    "attributes": {
                        "entity": {
                            "legalName": {"name": "Example Financial Holdings Inc."},
                            "status": "ACTIVE",
                            "legalAddress": {"country": "US"},
                            "jurisdiction": "US-DE",
                            "legalForm": {"id": "XTIQ"},
                        },
                        "registration": {
                            "status": "ISSUED",
                            "lastUpdateDate": "2026-01-05T09:00:00Z",
                        },
                    },
                    "links": {"self": "https://example.com/lei/5493001KJTIIGC8Y1R12"},
                }
            ]
        },
        {
            "data": [
                {
                    "id": "rel-1",
                    "attributes": {
                        "relationship": {
                            "type": "DIRECT_PARENT",
                            "startNode": {"nodeID": "5493001KJTIIGC8Y1R12"},
                            "endNode": {"nodeID": "213800D1EI4B9WTWWD28"},
                        },
                        "status": "ACTIVE",
                        "periods": [{"startDate": "2026-01-01"}],
                    },
                }
            ]
        },
    )
    client = GLEIFClient(urlopen=transport)

    records = client.search_records("Example Financial", country_code="US", limit=2)
    relationships = client.fetch_relationships(
        "5493001KJTIIGC8Y1R12", relationship_type="DIRECT_PARENT", limit=1
    )

    record_query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    assert record_query["filter[entity.legalName]"] == ["Example Financial"]
    assert record_query["filter[entity.legalAddress.country]"] == ["US"]
    assert record_query["page[size]"] == ["2"]
    assert records[0].legal_name == "Example Financial Holdings Inc."
    assert records[0].last_updated_at == datetime.datetime(2026, 1, 5, 9, tzinfo=datetime.UTC)

    relationship_query = parse_qs(urlparse(_target_url(transport.targets[1])).query)
    assert relationship_query["filter[relationship.startNode.nodeID]"] == [
        "5493001KJTIIGC8Y1R12"
    ]
    assert relationship_query["filter[relationship.type]"] == ["DIRECT_PARENT"]
    assert relationships[0].related_lei == "213800D1EI4B9WTWWD28"


def test_world_bank_client_builds_queries_and_parses_indicators() -> None:
    transport = RecordingTransport(
        [
            {"page": 1},
            [
                {
                    "id": "NY.GDP.MKTP.CD",
                    "name": "GDP (current US$)",
                    "source": {"value": "World Development Indicators"},
                    "unit": "USD",
                    "topics": [{"value": "Economy"}],
                }
            ],
        ],
        [
            {"page": 1},
            [
                {
                    "countryiso3code": "USA",
                    "country": {"value": "United States"},
                    "indicator": {"id": "NY.GDP.MKTP.CD"},
                    "date": "2025",
                    "value": "100.5",
                    "unit": "USD",
                }
            ],
        ],
    )
    client = WorldBankClient(urlopen=transport)

    indicator = client.fetch_indicator_metadata("NY.GDP.MKTP.CD")
    observations = client.fetch_indicator_observations(
        "USA", "NY.GDP.MKTP.CD", start_year=2025, end_year=2025, limit=1
    )

    metadata_query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    observation_query = parse_qs(urlparse(_target_url(transport.targets[1])).query)
    assert metadata_query["format"] == ["json"]
    assert observation_query["date"] == ["2025:2025"]
    assert observation_query["per_page"] == ["1"]
    assert indicator is not None
    assert indicator.topics == ("Economy",)
    assert observations[0].observed_at == datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
    assert observations[0].value == 100.5


def test_reliefweb_client_builds_query_and_parses_reports() -> None:
    transport = RecordingTransport(
        {
            "data": [
                {
                    "id": "rw-1",
                    "fields": {
                        "title": "Flooding situation update",
                        "url": "https://example.com/reliefweb/rw-1",
                        "date.created": "2026-01-06T07:30:00Z",
                        "date.changed": "2026-01-06T08:00:00Z",
                        "source": [{"name": "Example Relief Agency"}],
                        "country": [{"iso3": "USA"}],
                        "disaster_type": [{"name": "Flood"}],
                        "theme": [{"name": "Coordination"}],
                        "body": "Situation update body",
                    },
                }
            ]
        }
    )
    client = ReliefWebClient(app_name="tests", urlopen=transport)

    reports = client.search_reports("flood", country_code="USA", disaster_type="Flood", limit=3)

    query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    assert query["appname"] == ["tests"]
    assert query["query[value]"] == ["flood"]
    assert query["filter[country.iso3]"] == ["USA"]
    assert query["filter[disaster_type.name]"] == ["Flood"]
    assert reports[0].published_at == datetime.datetime(2026, 1, 6, 7, 30, tzinfo=datetime.UTC)
    assert reports[0].country_codes == ("USA",)
    assert reports[0].themes == ("Coordination",)


def test_usgs_client_builds_query_and_parses_geojson() -> None:
    transport = RecordingTransport(
        {
            "features": [
                {
                    "id": "usgs-1",
                    "properties": {
                        "title": "M 4.5 - Example Region",
                        "time": 1767776100000,
                        "mag": 4.5,
                        "place": "Example Region",
                        "url": "https://example.com/usgs/usgs-1",
                        "alert": "green",
                    },
                    "geometry": {"coordinates": [-122.2, 38.1, 10.0]},
                }
            ]
        }
    )
    client = USGSEarthquakeClient(urlopen=transport)

    incidents = client.search_incidents(
        start_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        end_at=datetime.datetime(2026, 1, 8, tzinfo=datetime.UTC),
        region="US",
        min_magnitude=4.0,
        bbox=(30.0, -130.0, 45.0, -110.0),
        limit=1,
    )

    query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    assert query["format"] == ["geojson"]
    assert query["minmagnitude"] == ["4.0"]
    assert query["minlatitude"] == ["30.0"]
    assert query["limit"] == ["1"]
    assert incidents[0].incident_id == "usgs-1"
    assert incidents[0].latitude == 38.1
    assert incidents[0].longitude == -122.2
    assert incidents[0].depth_km == 10.0
    assert incidents[0].severity == "green"


def test_nasa_firms_client_builds_query_and_parses_json_fires() -> None:
    transport = RecordingTransport(
        {
            "fires": [
                {
                    "id": "fire-1",
                    "latitude": "38.1",
                    "longitude": "-122.2",
                    "acq_date": "2026-01-07",
                    "acq_time": "0615",
                    "confidence": "high",
                    "frp": "12.5",
                    "satellite": "N",
                    "instrument": "VIIRS",
                }
            ]
        }
    )
    client = NASAFIRMSClient(map_key="test-map-key", urlopen=transport)

    incidents = client.search_incidents(
        start_at=datetime.datetime(2026, 1, 7, tzinfo=datetime.UTC),
        end_at=datetime.datetime(2026, 1, 8, tzinfo=datetime.UTC),
        region="world",
        min_confidence=50,
        limit=1,
    )

    query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    assert query["map_key"] == ["test-map-key"]
    assert query["source"] == ["VIIRS_SNPP_NRT"]
    assert query["area"] == ["world"]
    assert query["start"] == ["2026-01-07"]
    assert query["min_confidence"] == ["50"]
    assert incidents[0].incident_type == "fire"
    assert incidents[0].occurred_at == datetime.datetime(2026, 1, 7, 6, 15, tzinfo=datetime.UTC)
    assert incidents[0].magnitude == 12.5


def test_eia_client_builds_query_and_parses_series_and_observations() -> None:
    payload = {
        "response": {
            "description": "Weekly U.S. Ending Stocks of Crude Oil",
            "units": "Thousand Barrels",
            "frequency": "weekly",
            "data": [
                {
                    "series": "PET.WCRSTUS1.W",
                    "period": "2026-01-02",
                    "value": "420000",
                    "units": "Thousand Barrels",
                    "area-name": "USA",
                }
            ],
        }
    }
    transport = RecordingTransport(payload, payload)
    client = EIAClient(api_key="test-key", urlopen=transport)

    series = client.fetch_series("PET.WCRSTUS1.W")
    observations = client.fetch_series_observations(
        "PET.WCRSTUS1.W",
        start_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        end_at=datetime.datetime(2026, 1, 3, tzinfo=datetime.UTC),
        limit=1,
    )

    series_query = parse_qs(urlparse(_target_url(transport.targets[0])).query)
    observation_query = parse_qs(urlparse(_target_url(transport.targets[1])).query)
    assert series_query["api_key"] == ["test-key"]
    assert observation_query["start"] == ["2026-01-01"]
    assert observation_query["end"] == ["2026-01-03"]
    assert observation_query["length"] == ["1"]
    assert series.units == "Thousand Barrels"
    assert observations[0].observed_at == datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)
    assert observations[0].value == 420000.0
    with pytest.raises(ValueError):
        EIAClient(api_key="")
