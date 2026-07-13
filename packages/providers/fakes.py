"""Deterministic fake provider implementations for tests and local development.

These satisfy the Protocols in ``packages.providers.base`` without any network access,
so the whole test suite runs in a clean container with no external news, embedding, or
LLM APIs. Outputs are deterministic to keep tests stable.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Mapping, Sequence

from packages.providers.base import (
    Clock,
    CountryIndicator,
    CountryIndicatorObservation,
    CountryIndicatorProvider,
    EmbeddingProvider,
    EmbeddingResult,
    EnergyObservation,
    EnergyProvider,
    EnergySeries,
    EntityIdentityProvider,
    FREDObservation,
    FREDProvider,
    GDELTArticle,
    GDELTEvent,
    GDELTProvider,
    GeoIncident,
    GeoIncidentProvider,
    HumanitarianProvider,
    HumanitarianReport,
    LEIRecord,
    LEIRelationship,
    LLMProvider,
    LLMResponse,
    RSSItem,
    RSSProvider,
    SanctionsAlias,
    SanctionsDelta,
    SanctionsEntity,
    SanctionsIdentifier,
    SanctionsProvider,
    SECCompanyFact,
    SECCompanyTicker,
    SECCompanyTickerProvider,
    SECEdgarProvider,
    SECSubmission,
    WikidataAlias,
    WikidataEntity,
    WikidataItemRef,
    WikidataProvider,
    WikidataQidMatch,
    ensure_utc,
    normalize_wikidata_qid,
)

FAKE_PROVIDER_NAME = "fake-provider"
FAKE_MODEL_VERSION = "v1"


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


class FakeRSSProvider(RSSProvider):
    """Return a fixed, deterministic set of RSS items for any feed URL."""

    def __init__(self, items: list[RSSItem] | None = None) -> None:
        self._items = items if items is not None else self._default_items()

    @staticmethod
    def _default_items() -> list[RSSItem]:
        published = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
        return [
            RSSItem(
                guid="fake-1",
                title="Sample headline one",
                url="https://example.com/news/1",
                published_at=published,
                summary="A deterministic fake article used for testing.",
                source="fake-feed",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("fake-feed",),
                evidence_refs=("https://example.com/news/1",),
            ),
            RSSItem(
                guid="fake-2",
                title="Sample headline two",
                url="https://example.com/news/2",
                published_at=published,
                summary="Another deterministic fake article.",
                source="fake-feed",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("fake-feed",),
                evidence_refs=("https://example.com/news/2",),
            ),
        ]

    def fetch(self, feed_url: str) -> list[RSSItem]:
        return list(self._items)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Hash-based deterministic embeddings of a fixed dimension."""

    def __init__(self, dimension: int = 8, model: str = "fake-embed") -> None:
        self.dimension = dimension
        self.model = model
        self.model_name = model
        self.model_version = FAKE_MODEL_VERSION
        self.provider_name = FAKE_PROVIDER_NAME

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        results: list[EmbeddingResult] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Map digest bytes into [0, 1) floats, cycling to fill the dimension.
            vector = tuple(digest[i % len(digest)] / 255.0 for i in range(self.dimension))
            run_input = (
                f"{self.provider_name}|{self.model_name}|{self.model_version}|"
                f"{self.dimension}|{text}"
            )
            results.append(
                EmbeddingResult(
                    vector=vector,
                    provider_name=self.provider_name,
                    model_name=self.model_name,
                    model_version=self.model_version,
                    dimension=self.dimension,
                    model_run_id=_stable_id("fake-embed", run_input),
                    source_refs=(_stable_id("text", text),),
                )
            )
        return results


class FakeLLMProvider(LLMProvider):
    """Echo-style deterministic LLM that never makes a network call."""

    def __init__(self, model: str = "fake-llm") -> None:
        self.model = model
        self.model_name = model
        self.model_version = FAKE_MODEL_VERSION
        self.provider_name = FAKE_PROVIDER_NAME

    def complete(self, prompt_name: str, prompt_version: str, prompt: str) -> LLMResponse:
        text = f"[fake:{prompt_name}@{prompt_version}] {prompt}"
        run_input = (
            f"{self.provider_name}|{self.model_name}|{self.model_version}|"
            f"{prompt_name}|{prompt_version}|{prompt}"
        )
        return LLMResponse(
            text=text,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            model_run_id=_stable_id("fake-llm", run_input),
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
            source_refs=(_stable_id("prompt-source", prompt),),
            evidence_refs=(_stable_id("prompt", prompt),),
            metadata={"deterministic": True, "nested": {"provider": self.provider_name}},
        )


