"""Wikidata SPARQL provider client using stdlib urllib/json transport (ADR 0006 source 3).

Every query this client builds is bounded. It accepts explicit QIDs, or explicit identifier
values a caller already holds, and pins them into a ``VALUES`` clause under a row ``LIMIT``;
there is no free-text search and no crawl. QIDs are validated against ``Q[1-9][0-9]*`` and
literals are escaped, so caller-supplied text never reaches the query as SPARQL syntax.

The result shape is deliberately long-format — one row per ``(item, field, value)`` — because
a single SELECT with one OPTIONAL per property would emit the cartesian product of every
independent property and blow the endpoint's row and time budget.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import (
    WikidataAlias,
    WikidataEntity,
    WikidataItemRef,
    WikidataProvider,
    WikidataQidMatch,
    normalize_sec_cik,
    normalize_wikidata_qid,
)

UrlOpenCallable = Callable[..., Any]

DEFAULT_WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Hard caps: how many values one query may pin, and how many rows it may return. Callers that
# hold more inputs than this must batch, which keeps every single request bounded.
DEFAULT_MAX_VALUES = 250
DEFAULT_ROW_LIMIT = 10_000

ENTITY_URI_PREFIX = "http://www.wikidata.org/entity/"

# ADR 0006 property ids. The ADR names ticker/LEI/CIK/parent/subsidiary/industry; official name
# plus its start/end qualifiers is how Wikidata dates a rename, and is the former-name source.
PROPERTY_TICKER = "P249"
PROPERTY_LEI = "P1278"
PROPERTY_CIK = "P5531"
PROPERTY_PARENT = "P749"
PROPERTY_SUBSIDIARY = "P355"
PROPERTY_INDUSTRY = "P452"
PROPERTY_OFFICIAL_NAME = "P1448"
PROPERTY_SHORT_NAME = "P1813"
PROPERTY_BRAND = "P1716"
# A ticker is exchange-scoped, so Wikidata overwhelmingly states it as a qualifier on the
# stock-exchange (P414) statement rather than as a truthy P249 claim: every large issuer checked
# against the live endpoint (Apple, Alphabet, Microsoft, Meta, Tesla) has an empty wdt:P249 and
# carries its symbols under p:P414/pq:P249. Truthy P249 does occur, so both shapes are queried.
PROPERTY_STOCK_EXCHANGE = "P414"
QUALIFIER_START_TIME = "P580"
QUALIFIER_END_TIME = "P582"

# HTTP statuses worth retrying; everything else is a permanent client error.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_LITERAL_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class WikidataError(RuntimeError):
    """A Wikidata transport or protocol failure, tagged for retry decisions."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class WikidataClient(WikidataProvider):
    """Bounded Wikidata SPARQL client with injectable transport and an explicit timeout."""

    def __init__(
        self,
        *,
        user_agent: str,
        urlopen: UrlOpenCallable | None = None,
        endpoint: str = DEFAULT_WIKIDATA_SPARQL_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_values: int = DEFAULT_MAX_VALUES,
        row_limit: int = DEFAULT_ROW_LIMIT,
    ) -> None:
        # WDQS blocks generic agents under its user-agent policy, same as SEC fair access.
        if not user_agent.strip():
            msg = "Wikidata user_agent is required"
            raise ValueError(msg)
        self._user_agent = user_agent
        self._urlopen = urlopen or stdlib_urlopen
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_values = max_values
        self._row_limit = row_limit

    def resolve_qids(
        self,
        *,
        ciks: Sequence[str] = (),
        leis: Sequence[str] = (),
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> list[WikidataQidMatch]:
        """Map identifier values already seeded on entities onto the QIDs that carry them."""

        cik_values = self._bounded(
            sorted({form for cik in ciks for form in _cik_forms(cik)}),
        )
        lei_values = self._bounded(
            sorted({str(lei).strip().upper() for lei in leis if str(lei).strip()}),
        )
        if not cik_values and not lei_values:
            return []
        query = build_resolve_query(cik_values, lei_values, limit=self._bounded_limit(limit))
        return _parse_qid_matches(self._run(query))

    def fetch_entities(
        self, qids: Sequence[str], *, limit: int = DEFAULT_ROW_LIMIT
    ) -> list[WikidataEntity]:
        """Fetch the ADR 0006 identity payload for an explicit, bounded set of QIDs."""

        wanted = self._bounded(sorted({normalize_wikidata_qid(qid) for qid in qids}))
        if not wanted:
            return []
        query = build_entity_query(wanted, limit=self._bounded_limit(limit))
        return _parse_entities(wanted, self._run(query))

    def _bounded(self, values: list[str]) -> list[str]:
        if len(values) > self._max_values:
            msg = f"a bounded Wikidata query accepts at most {self._max_values} values"
            raise ValueError(msg)
        return values

    def _bounded_limit(self, limit: int) -> int:
        if limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        return min(limit, self._row_limit)

    def _run(self, query: str) -> Mapping[str, Any]:
        url = f"{self._endpoint}?{urlencode({'format': 'json', 'query': query})}"
        request = Request(
            url,
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": self._user_agent,
            },
        )
        try:
            handle = self._urlopen(request, timeout=self._timeout)
        except HTTPError as error:
            retryable = error.code in RETRYABLE_STATUS
            msg = f"Wikidata request failed with status {error.code}"
            raise WikidataError(msg, retryable=retryable, status=error.code) from error
        except (TimeoutError, URLError, OSError) as error:
            msg = f"Wikidata request failed: {error}"
            raise WikidataError(msg, retryable=True) from error

        if hasattr(handle, "__enter__"):
            with handle as response:
                body = response.read()
        else:
            body = handle.read()
        text = body.decode("utf-8") if isinstance(body, bytes | bytearray) else str(body)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            msg = f"Wikidata response is not valid JSON: {error}"
            raise WikidataError(msg, retryable=False) from error
        if not isinstance(payload, Mapping):
            msg = "Wikidata response must be a JSON object"
            raise WikidataError(msg, retryable=False)
        return payload


