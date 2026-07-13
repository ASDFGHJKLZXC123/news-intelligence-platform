"""Deterministic news-mention entity linking: ADR 0005 stage 2 (no LLM, no network).

Stage 1 (``services.nlp.mentions``) hands this module an extracted mention; this module turns
it into a scored, banded candidate list against the ADR 0006 identity store, and stage 3 (LLM
adjudication) consumes whatever lands in the ambiguous band. Nothing here calls a model, and
nothing here writes ``event_entities``.

The provider-side resolver (``services.entities.resolution``) is a different problem: it starts
from the CIK/LEI/ticker a provider already gave it. A news mention has a surface form and an
article, so it gets its own linker rather than more branches in that one.

How a mention is linked:

1. **Candidates** come only from matching ``normalize_alias(surface)`` against ``entity_aliases``
   (the ADR alias types), respecting each alias' validity interval at the article's publication
   date. Identity rows that carry no alias at all fall back to a controlled, indexed
   ``entity_profiles.normalized_name`` lookup, and only when no alias matched the surface.
2. **Redirects** are followed at resolve time as of the same date, so candidates merge onto the
   entity a mention resolves to today. A cycle, an over-deep chain, a fork into competing
   redirects sharing one effective date, or a redirect that points at a profile that is not there
   is flagged and never attached.
3. **Confidence** is the weighted sum of the seven ADR §10.3 signals, scaled by the alias prior
   (Wikidata-sourced evidence carries the ADR's 0.7 multiplier). Alias matching only *gates*
   candidate generation; it contributes no score of its own, so a bare alias match with no
   supporting signal scores 0.00 and goes to NIL. That is the ADR's design -- the weights are
   explicitly initial values, to be recalibrated against the labeled-mentions gold set.

Every returned object is frozen and JSON-serializable (``as_dict``), because the candidate list
crosses a Celery/LLM boundary in stage 3.
"""

from __future__ import annotations

import datetime
import math
import re
import uuid
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select

from db.models import EntityAlias, EntityProfile, EntityRedirect, EntityResolutionRun
from packages.config.settings import get_settings
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import EntityLabel, EntityMention
from services.provider_data.common import (
    add,
    find_one,
    flush_pending,
    idempotency_key,
    identity_precedence,
    identity_sources,
    json_safe,
    normalize_alias,
    normalize_name,
)
from services.provider_data.entity_redirects import (
    REDIRECT_AMBIGUOUS,
    REDIRECT_CYCLE,
    REDIRECT_MAX_DEPTH,
    REDIRECT_MISSING_TARGET,
    REDIRECT_TOO_DEEP,
)

# Runs written by this linker share ``entity_resolution_runs`` with the provider resolver; the
# target type is what keeps the two sets apart, for the review queue and for anything that later
# reads either.
NEWS_MENTION_TARGET_TYPE: Final = "news_mention"

# The ADR's alias vocabulary. An alias row of any other type never generates a candidate.
LINKABLE_ALIAS_TYPES: Final[frozenset[str]] = frozenset(
    {
        "legal_name",
        "short_name",
        "former_name",
        "colloquial",
        "brand_product",
        "ticker",
        "transliteration",
    }
)

# The synthetic type for the canonical-name fallback, so its evidence is never mistaken for a
# real alias row. It is not linkable on its own -- it is only reachable when no alias matched.
CANONICAL_NAME_EVIDENCE_TYPE: Final = "canonical_name"
CANONICAL_NAME_EVIDENCE_SOURCE: Final = "entity_profiles.normalized_name"

# Which evidence stands for an entity when several of its aliases match one surface: current
# legal names first, the stale (former) and the weak (brand) last.
ALIAS_TYPE_RANK: Final[Mapping[str, int]] = {
    "legal_name": 0,
    CANONICAL_NAME_EVIDENCE_TYPE: 1,
    "short_name": 2,
    "ticker": 3,
    "transliteration": 4,
    "colloquial": 5,
    "former_name": 6,
    "brand_product": 7,
}

WIKIDATA_SOURCE: Final = "wikidata"
# ADR 0005: "Wikidata-sourced aliases carry a 0.7 prior multiplier."
WIKIDATA_ALIAS_PRIOR: Final[float] = 0.7
DEFAULT_ALIAS_PRIOR: Final[float] = 1.0

ACCEPT_THRESHOLD: Final[float] = 0.85
ADJUDICATE_THRESHOLD: Final[float] = 0.50


class LinkBand(StrEnum):
    """ADR 0005 confidence bands: accept >= 0.85, adjudicate 0.50-0.85, reject/NIL < 0.50."""

    ACCEPT = "accept"
    ADJUDICATE = "adjudicate"
    NIL = "nil"


class LinkSignal(StrEnum):
    """The seven ADR §10.3 disambiguation signals, in weight order."""

    EXACT_TICKER = "exact_ticker"
    CO_MENTIONS = "co_mentions"
    MENTION_CONTEXT = "mention_context"
    URL_DOMAIN = "url_domain"
    INDUSTRY_TERMS = "industry_terms"
    SOURCE_CATEGORY = "source_category"
    LOCATION_CUES = "location_cues"


# ADR 0005 initial weights. They sum to exactly 1.00, which is what makes a score comparable to
# the band thresholds at all.
SIGNAL_WEIGHTS: Final[Mapping[LinkSignal, float]] = {
    LinkSignal.EXACT_TICKER: 0.25,
    LinkSignal.CO_MENTIONS: 0.25,
    LinkSignal.MENTION_CONTEXT: 0.20,
    LinkSignal.URL_DOMAIN: 0.15,
    LinkSignal.INDUSTRY_TERMS: 0.05,
    LinkSignal.SOURCE_CATEGORY: 0.05,
    LinkSignal.LOCATION_CUES: 0.05,
}

