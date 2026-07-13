"""Unit tests for the ADR 0006 item 2B Wikidata provider boundary: bounded queries and parsing."""

from __future__ import annotations

import datetime
import json
from types import TracebackType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from packages.providers.base import WikidataEntity, WikidataProvider, normalize_wikidata_qid
from packages.providers.fakes import FakeWikidataProvider
from packages.providers.wikidata import (
    DEFAULT_MAX_VALUES,
    WikidataClient,
    WikidataError,
    build_entity_query,
    build_resolve_query,
    sparql_literal,
)

APPLE_LEI = "HWUPKR0MPOU8FGXBT394"


class BytesResponse:
    def __init__(self, payload: Any) -> None:
        self._body = (
            json.dumps(payload).encode("utf-8")
            if not isinstance(payload, str)
            else payload.encode("utf-8")
        )

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
    """A urlopen stand-in that records every request and the timeout it was given."""

    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []

    def __call__(self, target: Request, timeout: float | None = None) -> BytesResponse:
        self.requests.append(target)
        self.timeouts.append(timeout)
        return BytesResponse(self.payloads.pop(0))


class FailingTransport:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __call__(self, target: Request, timeout: float | None = None) -> BytesResponse:
        raise self._error


def _client(transport: Any, **kwargs: Any) -> WikidataClient:
    return WikidataClient(
        user_agent="news-intelligence-platform/0.1 (ops@example.com)", urlopen=transport, **kwargs
    )


def _query_of(request: Request) -> str:
    return parse_qs(urlparse(request.full_url).query)["query"][0]


def _uri(qid: str) -> dict[str, str]:
    return {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"}


def _literal(value: str, language: str | None = None) -> dict[str, str]:
    cell = {"type": "literal", "value": value}
    if language is not None:
        cell["xml:lang"] = language
    return cell


def _rows(*bindings: dict[str, Any]) -> dict[str, Any]:
    return {"head": {"vars": []}, "results": {"bindings": list(bindings)}}


# --- Contract -------------------------------------------------------------------------
def test_client_and_fake_satisfy_the_provider_protocol() -> None:
    assert isinstance(_client(RecordingTransport(_rows())), WikidataProvider)
    assert isinstance(FakeWikidataProvider(), WikidataProvider)


def test_client_requires_a_descriptive_user_agent() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        WikidataClient(user_agent="   ")


# --- Bounded query construction -------------------------------------------------------
def test_entity_query_pins_explicit_qids_into_values_and_caps_rows() -> None:
    transport = RecordingTransport(_rows())
    client = _client(transport, endpoint="https://example.test/sparql", timeout=12.5)

    client.fetch_entities(["Q95", "Q312", "q312"])

    request = transport.requests[0]
    query = _query_of(request)
    assert request.full_url.startswith("https://example.test/sparql?")
    assert "VALUES ?item { wd:Q312 wd:Q95 }" in query
    assert query.rstrip().endswith("LIMIT 10000")
    # Every ADR 0006 property the pull depends on is in the one bounded query.
    for property_id in ("P249", "P1278", "P5531", "P749", "P355", "P452", "P1448", "P1716"):
        assert f":{property_id} " in query
    assert request.headers["User-agent"] == "news-intelligence-platform/0.1 (ops@example.com)"
    assert request.headers["Accept"] == "application/sparql-results+json"
    # The transport is always given an explicit timeout; no request can hang forever.
    assert transport.timeouts == [12.5]


def test_resolve_query_pins_seeded_identifiers_and_asks_for_both_cik_spellings() -> None:
    transport = RecordingTransport(_rows())
    client = _client(transport)

    client.resolve_qids(ciks=["320193"], leis=[APPLE_LEI])

    query = _query_of(transport.requests[0])
    # Wikidata stores CIKs both zero-padded and bare, so both forms are pinned.
    assert 'VALUES ?value { "0000320193" "320193" }' in query
    assert f'VALUES ?value {{ "{APPLE_LEI}" }}' in query
    assert "wdt:P5531" in query
    assert "wdt:P1278" in query


def test_no_query_is_issued_without_bounded_inputs() -> None:
    transport = RecordingTransport()
    client = _client(transport)

    assert client.fetch_entities([]) == []
    assert client.resolve_qids() == []
    assert transport.requests == []


def test_a_query_never_carries_more_values_than_the_bound() -> None:
    client = _client(RecordingTransport(_rows()))

    with pytest.raises(ValueError, match="at most"):
        client.fetch_entities([f"Q{index + 1}" for index in range(DEFAULT_MAX_VALUES + 1)])


# --- Escaping and injection defence ---------------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    [
        "Q312 } UNION { ?item ?p ?o } #",
        "?item",
        "*",
        "Q0",
        "P31",
        "",
    ],
)
def test_only_syntactic_qids_reach_a_query(hostile: str) -> None:
    client = _client(RecordingTransport(_rows()))

    with pytest.raises(ValueError, match="QID"):
        client.fetch_entities([hostile])
    with pytest.raises(ValueError, match="QID"):
        normalize_wikidata_qid(hostile)