def build_entity_query(qids: Sequence[str], *, limit: int) -> str:
    """Build the bounded long-format identity query for an explicit list of QIDs."""

    values = " ".join(f"wd:{normalize_wikidata_qid(qid)}" for qid in qids)
    branches = "\n  UNION ".join(
        (
            '{ ?item rdfs:label ?value . FILTER(LANG(?value) = "en") BIND("label" AS ?field) }',
            '{ ?item schema:description ?value . FILTER(LANG(?value) = "en") '
            'BIND("description" AS ?field) }',
            '{ ?item skos:altLabel ?value . FILTER(LANG(?value) = "en") BIND("alias" AS ?field) }',
            f'{{ ?item wdt:{PROPERTY_SHORT_NAME} ?value . BIND("short_name" AS ?field) }}',
            f'{{ ?item wdt:{PROPERTY_TICKER} ?value . BIND("ticker" AS ?field) }}',
            # The exchange-listing statement carries the symbol and the interval it traded
            # under: an end time marks a delisting or a symbol change (Meta: FB -> META).
            f"{{ ?item p:{PROPERTY_STOCK_EXCHANGE} ?statement . "
            f"?statement pq:{PROPERTY_TICKER} ?value . "
            f"OPTIONAL {{ ?statement pq:{QUALIFIER_START_TIME} ?start }} "
            f"OPTIONAL {{ ?statement pq:{QUALIFIER_END_TIME} ?end }} "
            'BIND("ticker" AS ?field) }',
            f'{{ ?item wdt:{PROPERTY_LEI} ?value . BIND("lei" AS ?field) }}',
            f'{{ ?item wdt:{PROPERTY_CIK} ?value . BIND("cik" AS ?field) }}',
            f'{{ ?item wdt:{PROPERTY_BRAND} ?value . BIND("brand" AS ?field) }}',
            f'{{ ?item wdt:{PROPERTY_PARENT} ?value . BIND("parent" AS ?field) }}',
            f'{{ ?item wdt:{PROPERTY_SUBSIDIARY} ?value . BIND("subsidiary" AS ?field) }}',
            f'{{ ?item wdt:{PROPERTY_INDUSTRY} ?value . BIND("industry" AS ?field) }}',
            # Official-name statements carry the rename interval: an end time makes the name a
            # former name. Left unfiltered by language so non-English legal names survive.
            f"{{ ?item p:{PROPERTY_OFFICIAL_NAME} ?statement . "
            f"?statement ps:{PROPERTY_OFFICIAL_NAME} ?value . "
            f"OPTIONAL {{ ?statement pq:{QUALIFIER_START_TIME} ?start }} "
            f"OPTIONAL {{ ?statement pq:{QUALIFIER_END_TIME} ?end }} "
            'BIND("official_name" AS ?field) }',
        )
    )
    return (
        "SELECT ?item ?field ?value ?valueLabel ?start ?end WHERE {\n"
        f"  VALUES ?item {{ {values} }}\n"
        f"  {branches}\n"
        '  OPTIONAL { ?value rdfs:label ?valueLabel . FILTER(LANG(?valueLabel) = "en") }\n'
        "}\n"
        f"LIMIT {int(limit)}"
    )


