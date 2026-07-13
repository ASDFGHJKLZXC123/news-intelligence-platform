"""Provider contract tests for GDELT, FRED, and SEC EDGAR."""

from __future__ import annotations

import datetime
import json
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from packages.providers.base import (
    FREDObservation,
    FREDProvider,
    GDELTArticle,
    GDELTProvider,
    SECCompanyFact,
    SECEdgarProvider,
)
from packages.providers.fakes import FakeFREDProvider, FakeGDELTProvider, FakeSECEdgarProvider
from packages.providers.fred import FREDClient
from packages.providers.gdelt import GDELTClient
from packages.providers.sec_edgar import SECEdgarClient


class BytesResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
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
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.targets: list[str | Request] = []
        self.timeouts: list[float | None] = []

    def __call__(self, target: str | Request, timeout: float | None = None) -> BytesResponse:
        self.targets.append(target)
        self.timeouts.append(timeout)
        return BytesResponse(self.payloads.pop(0))


def test_new_provider_dtos_normalize_utc_and_freeze_metadata() -> None:
    metadata = {"nested": {"tags": ["banking"]}}
    article = GDELTArticle(
        guid="a-1",
        title="Headline",
        url="https://example.com/a-1",
        seen_at=datetime.datetime(2026, 1, 1, 12, 30, tzinfo=datetime.UTC),
        metadata=metadata,
    )

    metadata["nested"]["tags"].append("mutated")

    assert article.seen_at.tzinfo is datetime.UTC
    assert article.metadata["nested"]["tags"] == ("banking",)
    assert article.schema_version == "gdelt-article.v1"
    with pytest.raises(TypeError):
        article.metadata["nested"]["tags"] += ("other",)  # type: ignore[index,operator]
    with pytest.raises(ValueError):
        FREDObservation(
            series_id="GDP",
            observed_at=datetime.datetime(2026, 1, 1),
            value=1.0,
        )
    with pytest.raises(ValueError):
        SECCompanyFact(
            cik="0000320193",
            taxonomy="us-gaap",
            concept="Assets",
            unit="USD",
            value=1,
            accession_number="a",
            filed_at=datetime.datetime(
                2026, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
            ),
        )


def test_new_fakes_and_clients_satisfy_protocols() -> None:
    assert isinstance(FakeGDELTProvider(), GDELTProvider)
    assert isinstance(FakeFREDProvider(), FREDProvider)
    assert isinstance(FakeSECEdgarProvider(), SECEdgarProvider)
    assert isinstance(GDELTClient(urlopen=RecordingTransport({"articles": []})), GDELTProvider)
    assert isinstance(
        FREDClient(api_key="test-key", urlopen=RecordingTransport({"observations": []})),
        FREDProvider,
    )
    assert isinstance(
        SECEdgarClient(user_agent="news-intel tests@example.com", urlopen=RecordingTransport()),
        SECEdgarProvider,
    )


def test_deterministic_fakes_return_stable_results() -> None:
    gdelt = FakeGDELTProvider()
    assert gdelt.search_articles("bank stress") == gdelt.search_articles("credit stress")
    assert gdelt.search_events("bank stress") == gdelt.search_events("bank stress")

    fred = FakeFREDProvider()
    assert fred.fetch_series_observations("GDP") == fred.fetch_series_observations("GDP")
    filtered = fred.fetch_series_observations(
        "GDP",
        observation_start=datetime.date(2026, 1, 2),
        limit=1,
    )
    assert len(filtered) == 1
    assert filtered[0].date == datetime.date(2026, 1, 2)

    sec = FakeSECEdgarProvider()
    assert sec.fetch_submissions("320193") == sec.fetch_submissions("0000320193")
    assert sec.fetch_company_facts("320193") == sec.fetch_company_facts("0000320193")