def test_identifier_literals_are_escaped_not_interpolated() -> None:
    hostile = '" } UNION { ?item ?p ?o } #'

    assert sparql_literal(hostile) == '"\\" } UNION { ?item ?p ?o } #"'
    assert sparql_literal("back\\slash") == '"back\\\\slash"'
    query = build_resolve_query([], [hostile], limit=10)
    # The hostile text survives only as an escaped literal: the query still has one VALUES block.
    assert query.count("VALUES") == 1
    assert '"\\" } UNION' in query

    with pytest.raises(ValueError, match="control characters"):
        sparql_literal("bell\x07")


def test_a_resolve_query_needs_at_least_one_value() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_resolve_query([], [], limit=10)


def test_entity_query_rejects_a_non_qid_value() -> None:
    with pytest.raises(ValueError, match="QID"):
        build_entity_query(["Q312", "DROP"], limit=10)


# --- DTO parsing ----------------------------------------------------------------------
def test_long_format_rows_parse_into_one_identity_dto_per_item() -> None:
    payload = _rows(
        {"item": _uri("Q312"), "field": _literal("label"), "value": _literal("Apple Inc.", "en")},
        {
            "item": _uri("Q312"),
            "field": _literal("description"),
            "value": _literal("tech company", "en"),
        },
        {
            "item": _uri("Q312"),
            "field": _literal("alias"),
            "value": _literal("Apple Computer", "en"),
        },
        {"item": _uri("Q312"), "field": _literal("short_name"), "value": _literal("Apple", "en")},
        {"item": _uri("Q312"), "field": _literal("ticker"), "value": _literal("aapl")},
        {"item": _uri("Q312"), "field": _literal("lei"), "value": _literal(APPLE_LEI)},
        # Wikidata stores the CIK unpadded here; the DTO normalizes it to the SEC form.
        {"item": _uri("Q312"), "field": _literal("cik"), "value": _literal("320193")},
        {
            "item": _uri("Q312"),
            "field": _literal("official_name"),
            "value": _literal("Apple Inc.", "en"),
            "start": _literal("+2007-01-09T00:00:00Z"),
        },
        {
            "item": _uri("Q312"),
            "field": _literal("official_name"),
            "value": _literal("Apple Computer, Inc.", "en"),
            "start": _literal("+1977-01-03T00:00:00Z"),
            "end": _literal("+2007-01-09T00:00:00Z"),
        },
        {
            "item": _uri("Q312"),
            "field": _literal("brand"),
            "value": _uri("Q2766"),
            "valueLabel": _literal("iPhone", "en"),
        },
        {
            "item": _uri("Q312"),
            "field": _literal("parent"),
            "value": _uri("Q95"),
            "valueLabel": _literal("Alphabet", "en"),
        },
        {"item": _uri("Q312"), "field": _literal("subsidiary"), "value": _uri("Q1")},
        {
            "item": _uri("Q312"),
            "field": _literal("industry"),
            "value": _uri("Q11661"),
            "valueLabel": _literal("IT", "en"),
        },
        # A row for an item nobody asked for is dropped rather than trusted.
        {"item": _uri("Q9999"), "field": _literal("label"), "value": _literal("Intruder", "en")},
    )
    client = _client(RecordingTransport(payload))

    [entity] = client.fetch_entities(["Q312"])

    assert (entity.qid, entity.label) == ("Q312", "Apple Inc.")
    assert entity.description == "tech company"
    assert (entity.tickers, entity.leis, entity.ciks) == (("AAPL",), (APPLE_LEI,), ("0000320193",))
    assert [(ref.qid, ref.label) for ref in entity.parents] == [("Q95", "Alphabet")]
    assert [(ref.qid, ref.label) for ref in entity.subsidiaries] == [("Q1", "")]
    assert [(ref.qid, ref.label) for ref in entity.industries] == [("Q11661", "IT")]

    by_type = {(alias.alias_type, alias.value): alias for alias in entity.aliases}
    # The label leads the alias list so its surface form wins its normalized key downstream.
    assert entity.aliases[0].value == "Apple Inc."
    assert entity.aliases[0].alias_type == "legal_name"
    assert ("colloquial", "Apple Computer") in by_type
    assert ("short_name", "Apple") in by_type
    assert ("ticker", "aapl") in by_type
    # An official name with an end date is a former name and keeps its interval; without one it
    # is the current legal name and stays open.
    former = by_type[("former_name", "Apple Computer, Inc.")]
    assert (former.valid_from, former.valid_to) == (
        datetime.date(1977, 1, 3),
        datetime.date(2007, 1, 9),
    )
    brand = by_type[("brand_product", "iPhone")]
    assert brand.qid == "Q2766"
    assert brand.valid_to is None
    # The raw bindings survive on the DTO so the service can retain them verbatim.
    assert len(entity.metadata["bindings"]) == 13
    assert entity.evidence_refs == ("https://www.wikidata.org/wiki/Q312",)