def build_resolve_query(ciks: Sequence[str], leis: Sequence[str], *, limit: int) -> str:
    """Build the bounded VALUES query that maps seeded CIK/LEI values onto QIDs."""

    branches: list[str] = []
    for field, property_id, values in (
        ("cik", PROPERTY_CIK, ciks),
        ("lei", PROPERTY_LEI, leis),
    ):
        if not values:
            continue
        literals = " ".join(sparql_literal(value) for value in values)
        branches.append(
            f"{{ VALUES ?value {{ {literals} }} "
            f'?item wdt:{property_id} ?value . BIND("{field}" AS ?field) }}'
        )
    if not branches:
        msg = "a resolve query needs at least one identifier value"
        raise ValueError(msg)
    return (
        "SELECT ?item ?field ?value WHERE {\n  "
        + "\n  UNION ".join(branches)
        + f"\n}}\nLIMIT {int(limit)}"
    )


def sparql_literal(value: str) -> str:
    """Escape a caller-supplied identifier value into a quoted SPARQL literal."""

    text = str(value)
    if any(ord(character) < 0x20 and character not in "\n\r\t" for character in text):
        msg = "identifier value contains control characters"
        raise ValueError(msg)
    escaped = "".join(_LITERAL_ESCAPES.get(character, character) for character in text)
    return f'"{escaped}"'


def _cik_forms(value: Any) -> tuple[str, ...]:
    """Both spellings a CIK appears under on Wikidata: zero-padded and bare."""

    padded = normalize_sec_cik(value)
    return (padded, padded.lstrip("0") or "0")


def _parse_qid_matches(payload: Mapping[str, Any]) -> list[WikidataQidMatch]:
    matches: list[WikidataQidMatch] = []
    for row in _bindings(payload):
        qid = _qid(row, "item")
        field = _text(row, "field")
        value = _text(row, "value")
        if not qid or not value or field not in ("cik", "lei"):
            continue
        normalized = normalize_sec_cik(value) if field == "cik" else value.upper()
        matches.append(
            WikidataQidMatch(qid=qid, identifier_type=field, identifier_value=normalized)
        )
    return matches


def _parse_entities(qids: Sequence[str], payload: Mapping[str, Any]) -> list[WikidataEntity]:
    """Fold the long-format rows back into one DTO per requested QID, in request order."""

    rows_by_qid: dict[str, list[Mapping[str, Any]]] = {qid: [] for qid in qids}
    for row in _bindings(payload):
        qid = _qid(row, "item")
        # A row for an item nobody asked for cannot exist in a VALUES-bounded query; drop it
        # rather than trust it.
        if qid in rows_by_qid:
            rows_by_qid[qid].append(row)
    return [_parse_entity(qid, rows) for qid, rows in rows_by_qid.items() if rows]