class FakeGDELTProvider(GDELTProvider):
    """Return fixed article and event results for GDELT-dependent tests."""

    def __init__(
        self,
        articles: list[GDELTArticle] | None = None,
        events: list[GDELTEvent] | None = None,
    ) -> None:
        self._articles = articles if articles is not None else self._default_articles()
        self._events = events if events is not None else self._default_events()

    @staticmethod
    def _default_articles() -> list[GDELTArticle]:
        seen_at = datetime.datetime(2026, 1, 2, 9, 30, tzinfo=datetime.UTC)
        return [
            GDELTArticle(
                guid="gdelt-article-1",
                title="Central bank signals stable policy path",
                url="https://example.com/gdelt/article-1",
                seen_at=seen_at,
                domain="example.com",
                language="English",
                source_country="US",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("gdelt:doc:fake",),
                evidence_refs=("https://example.com/gdelt/article-1",),
                metadata={"deterministic": True, "rank": 1},
            ),
            GDELTArticle(
                guid="gdelt-article-2",
                title="Regional credit conditions remain mixed",
                url="https://example.com/gdelt/article-2",
                seen_at=seen_at,
                domain="example.com",
                language="English",
                source_country="US",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("gdelt:doc:fake",),
                evidence_refs=("https://example.com/gdelt/article-2",),
                metadata={"deterministic": True, "rank": 2},
            ),
        ]

    @staticmethod
    def _default_events() -> list[GDELTEvent]:
        event_at = datetime.datetime(2026, 1, 2, 9, 0, tzinfo=datetime.UTC)
        return [
            GDELTEvent(
                global_event_id="fake-event-1",
                event_at=event_at,
                event_code="042",
                event_root_code="04",
                actor1_name="BANK",
                actor2_name="REGULATOR",
                action_geo_country_code="US",
                quad_class=1,
                goldstein_scale=1.9,
                avg_tone=-0.4,
                source_url="https://example.com/gdelt/event-1",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("gdelt:event:fake",),
                evidence_refs=("https://example.com/gdelt/event-1",),
                metadata={"deterministic": True},
            )
        ]

    def search_articles(
        self,
        query: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        max_records: int = 50,
    ) -> list[GDELTArticle]:
        return list(self._articles[:max_records])

    def search_events(
        self,
        query: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        max_records: int = 50,
    ) -> list[GDELTEvent]:
        return list(self._events[:max_records])


class FakeFREDProvider(FREDProvider):
    """Generate deterministic FRED observations keyed by series id."""

    def __init__(
        self, observations_by_series: Mapping[str, list[FREDObservation]] | None = None
    ) -> None:
        self._observations_by_series = dict(observations_by_series or {})

    @staticmethod
    def _default_observations(series_id: str) -> list[FREDObservation]:
        base_date = datetime.date(2026, 1, 1)
        seed = int(hashlib.sha256(series_id.encode("utf-8")).hexdigest()[:6], 16)
        observations: list[FREDObservation] = []
        for offset in range(3):
            observed_at = datetime.datetime.combine(
                base_date + datetime.timedelta(days=offset),
                datetime.time(tzinfo=datetime.UTC),
            )
            value = round(((seed % 1000) / 10.0) + offset, 2)
            observations.append(
                FREDObservation(
                    series_id=series_id,
                    observed_at=observed_at,
                    value=value,
                    realtime_start=observed_at.date(),
                    realtime_end=observed_at.date(),
                    provider_name=FAKE_PROVIDER_NAME,
                    source_refs=(f"fred:{series_id}",),
                    evidence_refs=(f"fred:{series_id}:{observed_at.date().isoformat()}",),
                    metadata={"deterministic": True, "offset": offset},
                )
            )
        return observations

    def fetch_series_observations(
        self,
        series_id: str,
        *,
        observation_start: datetime.date | None = None,
        observation_end: datetime.date | None = None,
        limit: int | None = None,
    ) -> list[FREDObservation]:
        observations = list(
            self._observations_by_series.get(series_id, self._default_observations(series_id))
        )
        if observation_start is not None:
            observations = [
                observation
                for observation in observations
                if observation.observed_at.date() >= observation_start
            ]
        if observation_end is not None:
            observations = [
                observation
                for observation in observations
                if observation.observed_at.date() <= observation_end
            ]
        if limit is not None:
            observations = observations[:limit]
        return observations