# "Apple reported revenue" -> company reading. Evaluated word-bounded on the mention window.
MENTION_CONTEXT_CUES: Final[tuple[str, ...]] = (
    "acquisition",
    "analysts",
    "ceo",
    "chief executive",
    "dividend",
    "earnings",
    "filing",
    "guidance",
    "investors",
    "ipo",
    "market cap",
    "merger",
    "profit",
    "quarterly",
    "reported",
    "revenue",
    "shareholders",
    "shares",
    "stock",
    "valuation",
)

# "Financial source raises company prior" (ADR 0005).
FINANCIAL_SOURCE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"business", "business_news", "finance", "financial", "financial_news", "markets"}
)
COMPANY_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {"company", "corporation", "organization", "private_company", "public_company"}
)
# The stage-1 labels that read as an organization/company mention.
COMPANY_MENTION_LABELS: Final[frozenset[EntityLabel]] = frozenset(
    {EntityLabel.ORG, EntityLabel.PRODUCT}
)
LOCATION_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {"country", "government", "location", "region"}
)

# A ticker is uppercase and short; anything else in a ticker alias row is data, not a symbol.
_TICKER_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_WORD = "A-Za-z0-9"
_SCORE_QUANTUM: Final = Decimal("0.0001")
_EXPLANATION_LIMIT: Final = 480
_EXPLANATION_SEPARATOR: Final = " | "

REASON_NO_SURFACE: Final = "mention surface has no normalized alias key"
REASON_NO_CANDIDATE: Final = "no valid alias or canonical name matched the mention surface"
REASON_BELOW_ADJUDICATE: Final = "best candidate scored below the adjudicate threshold"
REASON_AMBIGUOUS_BAND: Final = "best candidate scored in the adjudicate band"
REASON_ACCEPT_TIE: Final = "accept-threshold tie between distinct candidates"
REASON_BRAND_GATE: Final = "brand/product alias with no independent signal"
REASON_ACCEPTED: Final = "top candidate cleared the accept threshold"

# A rejected candidate's reason is the identity store's own (``REDIRECT_CYCLE`` and the rest are
# imported above, not restated here): this linker and ``resolve_entity_redirect`` refuse the same
# chains for the same reasons, and one vocabulary is what keeps them from drifting apart again.


class NewsLinkingError(ValueError):
    """Raised when a mention and its linking context do not describe the same article."""


# --- Context -------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArticleLinkingContext:
    """Everything outside the mention itself that the signals may read.

    Explicit and immutable by construction: the linker never reaches back into the article, the
    source, or the database for context, so a mention scores the same wherever it runs.
    ``location_terms`` are compared against ``EntityProfile.country`` as the identity store holds
    it (GLEIF writes a two-letter code), so a caller holding GPE surfaces should pass the country
    codes it mapped them to. ``linked_entity_ids`` are entities linked from *other* mentions of
    this article; passing this mention's own link back in would let a candidate support itself.
    """

    article_key: str
    published_on: datetime.date | None = None
    url: str | None = None
    source_category: str | None = None
    text: str | None = None
    co_mention_surfaces: tuple[str, ...] = ()
    linked_entity_ids: tuple[uuid.UUID, ...] = ()
    industry_terms: tuple[str, ...] = ()
    location_terms: tuple[str, ...] = ()
    max_candidates: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "co_mention_surfaces",
            "linked_entity_ids",
            "industry_terms",
            "location_terms",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.max_candidates is not None and self.max_candidates < 1:
            msg = f"max_candidates must allow at least one candidate, got {self.max_candidates}"
            raise ValueError(msg)