def _parse_entity(qid: str, rows: Sequence[Mapping[str, Any]]) -> WikidataEntity:
    label = ""
    description = ""
    aliases: list[WikidataAlias] = []
    identifiers: dict[str, list[str]] = {"ticker": [], "lei": [], "cik": []}
    refs: dict[str, list[WikidataItemRef]] = {"parent": [], "subsidiary": [], "industry": []}

    for row in rows:
        field = _text(row, "field")
        value = _text(row, "value")
        if field == "label":
            label = label or value
        elif field == "description":
            description = description or value
        elif field == "alias" and value:
            aliases.append(WikidataAlias(value=value, alias_type="colloquial"))
        elif field == "short_name" and value:
            aliases.append(WikidataAlias(value=value, alias_type="short_name"))
        elif field == "official_name" and value:
            aliases.append(_official_name_alias(row, value))
        elif field == "ticker" and value:
            alias = _ticker_alias(row, value)
            # A symbol the entity no longer trades under stays an alias, dated, because old
            # coverage still names it — but it is not one of the entity's current identifiers.
            if alias.valid_to is None:
                identifiers["ticker"].append(value.strip().upper())
            aliases.append(alias)
        elif field == "lei" and value:
            identifiers["lei"].append(value.strip().upper())
        elif field == "cik" and value:
            identifiers["cik"].append(normalize_sec_cik(value))
        elif field == "brand":
            aliases.append(_brand_alias(row))
        elif field in refs:
            related = _qid(row, "value")
            if related:
                refs[field].append(WikidataItemRef(qid=related, label=_text(row, "valueLabel")))

    if label:
        # The item's label is its canonical name; it leads the alias list so that the surface
        # form stored for its normalized key is the current name, not a variant of it.
        aliases.insert(0, WikidataAlias(value=label, alias_type="legal_name"))
    return WikidataEntity(
        qid=qid,
        label=label,
        description=description,
        aliases=tuple(alias for alias in aliases if alias.value),
        tickers=_unique(identifiers["ticker"]),
        leis=_unique(identifiers["lei"]),
        ciks=_unique(identifiers["cik"]),
        parents=_unique_refs(refs["parent"]),
        subsidiaries=_unique_refs(refs["subsidiary"]),
        industries=_unique_refs(refs["industry"]),
        source_refs=("wikidata:sparql",),
        evidence_refs=(f"https://www.wikidata.org/wiki/{qid}",),
        metadata={"bindings": [dict(row) for row in rows]},
    )


def _official_name_alias(row: Mapping[str, Any], value: str) -> WikidataAlias:
    """An official name with an end date is a former name; without one it is the legal name."""

    valid_from = _parse_date(_text(row, "start"))
    valid_to = _parse_date(_text(row, "end"))
    alias_type = "former_name" if valid_to is not None else "legal_name"
    return WikidataAlias(
        value=value,
        alias_type=alias_type,
        language=_language(row, "value"),
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _ticker_alias(row: Mapping[str, Any], value: str) -> WikidataAlias:
    """A listing's start/end qualifiers date the symbol; a truthy P249 claim carries neither."""

    return WikidataAlias(
        value=value,
        alias_type="ticker",
        valid_from=_parse_date(_text(row, "start")),
        valid_to=_parse_date(_text(row, "end")),
    )


def _brand_alias(row: Mapping[str, Any]) -> WikidataAlias:
    """A brand is a name the entity trades under, never a claim of legal identity with it."""

    return WikidataAlias(
        value=_text(row, "valueLabel"),
        alias_type="brand_product",
        qid=_qid(row, "value"),
    )


def _bindings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, Mapping):
        return []
    bindings = results.get("bindings")
    if not isinstance(bindings, list):
        return []
    return [row for row in bindings if isinstance(row, Mapping)]


def _cell(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    cell = row.get(key)
    return cell if isinstance(cell, Mapping) else {}


def _text(row: Mapping[str, Any], key: str) -> str:
    value = _cell(row, key).get("value")
    return "" if value is None else str(value).strip()


def _language(row: Mapping[str, Any], key: str) -> str:
    return str(_cell(row, key).get("xml:lang") or "").strip()


def _qid(row: Mapping[str, Any], key: str) -> str:
    value = _text(row, key)
    if not value.startswith(ENTITY_URI_PREFIX):
        return ""
    candidate = value[len(ENTITY_URI_PREFIX) :]
    try:
        return normalize_wikidata_qid(candidate)
    except ValueError:
        # Properties and lexemes share the entity namespace; they are not items.
        return ""


def _parse_date(value: str) -> datetime.date | None:
    """Parse a WDQS xsd:dateTime ("+1976-04-01T00:00:00Z"); an imprecise date yields None."""

    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value.lstrip("+")[:10])
    except ValueError:
        return None


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _unique_refs(refs: Sequence[WikidataItemRef]) -> tuple[WikidataItemRef, ...]:
    by_qid = {ref.qid: ref for ref in refs}
    return tuple(by_qid[qid] for qid in sorted(by_qid))