def test_gdelt_client_builds_queries_and_parses_articles_and_events() -> None:
    transport = RecordingTransport(
        {
            "articles": [
                {
                    "url": "https://example.com/article",
                    "title": "Policy shift",
                    "seendate": "20260102093000",
                    "domain": "example.com",
                    "language": "English",
                    "sourcecountry": "US",
                }
            ]
        },
        {
            "events": [
                {
                    "GlobalEventID": "12345",
                    "DATEADDED": "20260102094500",
                    "EventCode": "042",
                    "EventRootCode": "04",
                    "Actor1Name": "BANK",
                    "Actor2Name": "REGULATOR",
                    "ActionGeo_CountryCode": "US",
                    "QuadClass": "1",
                    "GoldsteinScale": "1.9",
                    "AvgTone": "-0.4",
                    "SOURCEURL": "https://example.com/event",
                }
            ]
        },
    )
    client = GDELTClient(urlopen=transport)
    start_at = datetime.datetime(2026, 1, 2, 9, tzinfo=datetime.UTC)
    end_at = datetime.datetime(2026, 1, 2, 10, tzinfo=datetime.UTC)

    articles = client.search_articles("central bank", start_at=start_at, end_at=end_at)
    events = client.search_events("central bank", max_records=10)

    article_url = str(transport.targets[0])
    article_query = parse_qs(urlparse(article_url).query)
    assert article_query["query"] == ["central bank"]
    assert article_query["mode"] == ["ArtList"]
    assert article_query["format"] == ["json"]
    assert article_query["startdatetime"] == ["20260102090000"]
    assert article_query["enddatetime"] == ["20260102100000"]
    assert articles[0].seen_at == datetime.datetime(2026, 1, 2, 9, 30, tzinfo=datetime.UTC)
    assert articles[0].evidence_refs == ("https://example.com/article",)
    assert articles[0].metadata["domain"] == "example.com"

    event_url = str(transport.targets[1])
    event_query = parse_qs(urlparse(event_url).query)
    assert event_query["maxrecords"] == ["10"]
    assert events[0].global_event_id == "12345"
    assert events[0].event_at == datetime.datetime(2026, 1, 2, 9, 45, tzinfo=datetime.UTC)
    assert events[0].quad_class == 1
    assert events[0].goldstein_scale == 1.9
    assert events[0].avg_tone == -0.4


def test_fred_client_requires_key_builds_query_and_parses_observations() -> None:
    transport = RecordingTransport(
        {
            "observations": [
                {
                    "date": "2026-01-01",
                    "value": "123.45",
                    "realtime_start": "2026-01-02",
                    "realtime_end": "2026-01-03",
                },
                {
                    "date": "2026-01-02",
                    "value": ".",
                    "realtime_start": "2026-01-02",
                    "realtime_end": "2026-01-03",
                },
            ]
        }
    )
    client = FREDClient(api_key="test-key", urlopen=transport)

    observations = client.fetch_series_observations(
        "GDP",
        observation_start=datetime.date(2026, 1, 1),
        observation_end=datetime.date(2026, 1, 2),
        limit=2,
    )

    query = parse_qs(urlparse(str(transport.targets[0])).query)
    assert query["series_id"] == ["GDP"]
    assert query["api_key"] == ["test-key"]
    assert query["file_type"] == ["json"]
    assert query["observation_start"] == ["2026-01-01"]
    assert query["observation_end"] == ["2026-01-02"]
    assert query["limit"] == ["2"]
    assert observations[0].observed_at == datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert observations[0].value == 123.45
    assert observations[1].value is None
    assert observations[0].metadata["value"] == "123.45"
    with pytest.raises(ValueError):
        FREDClient(api_key="")


def test_sec_client_sets_user_agent_builds_urls_and_parses_payloads() -> None:
    transport = RecordingTransport(
        {
            "name": "Apple Inc.",
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-26-000001"],
                    "filingDate": ["2026-01-30"],
                    "reportDate": ["2025-12-31"],
                    "form": ["10-Q"],
                    "primaryDocument": ["aapl-20251231.htm"],
                    "primaryDocDescription": ["Quarterly report"],
                    "items": [""],
                }
            },
        },
        {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "label": "Assets",
                        "description": "Total assets",
                        "units": {
                            "USD": [
                                {
                                    "end": "2025-12-31",
                                    "val": 1000,
                                    "accn": "0000320193-26-000001",
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-02-01",
                                    "frame": "CY2025",
                                }
                            ]
                        },
                    }
                }
            }
        },
    )
    user_agent = "news-intel tests@example.com"
    client = SECEdgarClient(user_agent=user_agent, urlopen=transport)

    submissions = client.fetch_submissions("320193")
    facts = client.fetch_company_facts("0000320193")

    first_request = transport.targets[0]
    second_request = transport.targets[1]
    assert isinstance(first_request, Request)
    assert isinstance(second_request, Request)
    assert first_request.full_url.endswith("/submissions/CIK0000320193.json")
    assert second_request.full_url.endswith("/api/xbrl/companyfacts/CIK0000320193.json")
    assert first_request.get_header("User-agent") == user_agent
    assert second_request.get_header("User-agent") == user_agent

    assert submissions[0].cik == "0000320193"
    assert submissions[0].company_name == "Apple Inc."
    assert submissions[0].filed_at == datetime.datetime(2026, 1, 30, tzinfo=datetime.UTC)
    assert submissions[0].report_at == datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC)
    assert submissions[0].evidence_refs[0].endswith(
        "/320193/000032019326000001/aapl-20251231.htm"
    )

    assert facts[0].taxonomy == "us-gaap"
    assert facts[0].concept == "Assets"
    assert facts[0].value == 1000
    assert facts[0].filed_at == datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
    assert facts[0].period_end_at == datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC)
    assert facts[0].metadata["frame"] == "CY2025"
    with pytest.raises(ValueError):
        SECEdgarClient(user_agent="")