def test_qid_matches_normalize_the_identifier_they_resolved_on() -> None:
    payload = _rows(
        {"item": _uri("Q312"), "field": _literal("cik"), "value": _literal("320193")},
        {"item": _uri("Q95"), "field": _literal("lei"), "value": _literal(APPLE_LEI.lower())},
        {"item": _literal("not-an-item"), "field": _literal("cik"), "value": _literal("1")},
    )
    client = _client(RecordingTransport(payload))

    matches = client.resolve_qids(ciks=["0000320193"], leis=[APPLE_LEI])

    assert [(match.qid, match.identifier_type, match.identifier_value) for match in matches] == [
        ("Q312", "cik", "0000320193"),
        ("Q95", "lei", APPLE_LEI),
    ]


def test_imprecise_dates_and_empty_results_degrade_quietly() -> None:
    payload = _rows(
        {"item": _uri("Q312"), "field": _literal("label"), "value": _literal("Apple Inc.", "en")},
        {
            "item": _uri("Q312"),
            "field": _literal("official_name"),
            "value": _literal("Apple Computer, Inc.", "en"),
            # Wikidata expresses year-only precision with a zeroed month/day.
            "end": _literal("+2007-00-00T00:00:00Z"),
        },
    )
    client = _client(RecordingTransport(payload, _rows()))

    [entity] = client.fetch_entities(["Q312"])
    assert client.fetch_entities(["Q95"]) == []

    official = [alias for alias in entity.aliases if alias.value == "Apple Computer, Inc."]
    # An unparseable end date must not silently become a valid interval.
    assert [(alias.alias_type, alias.valid_to) for alias in official] == [("legal_name", None)]


# --- Error mapping --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (500, True), (403, False), (404, False)],
)
def test_http_failures_map_to_retry_tagged_provider_errors(status: int, retryable: bool) -> None:
    error = HTTPError("https://example.test/sparql", status, "boom", {}, None)  # type: ignore[arg-type]
    client = _client(FailingTransport(error))

    with pytest.raises(WikidataError) as caught:
        client.fetch_entities(["Q312"])

    assert caught.value.status == status
    assert caught.value.retryable is retryable


def test_transport_and_payload_failures_map_to_provider_errors() -> None:
    with pytest.raises(WikidataError) as network:
        _client(FailingTransport(URLError("timed out"))).fetch_entities(["Q312"])
    assert network.value.retryable is True

    with pytest.raises(WikidataError) as malformed:
        _client(RecordingTransport("<html>error</html>")).fetch_entities(["Q312"])
    assert malformed.value.retryable is False

    with pytest.raises(WikidataError, match="JSON object"):
        _client(RecordingTransport("[]")).fetch_entities(["Q312"])


# --- Fake -----------------------------------------------------------------------------
def test_fake_provider_answers_only_for_the_bounded_inputs_it_is_given() -> None:
    provider = FakeWikidataProvider()

    [entity] = provider.fetch_entities(["Q312"])
    assert isinstance(entity, WikidataEntity)
    assert entity.ciks == ("0000320193",)
    assert provider.fetch_entities(["Q95"]) == []

    matches = provider.resolve_qids(ciks=["0000320193"])
    assert [match.qid for match in matches] == ["Q312"]
    assert provider.resolve_qids(ciks=["0000000001"]) == []
    assert provider.resolve_qids() == []