# --- Evidence and results ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AliasEvidence:
    """The identity row that made an entity a candidate for this surface."""

    entity_id: uuid.UUID
    alias: str
    normalized_alias: str
    alias_type: str
    source: str
    prior_multiplier: float
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class SignalEvidence:
    """One weighted signal's contribution, with the terms that produced it."""

    signal: LinkSignal
    weight: float
    strength: float
    contribution: float
    detail: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class LinkCandidate:
    """One canonical entity a mention may refer to, with its full, inspectable score."""

    entity_id: uuid.UUID
    canonical_name: str
    entity_type: str | None
    country: str | None
    ticker: str | None
    industries: tuple[str, ...]
    evidence: AliasEvidence
    alternate_evidence: tuple[AliasEvidence, ...]
    redirected_from: tuple[uuid.UUID, ...]
    redirect_paths: tuple[tuple[uuid.UUID, ...], ...]
    signals: tuple[SignalEvidence, ...]
    prior_multiplier: float
    signal_score: float
    score: float
    band: LinkBand

    @property
    def has_supporting_signal(self) -> bool:
        """True when at least one signal fired -- the gate a brand/product alias must clear."""
        return any(signal.strength > 0.0 for signal in self.signals)

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """An alias match that could not be attached, and why. Never scored, never linked."""

    entity_id: uuid.UUID
    reason: str
    chain: tuple[uuid.UUID, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class MentionLinkResult:
    """The linking outcome for one mention: band, candidates, and the whole audit trail."""

    article_key: str
    surface: str
    normalized_surface: str
    start_char: int
    end_char: int
    label: EntityLabel
    assertion_status: AssertionStatus
    band: LinkBand
    matched_entity_id: uuid.UUID | None
    confidence_score: float | None
    reason: str
    run_key: str
    target_id: str
    explanation: str
    candidates: tuple[LinkCandidate, ...] = ()
    rejected: tuple[RejectedCandidate, ...] = ()

    @property
    def should_attach(self) -> bool:
        """Only a deterministic ACCEPT attaches an entity; every other band waits for stage 3."""
        return self.band is LinkBand.ACCEPT and self.matched_entity_id is not None

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


# --- Scoring primitives --------------------------------------------------------
def round_score(value: float) -> float:
    """Round a score half-up to 4 decimals, so a band comparison never sees float noise."""
    return float(Decimal(repr(value)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP))


def band_for_score(score: float) -> LinkBand:
    """ADR 0005 bands, exact at the boundaries: 0.85 accepts, and 0.50 adjudicates."""
    if score >= ACCEPT_THRESHOLD:
        return LinkBand.ACCEPT
    if score >= ADJUDICATE_THRESHOLD:
        return LinkBand.ADJUDICATE
    return LinkBand.NIL


def band_of_run(matched_entity_id: uuid.UUID | None, score: float | None) -> LinkBand:
    """The band a persisted run landed in, from its own columns rather than its explanation text.

    A run that matched an entity resolved, so it is not an unresolved mention: a deterministic
    ACCEPT writes ``matched_entity_id`` here, and stage-3 adjudication writes it back onto the run
    it resolved (``services.entities.adjudication.record_adjudication``). An *unmatched* run that
    nonetheless scored at or above the accept threshold is one the tie or brand/product gate held
    back — ``decide_band`` sent it to stage 3, so it is an ADJUDICATE, not an ACCEPT. A run with no
    score at all had no candidate to score, so it is NIL.

    This is what the review queue bands on. The ``explanation`` column is prose meant for a human,
    and banding on a string parsed back out of it would make the queue's correctness depend on
    text formatting.
    """
    if matched_entity_id is not None:
        return LinkBand.ACCEPT
    if score is None or score < ADJUDICATE_THRESHOLD:
        return LinkBand.NIL
    return LinkBand.ADJUDICATE


def alias_prior_multiplier(source: str | None) -> float:
    """ADR 0005: Wikidata-sourced matching evidence is discounted; every other source is not."""
    normalized = (source or "").strip().casefold()
    return WIKIDATA_ALIAS_PRIOR if normalized == WIKIDATA_SOURCE else DEFAULT_ALIAS_PRIOR


def decide_band(candidates: Sequence[LinkCandidate]) -> tuple[LinkBand, str]:
    """The band a mention lands in, given its candidates in descending score order.

    Two conservative gates sit in front of an automatic attach, and both send the mention to
    stage 3 rather than to an entity:

    * a *tie* at the accept threshold — deterministic evidence that cannot separate two entities
      is not evidence for either of them;
    * a *brand/product* alias with no signal behind it. Under the ADR's initial weights a score
      of 0.85 already implies four signals fired, so this gate is a guarantee rather than a
      frequent event; it is enforced here so that recalibrating the weights (or adding a prior
      that scores an alias match on its own) cannot quietly start auto-accepting brands.
    """
    if not candidates:
        return LinkBand.NIL, REASON_NO_CANDIDATE

    top = candidates[0]
    if top.band is LinkBand.ACCEPT:
        if len(candidates) > 1 and candidates[1].score == top.score:
            return LinkBand.ADJUDICATE, REASON_ACCEPT_TIE
        if top.evidence.alias_type == "brand_product" and not top.has_supporting_signal:
            return LinkBand.ADJUDICATE, REASON_BRAND_GATE
        return LinkBand.ACCEPT, REASON_ACCEPTED
    if top.band is LinkBand.ADJUDICATE:
        return LinkBand.ADJUDICATE, REASON_AMBIGUOUS_BAND
    return LinkBand.NIL, REASON_BELOW_ADJUDICATE


def format_link_explanation(fields: Mapping[str, Any]) -> str:
    """Render the persisted audit summary as stable, human-readable ``key=value`` fields.

    ``entity_resolution_runs.explanation`` is unstructured text that the provider resolver already
    fills with prose, so the structured breakdown stays on the returned result and only this
    concise summary is persisted. The format is this linker's convention, read back by the review
    queue through ``parse_link_explanation``; nothing else parses that column.

    The round trip is total, which is what lets the queue read the column at all: every value is
    stripped of the two separators before it is written, and a field that would not fit the column
    budget is dropped whole rather than cut in half. So no alias, source, or reason a provider ever
    supplies can smuggle a delimiter in and shift the fields that follow it.
    """
    rendered: list[str] = []
    length = 0
    for key, value in fields.items():
        if value in (None, ""):
            continue
        field = f"{_explanation_token(key)}={_explanation_token(value)}"
        cost = len(field) + (len(_EXPLANATION_SEPARATOR) if rendered else 0)
        if length + cost > _EXPLANATION_LIMIT:
            break
        rendered.append(field)
        length += cost
    return _EXPLANATION_SEPARATOR.join(rendered)


def _explanation_token(value: Any) -> str:
    """One key or value, with the field separators and newlines folded away to spaces."""
    text = str(value)
    for character in ("|", "=", "\n", "\r"):
        text = text.replace(character, " ")
    return " ".join(text.split())


def parse_link_explanation(explanation: str | None) -> dict[str, str]:
    """Read back the ``key=value`` fields ``format_link_explanation`` wrote."""
    fields: dict[str, str] = {}
    for part in (explanation or "").split(_EXPLANATION_SEPARATOR):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


# --- Signals -------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SignalInput:
    """The normalized view of a mention and its article that every signal reads."""

    text: str
    window_text: str
    label: EntityLabel
    co_mention_keys: Mapping[str, str]  # normalized key -> the surface it came from
    linked_entity_ids: frozenset[str]
    url_host: str | None
    edgar_ciks: frozenset[int]
    source_category: str | None
    industry_terms: frozenset[str]
    location_terms: frozenset[str]


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    """The identity-store facts about one candidate that the signals read."""

    profile: EntityProfile
    tickers: tuple[str, ...]
    alias_keys: frozenset[str]
    industries: tuple[str, ...]


@lru_cache(maxsize=512)
def _word_bounded(term: str) -> re.Pattern[str]:
    escaped = r"\s+".join(re.escape(word) for word in term.split())
    return re.compile(rf"(?<![{_WORD}]){escaped}(?![{_WORD}])", flags=re.IGNORECASE)


@lru_cache(maxsize=512)
def _ticker_patterns(ticker: str) -> tuple[re.Pattern[str], ...]:
    """A bare uppercase symbol, or the ``$TICKER`` form in any case -- never a substring.

    The bare form is case-sensitive on purpose: lowercase "it" or "all" in ordinary prose would
    otherwise match the tickers IT and ALL. Neither form matches inside a longer word, so "AAPL"
    is not found in "AAPLE".
    """
    symbol = re.escape(ticker)
    return (
        re.compile(rf"(?<![{_WORD}$]){symbol}(?![{_WORD}])"),
        re.compile(rf"(?<![{_WORD}])\${symbol}(?![{_WORD}])", flags=re.IGNORECASE),
    )


def _signal_exact_ticker(
    facts: CandidateFacts, source: SignalInput
) -> tuple[float, tuple[str, ...]]:
    matched = [
        ticker
        for ticker in facts.tickers
        if any(pattern.search(source.text) for pattern in _ticker_patterns(ticker))
    ]
    return (1.0 if matched else 0.0), tuple(sorted(matched))


def _signal_co_mentions(
    facts: CandidateFacts, source: SignalInput
) -> tuple[float, tuple[str, ...]]:
    """Other surfaces in the article that name this entity, plus entities already linked to it."""
    detail = [
        surface
        for key, surface in sorted(source.co_mention_keys.items())
        if key in facts.alias_keys
    ]
    if str(facts.profile.id) in source.linked_entity_ids:
        detail.append(f"linked:{facts.profile.id}")
    support = len(detail)
    strength = 1.0 if support >= 2 else 0.5 if support == 1 else 0.0
    return strength, tuple(detail)


def _signal_mention_context(
    facts: CandidateFacts, source: SignalInput
) -> tuple[float, tuple[str, ...]]:
    """Two checks: the window reads like a company sentence, and the label fits the entity type.

    Entity type is nullable, so the label check is only *considered* when the store knows the
    type; an unknown type neither helps nor penalizes.
    """
    detail: list[str] = []
    cues = [cue for cue in MENTION_CONTEXT_CUES if _word_bounded(cue).search(source.window_text)]
    if cues:
        detail.append(f"cue:{cues[0]}")

    entity_type = (facts.profile.entity_type or "").strip().casefold()
    considered = 1
    matched = 1 if cues else 0
    if entity_type:
        considered = 2
        if _label_agrees(source.label, entity_type):
            matched += 1
            detail.append(f"label:{source.label.value}~{entity_type}")
    return matched / considered, tuple(detail)


def _label_agrees(label: EntityLabel, entity_type: str) -> bool:
    if label in COMPANY_MENTION_LABELS:
        return entity_type in COMPANY_ENTITY_TYPES
    if label is EntityLabel.PERSON:
        return entity_type == "person"
    return entity_type in LOCATION_ENTITY_TYPES


def _signal_url_domain(facts: CandidateFacts, source: SignalInput) -> tuple[float, tuple[str, ...]]:
    """The article sits on the entity's own domain, or is an EDGAR filing under its CIK."""
    if source.url_host is None:
        return 0.0, ()
    website_host = _host_of(facts.profile.website)
    if website_host and _host_matches(source.url_host, website_host):
        return 1.0, (f"website:{website_host}",)
    cik = (facts.profile.primary_cik or "").strip()
    if (
        cik.isdigit()
        and _host_matches(source.url_host, "sec.gov")
        and int(cik) in source.edgar_ciks
    ):
        return 1.0, (f"sec_filing:{cik}",)
    return 0.0, ()


def _signal_industry_terms(
    facts: CandidateFacts, source: SignalInput
) -> tuple[float, tuple[str, ...]]:
    matched = [
        industry
        for industry in facts.industries
        if industry.casefold() in source.industry_terms
        or _word_bounded(industry).search(source.text)
    ]
    return (1.0 if matched else 0.0), tuple(sorted(matched))


def _signal_source_category(
    facts: CandidateFacts, source: SignalInput
) -> tuple[float, tuple[str, ...]]:
    entity_type = (facts.profile.entity_type or "").strip().casefold()
    financial = source.source_category in FINANCIAL_SOURCE_CATEGORIES
    if financial and entity_type in COMPANY_ENTITY_TYPES:
        return 1.0, (f"{source.source_category}:{entity_type}",)
    return 0.0, ()


def _signal_location_cues(
    facts: CandidateFacts, source: SignalInput
) -> tuple[float, tuple[str, ...]]:
    country = (facts.profile.country or "").strip().casefold()
    if country and country in source.location_terms:
        return 1.0, (country,)
    return 0.0, ()


_SIGNAL_FUNCTIONS: Final = {
    LinkSignal.EXACT_TICKER: _signal_exact_ticker,
    LinkSignal.CO_MENTIONS: _signal_co_mentions,
    LinkSignal.MENTION_CONTEXT: _signal_mention_context,
    LinkSignal.URL_DOMAIN: _signal_url_domain,
    LinkSignal.INDUSTRY_TERMS: _signal_industry_terms,
    LinkSignal.SOURCE_CATEGORY: _signal_source_category,
    LinkSignal.LOCATION_CUES: _signal_location_cues,
}


def evaluate_signals(facts: CandidateFacts, source: SignalInput) -> tuple[SignalEvidence, ...]:
    """Every signal, in weight order, fired or not -- the whole breakdown is the audit trail."""
    evidence: list[SignalEvidence] = []
    for signal, weight in SIGNAL_WEIGHTS.items():
        raw_strength, detail = _SIGNAL_FUNCTIONS[signal](facts, source)
        strength = min(max(raw_strength, 0.0), 1.0)
        evidence.append(
            SignalEvidence(
                signal=signal,
                weight=weight,
                strength=round_score(strength),
                contribution=round_score(weight * strength),
                detail=detail,
            )
        )
    return tuple(evidence)


# --- Repository ----------------------------------------------------------------
class NewsLinkingRepository:
    """Bounded reads over the identity store, with a seam for dict-backed test sessions.

    Every read is one query over an indexed column for the whole candidate set, never one query
    per candidate: the alias index (``ix_entity_aliases_normalized_alias``) carries the hot path,
    and the identity store is never loaded whole.
    """

    def __init__(self, source: Any) -> None:
        self.source = source

    def aliases_for_keys(self, keys: Iterable[str]) -> tuple[EntityAlias, ...]:
        return self._rows_in(EntityAlias, "normalized_alias", keys)

    def aliases_for_entities(self, entity_ids: Iterable[uuid.UUID]) -> tuple[EntityAlias, ...]:
        return self._rows_in(EntityAlias, "entity_id", entity_ids)

    def profiles_by_normalized_name(self, names: Iterable[str]) -> tuple[EntityProfile, ...]:
        return self._rows_in(EntityProfile, "normalized_name", names)

    def profiles_by_ids(self, entity_ids: Iterable[uuid.UUID]) -> dict[str, EntityProfile]:
        return {str(row.id): row for row in self._rows_in(EntityProfile, "id", entity_ids)}

    def redirects_from(self, entity_ids: Iterable[uuid.UUID]) -> tuple[EntityRedirect, ...]:
        return self._rows_in(EntityRedirect, "old_entity_id", entity_ids)

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        return find_one(self.source, model, **criteria)

    def add(self, obj: Any) -> Any:
        return add(self.source, obj)

    def _rows_in(self, model: type[Any], column: str, values: Iterable[Any]) -> tuple[Any, ...]:
        wanted = list(dict.fromkeys(values))
        if not wanted:
            return ()

        listing = getattr(self.source, "all_of", None)
        if callable(listing):
            keys = {str(value) for value in wanted}
            return tuple(row for row in listing(model) if str(getattr(row, column)) in keys)

        finder = getattr(self.source, "find_all", None)
        if callable(finder):
            return tuple(row for value in wanted for row in finder(model, **{column: value}))

        flush_pending(self.source)
        statement = select(model).where(getattr(model, column).in_(wanted))
        return tuple(self.source.execute(statement).scalars().all())


# --- Linker --------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _RedirectOutcome:
    """Where one candidate's redirect chain ends, or why it is not attachable."""

    chain: tuple[uuid.UUID, ...]
    reason: str | None  # None when the chain ends on a live entity


@dataclass(frozen=True, slots=True)
class _CandidateGroup:
    """Every origin that redirects onto one canonical entity, with its evidence and chains."""

    evidence: tuple[AliasEvidence, ...]
    redirect_paths: tuple[tuple[uuid.UUID, ...], ...]


class NewsEntityLinker:
    """Links extracted news mentions to canonical entities, deterministically."""

    def __init__(self, session: Any) -> None:
        self.repository = NewsLinkingRepository(session)

    def link(
        self,
        mention: EntityMention,
        context: ArticleLinkingContext,
        *,
        persist_run: bool = False,
    ) -> MentionLinkResult:
        """Link one mention: candidates, redirects, signals, band -- and optionally the run row."""
        if mention.article_key != context.article_key:
            msg = (
                "mention and linking context describe different articles: "
                f"{mention.article_key!r} vs {context.article_key!r}"
            )
            raise NewsLinkingError(msg)

        surface_key = normalize_alias(mention.text)
        if not surface_key:
            return self._finish(mention, LinkBand.NIL, REASON_NO_SURFACE, (), (), persist_run)

        groups, rejected = self._candidate_groups(mention.text, surface_key, context)
        if not groups:
            return self._finish(
                mention, LinkBand.NIL, REASON_NO_CANDIDATE, (), rejected, persist_run
            )

        candidates = self._score(mention, context, surface_key, groups)
        return self._decide(mention, context, candidates, rejected, persist_run)

    # -- candidate generation ---------------------------------------------------
    def _candidate_groups(
        self, surface: str, surface_key: str, context: ArticleLinkingContext
    ) -> tuple[dict[str, _CandidateGroup], tuple[RejectedCandidate, ...]]:
        """Alias rows (or the canonical fallback) for the surface, merged onto redirect targets.

        Several old entities can redirect onto one current entity; they merge into a single
        candidate that keeps every origin's evidence and chain.
        """
        evidence = self._alias_evidence(surface_key, context.published_on)
        if not evidence:
            evidence = self._canonical_name_evidence(surface, surface_key)
        if not evidence:
            return {}, ()

        chains = self._redirect_chains({item.entity_id for item in evidence}, context.published_on)
        merged: dict[str, list[AliasEvidence]] = {}
        paths: dict[str, list[tuple[uuid.UUID, ...]]] = {}
        rejected: list[RejectedCandidate] = []
        for item in sorted(evidence, key=_evidence_sort_key):
            outcome = chains[str(item.entity_id)]
            if outcome.reason is not None:
                rejected.append(
                    RejectedCandidate(
                        entity_id=item.entity_id, reason=outcome.reason, chain=outcome.chain
                    )
                )
                continue
            canonical = str(outcome.chain[-1])
            merged.setdefault(canonical, []).append(item)
            if len(outcome.chain) > 1 and outcome.chain not in paths.setdefault(canonical, []):
                paths[canonical].append(outcome.chain)

        groups = {
            canonical: _CandidateGroup(
                evidence=tuple(items),
                redirect_paths=tuple(sorted(paths.get(canonical, ()), key=_path_sort_key)),
            )
            for canonical, items in merged.items()
        }
        return groups, tuple(sorted(rejected, key=lambda item: (item.reason, str(item.entity_id))))

    def _alias_evidence(self, surface_key: str, as_of: datetime.date | None) -> list[AliasEvidence]:
        return [
            AliasEvidence(
                entity_id=row.entity_id,
                alias=row.alias,
                normalized_alias=row.normalized_alias,
                alias_type=row.alias_type,
                source=row.source,
                prior_multiplier=alias_prior_multiplier(row.source),
                valid_from=row.valid_from,
                valid_to=row.valid_to,
            )
            for row in self.repository.aliases_for_keys([surface_key])
            if row.normalized_alias == surface_key
            and row.alias_type in LINKABLE_ALIAS_TYPES
            and alias_is_valid(row, as_of)
        ]

    def _canonical_name_evidence(self, surface: str, surface_key: str) -> list[AliasEvidence]:
        """The controlled fallback for identity rows that carry no alias at all.

        Reachable only when no alias row matched the surface, and only through the indexed
        ``normalized_name`` column: the two keys looked up are the surface's own name key and its
        alias key, and a hit still has to normalize to the same alias key to count. That is an
        exact canonical-name match, not a scan -- a surface that is a *shortening* of an
        alias-less profile's canonical name ("Apple" against "Apple Inc.") stays unmatched, which
        is what alias rows exist for, and every identity provider writes a legal-name alias.
        """
        keys = {normalize_name(surface), surface_key}
        return [
            AliasEvidence(
                entity_id=profile.id,
                alias=profile.canonical_name,
                normalized_alias=surface_key,
                alias_type=CANONICAL_NAME_EVIDENCE_TYPE,
                source=CANONICAL_NAME_EVIDENCE_SOURCE,
                # A canonical name can itself be Wikidata-owned; the same prior applies to it.
                prior_multiplier=alias_prior_multiplier(
                    identity_sources(profile.profile_metadata).get("canonical_name")
                ),
            )
            for profile in self.repository.profiles_by_normalized_name(sorted(keys))
            if normalize_alias(profile.canonical_name) == surface_key
        ]

    def _redirect_chains(
        self, entity_ids: set[uuid.UUID], as_of: datetime.date | None
    ) -> dict[str, _RedirectOutcome]:
        """Follow every candidate's redirects as of the article date, one query per hop level.

        The hop rule is the identity store's (``services.provider_data.entity_redirects``): of the
        redirects in force at ``as_of``, only the *latest effective date* is read, and that date
        has to name exactly one target. Two distinct targets on it are not a tie to break but a
        fork nothing in the data settles — the unique key is ``(old, new, effective_date)``, so the
        schema permits both rows and neither says which merger happened. No edge is recorded for
        such an origin and neither branch is followed: choosing by id would attach the mention to a
        coin flip, and would let this linker and ``resolve_entity_redirect`` give two different
        answers over one store. A later-dated redirect settles the fork on its own, because only
        the latest date group is read at all.

        A cycle, an over-deep chain, a fork, or a hop onto a profile that is not there yields a
        reason instead of a target, and the candidate is flagged rather than attached.

        One level *past* the depth limit is expanded on purpose. ``_walk_redirects`` decides that a
        chain is over-deep by finding that it still continues after ``REDIRECT_MAX_DEPTH`` hops, so
        it has to be able to see that next edge. Stopping the search at the limit itself would hide
        the edge that proves the chain too long, and the walk would take the last node it *could*
        see for the endpoint — silently attaching the middle of a chain.
        """
        edges: dict[str, uuid.UUID] = {}
        forked: set[str] = set()
        frontier = set(entity_ids)
        expanded: set[str] = set()
        for _ in range(REDIRECT_MAX_DEPTH + 1):
            todo = {entity_id for entity_id in frontier if str(entity_id) not in expanded}
            if not todo:
                break
            expanded.update(str(entity_id) for entity_id in todo)
            rows_by_origin: dict[str, list[EntityRedirect]] = {}
            for row in self.repository.redirects_from(todo):
                if as_of is None or row.effective_date <= as_of:
                    rows_by_origin.setdefault(str(row.old_entity_id), []).append(row)
            frontier = set()
            for origin, rows in rows_by_origin.items():
                latest = max(row.effective_date for row in rows)
                targets = {row.new_entity_id for row in rows if row.effective_date == latest}
                if len(targets) > 1:
                    forked.add(origin)
                    continue
                target = targets.pop()
                edges[origin] = target
                frontier.add(target)

        outcomes = {
            str(entity_id): _walk_redirects(entity_id, edges, forked)
            for entity_id in sorted(entity_ids, key=str)
        }
        endpoints = [outcome.chain[-1] for outcome in outcomes.values() if outcome.reason is None]
        live = self.repository.profiles_by_ids(endpoints)
        return {
            key: outcome
            if outcome.reason is not None or str(outcome.chain[-1]) in live
            else _RedirectOutcome(chain=outcome.chain, reason=REDIRECT_MISSING_TARGET)
            for key, outcome in outcomes.items()
        }

    # -- scoring ----------------------------------------------------------------
    def _score(
        self,
        mention: EntityMention,
        context: ArticleLinkingContext,
        surface_key: str,
        groups: Mapping[str, _CandidateGroup],
    ) -> tuple[LinkCandidate, ...]:
        entity_ids = [uuid.UUID(key) for key in sorted(groups)]
        profiles = self.repository.profiles_by_ids(entity_ids)
        aliases_by_entity: dict[str, list[EntityAlias]] = {}
        for row in self.repository.aliases_for_entities(entity_ids):
            aliases_by_entity.setdefault(str(row.entity_id), []).append(row)

        source = _signal_input(mention, context, surface_key)
        candidates: list[LinkCandidate] = []
        for key in sorted(groups):
            profile = profiles.get(key)
            if profile is None:  # defensive: an alias row whose profile is gone links to nothing
                continue
            group = groups[key]
            evidence = sorted(group.evidence, key=_evidence_sort_key)
            strongest = evidence[0]
            facts = _candidate_facts(profile, aliases_by_entity.get(key, ()), context.published_on)
            signals = evaluate_signals(facts, source)
            signal_score = round_score(math.fsum(signal.contribution for signal in signals))
            score = round_score(min(max(signal_score * strongest.prior_multiplier, 0.0), 1.0))
            origins = {item.entity_id for item in evidence if str(item.entity_id) != key}
            candidates.append(
                LinkCandidate(
                    entity_id=profile.id,
                    canonical_name=profile.canonical_name,
                    entity_type=profile.entity_type,
                    country=profile.country,
                    ticker=profile.primary_ticker,
                    industries=facts.industries,
                    evidence=strongest,
                    alternate_evidence=tuple(evidence[1:]),
                    redirected_from=tuple(sorted(origins, key=str)),
                    redirect_paths=group.redirect_paths,
                    signals=signals,
                    prior_multiplier=strongest.prior_multiplier,
                    signal_score=signal_score,
                    score=score,
                    band=band_for_score(score),
                )
            )
        return tuple(sorted(candidates, key=_candidate_sort_key))

    # -- band decision ----------------------------------------------------------
    def _decide(
        self,
        mention: EntityMention,
        context: ArticleLinkingContext,
        candidates: tuple[LinkCandidate, ...],
        rejected: tuple[RejectedCandidate, ...],
        persist_run: bool,
    ) -> MentionLinkResult:
        if not candidates:
            return self._finish(
                mention, LinkBand.NIL, REASON_NO_CANDIDATE, (), rejected, persist_run
            )

        band, reason = decide_band(candidates)
        limit = context.max_candidates or get_settings().entity_link_max_candidates
        return self._finish(
            mention, band, reason, candidates[:limit], rejected, persist_run, top=candidates[0]
        )

    # -- result assembly and persistence ----------------------------------------
    def _finish(
        self,
        mention: EntityMention,
        band: LinkBand,
        reason: str,
        candidates: tuple[LinkCandidate, ...],
        rejected: tuple[RejectedCandidate, ...],
        persist_run: bool,
        *,
        top: LinkCandidate | None = None,
    ) -> MentionLinkResult:
        matched_entity_id = top.entity_id if (top is not None and band is LinkBand.ACCEPT) else None
        score = top.score if top is not None else None
        result = MentionLinkResult(
            article_key=mention.article_key,
            surface=mention.text,
            normalized_surface=normalize_alias(mention.text),
            start_char=mention.start_char,
            end_char=mention.end_char,
            label=mention.label,
            assertion_status=mention.assertion_status,
            band=band,
            matched_entity_id=matched_entity_id,
            confidence_score=score,
            reason=reason,
            run_key=mention_run_key(mention),
            target_id=mention_target_id(mention),
            explanation=_explanation(band, reason, score, matched_entity_id, candidates, top),
            candidates=candidates,
            rejected=rejected,
        )
        if persist_run:
            self._persist(result)
        return result

    def _persist(self, result: MentionLinkResult) -> EntityResolutionRun:
        """Write one run per mention, keyed by article and character span, idempotently.

        Only a deterministic ACCEPT stores a ``matched_entity_id``; every band stores its score
        and a concise explanation, so the ambiguous and NIL mentions are auditable (and are what
        the review queue reads) without ever having attached an entity.
        """
        values = {
            "target_type": NEWS_MENTION_TARGET_TYPE,
            "target_id": result.target_id,
            "input_names": json_safe([result.surface]),
            "matched_entity_id": result.matched_entity_id,
            "confidence_score": result.confidence_score,
            "explanation": result.explanation,
        }
        existing = self.repository.find_one(EntityResolutionRun, run_key=result.run_key)
        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
            return existing
        return self.repository.add(
            EntityResolutionRun(id=uuid.uuid4(), run_key=result.run_key, **values)
        )


def link_mention(
    session: Any,
    mention: EntityMention,
    context: ArticleLinkingContext,
    *,
    persist_run: bool = False,
) -> MentionLinkResult:
    """Link one extracted mention against the identity store (ADR 0005 stage 2)."""
    return NewsEntityLinker(session).link(mention, context, persist_run=persist_run)


def link_mentions(
    session: Any,
    mentions: Sequence[EntityMention],
    context: ArticleLinkingContext,
    *,
    persist_run: bool = False,
) -> tuple[MentionLinkResult, ...]:
    """Link an article's mentions in the order given, reusing one repository."""
    linker = NewsEntityLinker(session)
    return tuple(linker.link(mention, context, persist_run=persist_run) for mention in mentions)


def mention_target_id(mention: EntityMention) -> str:
    """The stable identity of a mention: its article, plus its character span."""
    return f"{mention.article_key}#{mention.start_char}-{mention.end_char}"


def mention_run_key(mention: EntityMention) -> str:
    """The idempotency key of a mention's resolution run: same mention, same row."""
    return idempotency_key(
        NEWS_MENTION_TARGET_TYPE,
        "link",
        mention.article_key,
        mention.start_char,
        mention.end_char,
        normalize_alias(mention.text),
    )


def alias_is_valid(alias: EntityAlias, as_of: datetime.date | None) -> bool:
    """True when the article date falls inside the alias' validity interval.

    An article with no publication date cannot be dated against an interval, so no interval
    filter applies; the alias type (``former_name``) still records what the evidence was.
    """
    if as_of is None:
        return True
    if alias.valid_from is not None and alias.valid_from > as_of:
        return False
    return not (alias.valid_to is not None and alias.valid_to < as_of)


# --- Helpers -------------------------------------------------------------------
def _explanation(
    band: LinkBand,
    reason: str,
    score: float | None,
    matched_entity_id: uuid.UUID | None,
    candidates: tuple[LinkCandidate, ...],
    top: LinkCandidate | None,
) -> str:
    return format_link_explanation(
        {
            "band": band.value,
            "score": "" if score is None else f"{score:.4f}",
            "reason": reason,
            "entity": matched_entity_id,
            "alias": "" if top is None else f"{top.evidence.alias_type}/{top.evidence.source}",
            "candidates": len(candidates),
            "signals": ",".join(
                f"{signal.signal.value}:{signal.contribution:.2f}"
                for signal in (() if top is None else top.signals)
                if signal.contribution > 0
            ),
        }
    )


def _walk_redirects(
    entity_id: uuid.UUID, edges: Mapping[str, uuid.UUID], forked: Container[str]
) -> _RedirectOutcome:
    """Follow the edges from one origin: to its endpoint, or to the reason it has none.

    A chain ends where an entity has no redirect out of it, so the *absence* of a next edge is the
    only thing that terminates a walk successfully. A node in ``forked`` has redirects out of it
    and still no next edge, so it is checked before that absence is read as an ending: the fork is
    a hole in the chain rather than the end of one, and the walk stops *on* it instead of picking a
    branch through it. That holds at the first hop and at every hop after it, because the check is
    made at each node the walk stands on rather than only at the one it started from.

    Running out of allowed hops while an edge is still there is the over-deep case, and the two are
    never conflated: the last node of a chain that is still going is not an endpoint, and is never
    returned as one.
    """
    chain = [entity_id]
    seen = {str(entity_id)}
    while True:
        origin = str(chain[-1])
        if origin in forked:
            return _RedirectOutcome(chain=tuple(chain), reason=REDIRECT_AMBIGUOUS)
        target = edges.get(origin)
        if target is None:
            return _RedirectOutcome(chain=tuple(chain), reason=None)
        if str(target) in seen:
            return _RedirectOutcome(chain=(*chain, target), reason=REDIRECT_CYCLE)
        if len(chain) > REDIRECT_MAX_DEPTH:
            return _RedirectOutcome(chain=tuple(chain), reason=REDIRECT_TOO_DEEP)
        chain.append(target)
        seen.add(str(target))


def _evidence_sort_key(evidence: AliasEvidence) -> tuple[float, int, int, str, str]:
    """Strongest evidence first: a full prior beats the Wikidata discount, then the alias type,
    then the ADR 0006 source precedence (SEC > GLEIF > Wikidata), then the surface itself."""
    return (
        -evidence.prior_multiplier,
        ALIAS_TYPE_RANK.get(evidence.alias_type, len(ALIAS_TYPE_RANK)),
        identity_precedence(evidence.source),
        evidence.alias,
        str(evidence.entity_id),
    )


def _candidate_sort_key(candidate: LinkCandidate) -> tuple[float, str, str]:
    return (-candidate.score, candidate.canonical_name, str(candidate.entity_id))


def _path_sort_key(path: tuple[uuid.UUID, ...]) -> tuple[str, ...]:
    return tuple(str(entity_id) for entity_id in path)


def _signal_input(
    mention: EntityMention, context: ArticleLinkingContext, surface_key: str
) -> SignalInput:
    co_mention_keys: dict[str, str] = {}
    for surface in context.co_mention_surfaces:
        key = normalize_alias(surface)
        if key and key != surface_key:
            co_mention_keys.setdefault(key, surface)
    return SignalInput(
        text=context.text if context.text else mention.sentence.window_text,
        window_text=mention.sentence.window_text,
        label=mention.label,
        co_mention_keys=co_mention_keys,
        linked_entity_ids=frozenset(str(entity_id) for entity_id in context.linked_entity_ids),
        url_host=_host_of(context.url),
        edgar_ciks=_edgar_ciks(context.url),
        source_category=(context.source_category or "").strip().casefold() or None,
        industry_terms=frozenset(_clean_terms(context.industry_terms)),
        location_terms=frozenset(_clean_terms(context.location_terms)),
    )


def _clean_terms(terms: Sequence[str]) -> list[str]:
    return [term.strip().casefold() for term in terms if term and term.strip()]


def _candidate_facts(
    profile: EntityProfile, aliases: Sequence[EntityAlias], as_of: datetime.date | None
) -> CandidateFacts:
    valid = [alias for alias in aliases if alias_is_valid(alias, as_of)]
    raw_tickers = {profile.primary_ticker or ""} | {
        alias.alias for alias in valid if alias.alias_type == "ticker"
    }
    symbols = {ticker.strip().upper() for ticker in raw_tickers if ticker.strip()}
    keys = {alias.normalized_alias for alias in valid if alias.alias_type in LINKABLE_ALIAS_TYPES}
    keys.add(normalize_alias(profile.canonical_name))
    return CandidateFacts(
        profile=profile,
        tickers=tuple(sorted(symbol for symbol in symbols if _TICKER_PATTERN.match(symbol))),
        alias_keys=frozenset(key for key in keys if key),
        industries=_profile_industries(profile),
    )


def _profile_industries(profile: EntityProfile) -> tuple[str, ...]:
    """The industry labels identity ingestion retained as provider metadata (ADR 0006)."""
    metadata = json_safe(profile.profile_metadata)
    if not isinstance(metadata, Mapping):
        return ()
    labels: set[str] = set()
    for value in metadata.values():
        if not isinstance(value, Mapping):
            continue
        for industry in value.get("industries") or ():
            label = industry.get("label") if isinstance(industry, Mapping) else None
            if isinstance(label, str) and label.strip():
                labels.add(label.strip())
    return tuple(sorted(labels))


def _host_of(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    candidate = url.strip()
    if "//" not in candidate:
        candidate = f"//{candidate}"
    host = (urlsplit(candidate).hostname or "").casefold()
    return host.removeprefix("www.") or None


def _host_matches(article_host: str, entity_host: str) -> bool:
    return article_host == entity_host or article_host.endswith(f".{entity_host}")


def _edgar_ciks(url: str | None) -> frozenset[int]:
    """The CIKs an EDGAR URL names: its ``/data/<cik>/`` path segment, and any ``cik`` query param.

    Only those two positions are read. Every other digit run in an EDGAR URL -- the accession
    number, the year, the form type in ``type=10-K`` -- is not a CIK, and treating one as if it
    were would hand the 0.15 URL signal to whichever low-numbered CIK happened to appear in an
    unrelated company's filing path.
    """
    if not url:
        return frozenset()
    parts = urlsplit(url if "//" in url else f"//{url}")
    segments = parts.path.split("/")
    named = {
        segments[index + 1]
        for index, segment in enumerate(segments[:-1])
        if segment.casefold() == "data"
    }
    named.update(value for key, value in parse_qsl(parts.query) if key.casefold() == "cik")
    return frozenset(int(value) for value in named if value.isdigit())