class FakeSECEdgarProvider(SECEdgarProvider):
    """Return fixed SEC EDGAR submissions and company facts by CIK."""

    def __init__(
        self,
        submissions_by_cik: Mapping[str, list[SECSubmission]] | None = None,
        facts_by_cik: Mapping[str, list[SECCompanyFact]] | None = None,
    ) -> None:
        self._submissions_by_cik = dict(submissions_by_cik or {})
        self._facts_by_cik = dict(facts_by_cik or {})

    @staticmethod
    def _normalize_cik(cik: str) -> str:
        digits = "".join(character for character in str(cik) if character.isdigit())
        return digits.zfill(10)

    @classmethod
    def _default_submissions(cls, cik: str) -> list[SECSubmission]:
        normalized_cik = cls._normalize_cik(cik)
        filed_at = datetime.datetime(2026, 1, 3, tzinfo=datetime.UTC)
        return [
            SECSubmission(
                cik=normalized_cik,
                accession_number="0000000000-26-000001",
                form="10-Q",
                filed_at=filed_at,
                report_at=datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC),
                company_name="Fake Public Company",
                primary_document="fake-10q.htm",
                primary_doc_description="Quarterly report",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=(f"sec:submissions:{normalized_cik}",),
                evidence_refs=(f"sec:accession:{normalized_cik}:0000000000-26-000001",),
                metadata={"deterministic": True},
            )
        ]

    @classmethod
    def _default_facts(cls, cik: str) -> list[SECCompanyFact]:
        normalized_cik = cls._normalize_cik(cik)
        filed_at = datetime.datetime(2026, 1, 3, tzinfo=datetime.UTC)
        period_end_at = datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC)
        return [
            SECCompanyFact(
                cik=normalized_cik,
                taxonomy="us-gaap",
                concept="Assets",
                unit="USD",
                value=1000,
                accession_number="0000000000-26-000001",
                filed_at=filed_at,
                period_end_at=period_end_at,
                form="10-Q",
                fiscal_year=2025,
                fiscal_period="FY",
                label="Assets",
                description="Fake total assets fact.",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=(f"sec:companyfacts:{normalized_cik}",),
                evidence_refs=(f"sec:fact:{normalized_cik}:Assets:USD",),
                metadata={"deterministic": True},
            )
        ]

    def fetch_submissions(self, cik: str) -> list[SECSubmission]:
        normalized_cik = self._normalize_cik(cik)
        return list(self._submissions_by_cik.get(normalized_cik, self._default_submissions(cik)))

    def fetch_company_facts(self, cik: str) -> list[SECCompanyFact]:
        normalized_cik = self._normalize_cik(cik)
        return list(self._facts_by_cik.get(normalized_cik, self._default_facts(cik)))


class FakeSECCompanyTickerProvider(SECCompanyTickerProvider):
    """Return a fixed SEC company_tickers.json identity seed."""

    def __init__(self, tickers: list[SECCompanyTicker] | None = None) -> None:
        self._tickers = tickers if tickers is not None else self._default_tickers()

    @staticmethod
    def _default_tickers() -> list[SECCompanyTicker]:
        rows = (
            ("320193", "AAPL", "Apple Inc."),
            ("1652044", "GOOGL", "Alphabet Inc."),
            # Same CIK, second class of stock: exercises multi-ticker-per-company handling.
            ("1652044", "GOOG", "Alphabet Inc."),
        )
        return [
            SECCompanyTicker(
                cik=cik,
                ticker=ticker,
                title=title,
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=(f"sec:company_tickers:{cik.zfill(10)}",),
                evidence_refs=("https://www.sec.gov/files/company_tickers.json",),
                metadata={"deterministic": True},
            )
            for cik, ticker, title in rows
        ]

    def fetch_company_tickers(self) -> list[SECCompanyTicker]:
        return list(self._tickers)