def test_entity_dto_is_immutable_after_construction() -> None:
    bindings: dict[str, Any] = {"rows": [{"item": "Q312"}]}
    entity = WikidataEntity(qid="Q312", label="  Apple Inc. ", metadata=bindings, tickers=["AAPL"])

    bindings["rows"].append({"item": "Q95"})

    assert entity.label == "Apple Inc."
    assert entity.tickers == ("AAPL",)
    assert len(entity.metadata["rows"]) == 1
    assert entity.schema_version == "wikidata-entity.v1"
    with pytest.raises(AttributeError):
        entity.label = "mutated"  # type: ignore[misc]


# --- Ticker property shape (verified against the live endpoint) ------------------------
# Wikidata states a ticker as a qualifier on the stock-exchange (P414) statement, not as a
# truthy P249 claim: every large issuer checked live (Apple, Alphabet, Microsoft, Meta,
# Tesla) has an empty wdt:P249. Querying only the truthy form silently yields no tickers at
# all in production, so both shapes are queried and the listing interval is carried through.
def test_entity_query_reads_tickers_from_the_exchange_statement_and_the_truthy_claim() -> None:
    query = build_entity_query(["Q312"], limit=100)

    assert "?item wdt:P249 ?value" in query
    assert "?item p:P414 ?statement" in query
    assert "?statement pq:P249 ?value" in query
    # The listing's interval qualifiers ride along on the same statement.
    assert "?statement pq:P580 ?start" in query
    assert "?statement pq:P582 ?end" in query


def _ticker_row(value: str, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "item": _uri("Q380"),
        "field": _literal("ticker"),
        "value": _literal(value),
    }
    if start is not None:
        row["start"] = _literal(start)
    if end is not None:
        row["end"] = _literal(end)
    return row


def test_a_delisted_ticker_stays_a_dated_alias_but_is_not_a_current_identifier() -> None:
    # Meta's real history: FB traded until 2022-06-08, META from 2022-06-09.
    transport = RecordingTransport(
        _rows(
            {"item": _uri("Q380"), "field": _literal("label"), "value": _literal("Meta", "en")},
            _ticker_row("FB", start="+2012-05-18T00:00:00Z", end="+2022-06-08T00:00:00Z"),
            _ticker_row("META", start="+2022-06-09T00:00:00Z"),
        )
    )

    entity = _client(transport).fetch_entities(["Q380"])[0]

    # Only the live symbol is an identifier; the retired one would otherwise assert that Meta
    # still trades as FB.
    assert entity.tickers == ("META",)
    by_value = {alias.value: alias for alias in entity.aliases if alias.alias_type == "ticker"}
    assert by_value["FB"].valid_from == datetime.date(2012, 5, 18)
    assert by_value["FB"].valid_to == datetime.date(2022, 6, 8)
    assert by_value["META"].valid_from == datetime.date(2022, 6, 9)
    # An open interval is what marks the symbol as current (ADR 0006 effective dating).
    assert by_value["META"].valid_to is None


def test_a_truthy_ticker_claim_carries_no_interval_and_stays_current() -> None:
    transport = RecordingTransport(_rows(_ticker_row("ES")))

    entity = _client(transport).fetch_entities(["Q380"])[0]

    assert entity.tickers == ("ES",)
    alias = next(alias for alias in entity.aliases if alias.alias_type == "ticker")
    assert (alias.valid_from, alias.valid_to) == (None, None)


def test_a_cross_listed_entity_keeps_every_live_symbol_as_an_identifier() -> None:
    # Apple really is listed on Nasdaq (AAPL) and, historically, Tokyo (6689, ended 2004).
    transport = RecordingTransport(
        _rows(
            _ticker_row("AAPL", start="+1980-12-01T00:00:00Z"),
            _ticker_row("6689", start="+1990-09-01T00:00:00Z", end="+2004-12-25T00:00:00Z"),
        )
    )

    entity = _client(transport).fetch_entities(["Q380"])[0]

    assert entity.tickers == ("AAPL",)
    assert {alias.value for alias in entity.aliases if alias.alias_type == "ticker"} == {
        "AAPL",
        "6689",
    }