class FakeSanctionsProvider(SanctionsProvider):
    """Return deterministic sanctioned entities and deltas."""

    def __init__(
        self,
        entities: list[SanctionsEntity] | None = None,
        deltas: list[SanctionsDelta] | None = None,
    ) -> None:
        self._entities = entities if entities is not None else self._default_entities()
        self._deltas = deltas if deltas is not None else self._default_deltas(self._entities)

    @staticmethod
    def _default_entities() -> list[SanctionsEntity]:
        updated_at = datetime.datetime(2026, 1, 4, 8, 0, tzinfo=datetime.UTC)
        return [
            SanctionsEntity(
                entity_id="ofac:1001",
                name="Example Sanctioned Entity",
                entity_type="Entity",
                programs=("CYBER2",),
                sanctions_lists=("SDN",),
                aliases=(
                    SanctionsAlias(
                        name="Example Alias LLC",
                        alias_type="aka",
                        quality="strong",
                        metadata={"deterministic": True},
                    ),
                ),
                identifiers=(
                    SanctionsIdentifier(
                        identifier_type="Tax ID",
                        value="12-3456789",
                        country="US",
                        metadata={"deterministic": True},
                    ),
                ),
                countries=("US",),
                listed_at=datetime.datetime(2026, 1, 3, 8, 0, tzinfo=datetime.UTC),
                updated_at=updated_at,
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("ofac:sdn",),
                evidence_refs=("ofac:sdn:1001",),
                metadata={"deterministic": True},
            )
        ]

    @staticmethod
    def _default_deltas(entities: list[SanctionsEntity]) -> list[SanctionsDelta]:
        entity = entities[0]
        return [
            SanctionsDelta(
                delta_id="ofac-delta-1",
                entity_id=entity.entity_id,
                change_type="add",
                changed_at=entity.updated_at
                or datetime.datetime(2026, 1, 4, 8, 0, tzinfo=datetime.UTC),
                entity=entity,
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("ofac:delta",),
                evidence_refs=("ofac:delta:1001",),
                metadata={"deterministic": True},
            )
        ]

    def fetch_entities(
        self,
        *,
        program: str | None = None,
        updated_since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[SanctionsEntity]:
        entities = list(self._entities)
        if program is not None:
            entities = [entity for entity in entities if program in entity.programs]
        if updated_since is not None:
            updated_since = ensure_utc(updated_since)
            entities = [
                entity
                for entity in entities
                if entity.updated_at is not None and entity.updated_at >= updated_since
            ]
        if limit is not None:
            entities = entities[:limit]
        return entities

    def fetch_deltas(
        self,
        *,
        since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[SanctionsDelta]:
        deltas = list(self._deltas)
        if since is not None:
            since = ensure_utc(since)
            deltas = [delta for delta in deltas if delta.changed_at >= since]
        if limit is not None:
            deltas = deltas[:limit]
        return deltas


class FakeEntityIdentityProvider(EntityIdentityProvider):
    """Return deterministic GLEIF-style LEI records and relationships."""

    def __init__(
        self,
        records: list[LEIRecord] | None = None,
        relationships: list[LEIRelationship] | None = None,
    ) -> None:
        self._records = records if records is not None else self._default_records()
        self._relationships = (
            relationships if relationships is not None else self._default_relationships()
        )

    @staticmethod
    def _default_records() -> list[LEIRecord]:
        updated_at = datetime.datetime(2026, 1, 5, 9, 0, tzinfo=datetime.UTC)
        return [
            LEIRecord(
                lei="5493001KJTIIGC8Y1R12",
                legal_name="Example Financial Holdings Inc.",
                entity_status="ACTIVE",
                registration_status="ISSUED",
                country_code="US",
                jurisdiction="US-DE",
                legal_form="Corporation",
                last_updated_at=updated_at,
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("gleif:lei-records",),
                evidence_refs=("gleif:lei:5493001KJTIIGC8Y1R12",),
                metadata={"deterministic": True},
            )
        ]

    @staticmethod
    def _default_relationships() -> list[LEIRelationship]:
        return [
            LEIRelationship(
                relationship_id="rel-1",
                lei="5493001KJTIIGC8Y1R12",
                related_lei="213800D1EI4B9WTWWD28",
                relationship_type="DIRECT_PARENT",
                status="ACTIVE",
                start_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("gleif:relationships",),
                evidence_refs=("gleif:relationship:rel-1",),
                metadata={"deterministic": True},
            )
        ]

    def search_records(
        self,
        query: str,
        *,
        country_code: str | None = None,
        limit: int = 20,
    ) -> list[LEIRecord]:
        del query
        records = list(self._records)
        if country_code is not None:
            records = [record for record in records if record.country_code == country_code]
        return records[:limit]

    def fetch_relationships(
        self,
        lei: str,
        *,
        relationship_type: str | None = None,
        limit: int = 100,
    ) -> list[LEIRelationship]:
        relationships = [
            relationship for relationship in self._relationships if relationship.lei == lei
        ]
        if relationship_type is not None:
            relationships = [
                relationship
                for relationship in relationships
                if relationship.relationship_type == relationship_type
            ]
        return relationships[:limit]


class FakeWikidataProvider(WikidataProvider):
    """Return deterministic Wikidata identity payloads for the bounded QIDs asked for."""

    def __init__(
        self,
        entities: list[WikidataEntity] | None = None,
        qid_matches: list[WikidataQidMatch] | None = None,
    ) -> None:
        self._entities = entities if entities is not None else self._default_entities()
        self._qid_matches = (
            qid_matches if qid_matches is not None else self._default_qid_matches()
        )

    @staticmethod
    def _default_entities() -> list[WikidataEntity]:
        return [
            WikidataEntity(
                qid="Q312",
                label="Apple Inc.",
                description="American multinational technology company",
                aliases=(
                    WikidataAlias(value="Apple Inc.", alias_type="legal_name"),
                    WikidataAlias(value="Apple", alias_type="colloquial"),
                    WikidataAlias(
                        value="Apple Computer, Inc.",
                        alias_type="former_name",
                        valid_from=datetime.date(1977, 1, 3),
                        valid_to=datetime.date(2007, 1, 9),
                    ),
                    WikidataAlias(value="iPhone", alias_type="brand_product", qid="Q2766"),
                ),
                tickers=("AAPL",),
                leis=("HWUPKR0MPOU8FGXBT394",),
                ciks=("0000320193",),
                industries=(WikidataItemRef(qid="Q11661", label="information technology"),),
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("wikidata:sparql",),
                evidence_refs=("https://www.wikidata.org/wiki/Q312",),
                metadata={"deterministic": True},
            )
        ]

    @staticmethod
    def _default_qid_matches() -> list[WikidataQidMatch]:
        return [
            WikidataQidMatch(
                qid="Q312",
                identifier_type="cik",
                identifier_value="0000320193",
                provider_name=FAKE_PROVIDER_NAME,
            )
        ]

    def resolve_qids(
        self,
        *,
        ciks: Sequence[str] = (),
        leis: Sequence[str] = (),
        limit: int = 10_000,
    ) -> list[WikidataQidMatch]:
        wanted = {("cik", str(value)) for value in ciks} | {("lei", str(value)) for value in leis}
        matches = [
            match
            for match in self._qid_matches
            if (match.identifier_type, match.identifier_value) in wanted
        ]
        return matches[:limit]

    def fetch_entities(
        self, qids: Sequence[str], *, limit: int = 10_000
    ) -> list[WikidataEntity]:
        wanted = {normalize_wikidata_qid(qid) for qid in qids}
        return [entity for entity in self._entities if entity.qid in wanted][:limit]


class FakeCountryIndicatorProvider(CountryIndicatorProvider):
    """Return deterministic World Bank-style country indicators."""

    def __init__(
        self,
        indicators: Mapping[str, CountryIndicator] | None = None,
        observations_by_key: Mapping[
            tuple[str, str], list[CountryIndicatorObservation]
        ]
        | None = None,
    ) -> None:
        self._indicators = dict(indicators or self._default_indicators())
        self._observations_by_key = dict(
            observations_by_key or self._default_observations_by_key()
        )

    @staticmethod
    def _default_indicators() -> Mapping[str, CountryIndicator]:
        indicator = CountryIndicator(
            indicator_id="NY.GDP.MKTP.CD",
            name="GDP (current US$)",
            source="World Development Indicators",
            unit="USD",
            frequency="annual",
            topics=("economy",),
            provider_name=FAKE_PROVIDER_NAME,
            source_refs=("world-bank:indicator:NY.GDP.MKTP.CD",),
            evidence_refs=("world-bank:indicator:NY.GDP.MKTP.CD",),
            metadata={"deterministic": True},
        )
        return {indicator.indicator_id: indicator}

    @staticmethod
    def _default_observations_by_key() -> Mapping[
        tuple[str, str], list[CountryIndicatorObservation]
    ]:
        indicator_id = "NY.GDP.MKTP.CD"
        country_code = "USA"
        observations: list[CountryIndicatorObservation] = []
        for offset, year in enumerate((2024, 2025, 2026)):
            observed_at = datetime.datetime(year, 1, 1, tzinfo=datetime.UTC)
            observations.append(
                CountryIndicatorObservation(
                    country_code=country_code,
                    indicator_id=indicator_id,
                    observed_at=observed_at,
                    value=1000.0 + offset,
                    country_name="United States",
                    unit="USD",
                    provider_name=FAKE_PROVIDER_NAME,
                    source_refs=(f"world-bank:{country_code}:{indicator_id}",),
                    evidence_refs=(f"world-bank:{country_code}:{indicator_id}:{year}",),
                    metadata={"deterministic": True, "year": year},
                )
            )
        return {(country_code, indicator_id): observations}

    def fetch_indicator_metadata(self, indicator_id: str) -> CountryIndicator | None:
        return self._indicators.get(indicator_id)

    def fetch_indicator_observations(
        self,
        country_code: str,
        indicator_id: str,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        limit: int | None = None,
    ) -> list[CountryIndicatorObservation]:
        observations = list(self._observations_by_key.get((country_code, indicator_id), ()))
        if start_year is not None:
            observations = [
                observation for observation in observations if observation.date.year >= start_year
            ]
        if end_year is not None:
            observations = [
                observation for observation in observations if observation.date.year <= end_year
            ]
        if limit is not None:
            observations = observations[:limit]
        return observations


class FakeHumanitarianProvider(HumanitarianProvider):
    """Return deterministic ReliefWeb-style reports."""

    def __init__(self, reports: list[HumanitarianReport] | None = None) -> None:
        self._reports = reports if reports is not None else self._default_reports()

    @staticmethod
    def _default_reports() -> list[HumanitarianReport]:
        published_at = datetime.datetime(2026, 1, 6, 7, 30, tzinfo=datetime.UTC)
        return [
            HumanitarianReport(
                report_id="rw-1",
                title="Flooding situation update",
                url="https://example.com/reliefweb/rw-1",
                published_at=published_at,
                source="Example Relief Agency",
                summary="Deterministic humanitarian report for tests.",
                country_codes=("USA",),
                disaster_types=("Flood",),
                themes=("Coordination",),
                updated_at=published_at,
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("reliefweb:reports",),
                evidence_refs=("https://example.com/reliefweb/rw-1",),
                metadata={"deterministic": True},
            )
        ]

    def search_reports(
        self,
        query: str,
        *,
        country_code: str | None = None,
        disaster_type: str | None = None,
        limit: int = 20,
    ) -> list[HumanitarianReport]:
        del query
        reports = list(self._reports)
        if country_code is not None:
            reports = [report for report in reports if country_code in report.country_codes]
        if disaster_type is not None:
            reports = [report for report in reports if disaster_type in report.disaster_types]
        return reports[:limit]


class FakeGeoIncidentProvider(GeoIncidentProvider):
    """Return deterministic geospatial incidents."""

    def __init__(self, incidents: list[GeoIncident] | None = None) -> None:
        self._incidents = incidents if incidents is not None else self._default_incidents()

    @staticmethod
    def _default_incidents() -> list[GeoIncident]:
        occurred_at = datetime.datetime(2026, 1, 7, 6, 15, tzinfo=datetime.UTC)
        return [
            GeoIncident(
                incident_id="usgs-1",
                incident_type="earthquake",
                title="M 4.5 - Example Region",
                occurred_at=occurred_at,
                latitude=38.1,
                longitude=-122.2,
                magnitude=4.5,
                depth_km=10.0,
                severity="moderate",
                country_code="US",
                place="Example Region",
                url="https://example.com/usgs/usgs-1",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=("geo:incidents",),
                evidence_refs=("https://example.com/usgs/usgs-1",),
                metadata={"deterministic": True, "region": "US"},
            )
        ]

    def search_incidents(
        self,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        region: str | None = None,
        limit: int | None = None,
    ) -> list[GeoIncident]:
        incidents = list(self._incidents)
        if start_at is not None:
            start_at = ensure_utc(start_at)
            incidents = [
                incident for incident in incidents if incident.occurred_at >= start_at
            ]
        if end_at is not None:
            end_at = ensure_utc(end_at)
            incidents = [incident for incident in incidents if incident.occurred_at <= end_at]
        if region is not None:
            incidents = [
                incident
                for incident in incidents
                if incident.metadata.get("region") == region or incident.country_code == region
            ]
        if limit is not None:
            incidents = incidents[:limit]
        return incidents


class FakeEnergyProvider(EnergyProvider):
    """Return deterministic EIA-style energy series and observations."""

    def __init__(
        self,
        series_by_id: Mapping[str, EnergySeries] | None = None,
        observations_by_series: Mapping[str, list[EnergyObservation]] | None = None,
    ) -> None:
        self._series_by_id = dict(series_by_id or self._default_series_by_id())
        self._observations_by_series = dict(
            observations_by_series or self._default_observations_by_series()
        )

    @staticmethod
    def _default_series_by_id() -> Mapping[str, EnergySeries]:
        series = EnergySeries(
            series_id="PET.WCRSTUS1.W",
            name="Weekly U.S. Ending Stocks of Crude Oil",
            units="Thousand Barrels",
            frequency="weekly",
            geography="USA",
            provider_name=FAKE_PROVIDER_NAME,
            source_refs=("eia:series:PET.WCRSTUS1.W",),
            evidence_refs=("eia:series:PET.WCRSTUS1.W",),
            metadata={"deterministic": True},
        )
        return {series.series_id: series}

    @staticmethod
    def _default_observations_by_series() -> Mapping[str, list[EnergyObservation]]:
        series_id = "PET.WCRSTUS1.W"
        observations: list[EnergyObservation] = []
        for offset in range(3):
            observed_at = datetime.datetime(2026, 1, 2 + offset, tzinfo=datetime.UTC)
            observations.append(
                EnergyObservation(
                    series_id=series_id,
                    observed_at=observed_at,
                    value=420000.0 + offset,
                    units="Thousand Barrels",
                    geography="USA",
                    provider_name=FAKE_PROVIDER_NAME,
                    source_refs=(f"eia:{series_id}",),
                    evidence_refs=(f"eia:{series_id}:{observed_at.date().isoformat()}",),
                    metadata={"deterministic": True, "offset": offset},
                )
            )
        return {series_id: observations}

    def fetch_series(self, series_id: str) -> EnergySeries:
        return self._series_by_id.get(
            series_id,
            EnergySeries(
                series_id=series_id,
                name=f"Fake energy series {series_id}",
                provider_name=FAKE_PROVIDER_NAME,
                source_refs=(f"eia:series:{series_id}",),
                evidence_refs=(f"eia:series:{series_id}",),
                metadata={"deterministic": True},
            ),
        )

    def fetch_series_observations(
        self,
        series_id: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[EnergyObservation]:
        observations = list(
            self._observations_by_series.get(
                series_id,
                [
                    EnergyObservation(
                        series_id=series_id,
                        observed_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                        value=1.0,
                        provider_name=FAKE_PROVIDER_NAME,
                        source_refs=(f"eia:{series_id}",),
                        evidence_refs=(f"eia:{series_id}:2026-01-01",),
                        metadata={"deterministic": True},
                    )
                ],
            )
        )
        if start_at is not None:
            start_at = ensure_utc(start_at)
            observations = [
                observation for observation in observations if observation.observed_at >= start_at
            ]
        if end_at is not None:
            end_at = ensure_utc(end_at)
            observations = [
                observation for observation in observations if observation.observed_at <= end_at
            ]
        if limit is not None:
            observations = observations[:limit]
        return observations


class FixedClock(Clock):
    """A clock that always returns a fixed UTC instant."""

    def __init__(self, fixed: datetime.datetime | None = None) -> None:
        self._fixed = ensure_utc(
            fixed or datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        )

    def now(self) -> datetime.datetime:
        return self._fixed
