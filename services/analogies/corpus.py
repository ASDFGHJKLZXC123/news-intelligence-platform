"""The curated historical-episode corpus and its gold set: pure load and validation.

No database, no network, no embedding call. Everything this module can reject, it rejects before
the seeder opens a connection or the evaluator spends a token, because the failures that matter
here are data failures: a duplicate id, a child seeded before its parent, an outcome sentence that
leaked into an onset field. A corpus that is wrong in those ways would still *seed*, and would
then quietly return wrong analogies forever.

Three rules are enforced mechanically rather than left to the reviewer, because they are the ones
a human eye slides over:

* **No hindsight in onset text.** ``onset_summary`` and the indicator labels may not contain a
  hindsight phrase, and may not mention a year later than the year of ``onset_date`` -- a live
  observer in August 2007 could not write "2008". Outcome facts live in the outcome fields, which
  are never embedded (``services.nlp.embedding_text.build_episode_onset_text`` cannot even be
  passed one).
* **Indicators are signals, not results.** Every indicator's ``as_of`` must fall inside the onset
  window (:data:`INDICATOR_WINDOW_DAYS` after ``onset_date``, and no more than
  :data:`INDICATOR_LOOKBACK_YEARS` before it). A "signal visible at onset" dated six months into
  the episode is a result wearing a signal's clothes.
* **Quotas.** The spec's curation plan is a contract, not an aspiration: N in 80-120, >=60
  crisis/stress rows, >=25% counterexamples, >=20 non-US, every episode type present with real
  breadth. :func:`validate_corpus` fails if the committed data drifts out of it.

Review state is recorded, not asserted: the committed corpus is ``llm_drafted_pending_human_review``
and says so on every row. A row may only claim ``human_reviewed`` when it names a real reviewer, a
date, and a completed checklist -- so the format cannot be used to manufacture a sign-off that did
not happen.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from db.models.core import EPISODE_OUTCOMES, EPISODE_TYPES
from services.nlp.episodes import EpisodeRecord

#: Product data lives next to the seed script (with the authoring template); code lives here.
CORPUS_DIR = Path(__file__).resolve().parents[2] / "db" / "seed" / "episodes"
GOLD_PATH = CORPUS_DIR / "analogy_gold.json"
TEMPLATE_PATH = CORPUS_DIR / "authoring_template.json"

#: Only numbered files are corpus data, so the template and the gold set can sit beside them.
#: Sorted filename order *is* seed order, and ``00_arcs.json`` holds every parent.
CORPUS_GLOB = "[0-9][0-9]_*.json"

SCHEMA_VERSION = 1
INDICATORS_SCHEMA = "onset_indicators_v1"

# --- curation quotas (historical-episode spec, "Curation plan (MVP)") ---------------------
CORPUS_MIN, CORPUS_MAX = 80, 120
#: The committed size. Fixed so a quota audit is arithmetic, not judgement.
CORPUS_TARGET = 100
MIN_CRISIS_EPISODES = 60
MIN_COUNTEREXAMPLE_RATE = 0.25

#: What makes a row a *crisis/stress* episode, encoded rather than assumed. The spec's curation
#: plan splits the corpus into "~60 crisis/stress episodes" plus the counterexamples that fight doom
#: bias, so "crisis" has to be something a row demonstrably carries -- not merely the absence of a
#: near-miss label. An episode qualifies when it is not a counterexample *and* at least one of its
#: outcome tags is adverse. Counting non-counterexamples instead would let a row whose outcomes are
#: only `contained`/`recovery` -- a benign episode nobody called a near-miss -- pad the quota.
BENIGN_OUTCOMES = frozenset({"contained", "recovery"})
ADVERSE_OUTCOMES = frozenset(EPISODE_OUTCOMES) - BENIGN_OUTCOMES
MIN_NON_US_EPISODES = 20
#: Breadth floor per family: "meaningful breadth rather than one token row".
MIN_EPISODES_PER_TYPE = 5
GOLD_MIN, GOLD_MAX = 30, 50

#: An indicator must be observable when the episode was identified, not after it resolved.
INDICATOR_WINDOW_DAYS = 14
INDICATOR_LOOKBACK_YEARS = 5

# --- controlled vocabularies ---------------------------------------------------------------
#: Region codes sit alongside ISO-3166 alpha-2 so a genuinely multi-country episode need not be
#: mislabelled as one country's.
REGIONS = frozenset({"GLOBAL", "EU", "EA", "ASIA", "LATAM", "MENA", "AFRICA"})
COUNTRIES = frozenset(
    {
        "AR", "CH", "CN", "CY", "DE", "EG", "ES", "FR", "GB", "GR", "HK", "IE", "IS", "IT",
        "JP", "KR", "LK", "LV", "MX", "PT", "RU", "SA", "SE", "TH", "TW", "UA", "US",
    }
)
GEOGRAPHIES = REGIONS | COUNTRIES
#: "at least 20 non-US episodes (not merely globally scoped rows labelled US)": a global row is
#: not evidence of non-US coverage either, so only a specific non-US geography counts.
NON_US_EXCLUDED = frozenset({"US", "GLOBAL"})

INDUSTRIES = frozenset(
    {
        "aerospace", "agriculture", "airlines", "asset_management", "automotive", "banking",
        "chemicals", "commodities_trading", "construction", "crypto", "defense",
        "electric_utilities", "electronics", "energy", "exchanges", "food", "healthcare",
        "hedge_funds", "hospitality", "housing", "insurance", "livestock", "logistics",
        "manufacturing", "media", "metals", "mining", "money_markets", "municipal_finance",
        "natural_gas", "oil_gas", "payments", "pharmaceuticals", "public_health", "rail",
        "real_estate", "retail", "semiconductors", "shipping", "software", "sovereign_finance",
        "technology", "telecom", "tourism", "transport", "utilities",
    }
)

REGIME_TAGS = frozenset(
    {
        "advanced_economy", "basel_ii", "basel_iii", "bretton_woods", "capital_controls",
        "central_bank_backstop", "currency_union", "deposit_insurance", "emerging_market",
        "fixed_exchange_rate", "floating_exchange_rate", "globalized_supply_chain",
        "gold_standard", "high_inflation", "post_dodd_frank", "post_fiat", "post_ihr_2005",
        "post_qe", "post_social_media", "pre_basel", "pre_deposit_insurance", "pre_dodd_frank",
        "pre_fiat", "pre_ihr_2005", "pre_qe", "pre_social_media", "zirp",
    }
)

INDICATOR_UNITS = frozenset(
    {
        "basis_points", "boolean", "cases", "category", "chf_billions", "count", "days", "deaths",
        "eur_billions", "eur_per_mwh", "gbp_billions", "index_level", "million_barrels_per_day",
        "minutes", "months", "multiple", "people", "percent", "percent_of_gdp",
        "percentage_points", "ratio", "usd_billions", "usd_millions", "usd_per_barrel",
        "usd_trillions", "weeks", "years",
    }
)

SOURCE_KINDS = frozenset({"primary", "institutional", "statistical"})

#: Central banks, regulators, governments and the IMF/World Bank/BIS/WHO family. A source outside
#: this list is not automatically wrong, but it is not automatically auditable either, so it has to
#: be added here deliberately. Matched as a domain suffix; this is also what stops a placeholder
#: domain from being committed.
SOURCE_DOMAINS = frozenset(
    {
        "adb.org", "bafin.de", "bancaditalia.it", "bank.lv", "bankofengland.co.uk",
        "banxico.org.mx", "bcra.gob.ar", "bde.es", "bis.org", "boj.or.jp", "bok.or.kr",
        "bot.or.th", "bportugal.pt", "bundestag.de", "cbe.org.eg", "cbo.gov", "cbr.ru",
        "cbsl.gov.lk", "cdc.gov", "centralbank.ie", "cftc.gov", "cisa.gov", "clevelandfed.org",
        "commerce.gov", "eia.gov", "epa.gov", "eurocontrol.int", "europa.eu", "faa.gov",
        "fao.org", "fca.org.uk", "fcc.gov", "fdic.gov", "fdicoig.gov", "federalreserve.gov",
        "federalreservehistory.org", "finma.ch", "fsb.org", "gao.gov", "gov.uk", "govinfo.gov",
        "hkexnews.hk", "hkma.gov.hk", "icao.int", "iea.org", "imf.org", "imo.org", "iss.it",
        "justice.gov", "kdb.co.kr", "meti.go.jp", "nber.org", "newyorkfed.org", "ntsb.gov",
        "oecd.org", "opec.org", "parliament.uk", "pbc.gov.cn", "riksbank.se", "safe.gov.cn",
        "sec.gov", "sedlabanki.is", "snb.ch", "state.gov", "stlouisfed.org", "treasury.gov",
        "un.org", "unctad.org", "usda.gov", "ustr.gov", "who.int", "woah.org", "worldbank.org",
        "wto.org",
    }
)

REVIEW_STATUSES = frozenset(
    {"llm_drafted_pending_human_review", "human_reviewed", "needs_revision"}
)
HUMAN_REVIEWED = "human_reviewed"
REVIEW_CHECKLIST_KEYS = (
    "onset_as_if_live",
    "onset_outcome_separation",
    "dates_verified",
    "indicators_verified",
    "sources_verified",
    "outcome_tags_verified",
    "license_original_prose",
)

#: Phrases that can only be written by someone who already knows how it ended.
HINDSIGHT_PHRASES = (
    "in hindsight",
    "in retrospect",
    "with hindsight",
    "as it turned out",
    "turned out to",
    "would later",
    "would eventually",
    "went on to",
    "later collapsed",
    "later failed",
    "proved to be",
    "marked the beginning",
    "precursor to",
    "foreshadow",
    "the aftermath",
    "months later",
    "weeks later",
    "years later",
    "days later",
    "eventually",
    "ultimately",
    "subsequently",
)

_YEAR = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b")
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_EPISODE_KEYS = frozenset(
    {
        "id", "slug", "name", "episode_type", "parent_slug", "onset_date", "peak_date",
        "end_date", "geography", "affected_industries", "regime_tags", "is_counterexample",
        "onset_summary", "onset_indicators", "outcome_summary", "outcomes",
        "resolution_mechanism", "source_refs", "license_note", "version", "updated_at",
        "review_due_at", "review",
    }
)
_REQUIRED_EPISODE_KEYS = _EPISODE_KEYS - {"parent_slug", "peak_date", "end_date"}
_INHERITABLE = ("license_note", "version", "updated_at", "review_due_at", "review")

_GOLD_KEYS = frozenset(
    {
        "pair_id", "title", "summary", "as_of", "episode_types", "regime_tags", "geographies",
        "industries", "expected_episode_ids", "acceptable_episode_ids", "expects_counterexample",
        "notes",
    }
)


class CorpusValidationError(ValueError):
    """The committed corpus or gold set is not usable. Carries every problem found, not the first.

    Raised before any database or network work: a bad corpus must never reach an embedding call.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        listed = "\n  - ".join(self.problems)
        super().__init__(f"{len(self.problems)} corpus validation problem(s):\n  - {listed}")


@dataclass(frozen=True)
class CuratedEpisode:
    """One validated curated episode, plus the curation-only fields the table does not hold."""

    episode_id: uuid.UUID
    slug: str
    name: str
    episode_type: str
    onset_date: datetime.date
    peak_date: datetime.date | None
    end_date: datetime.date | None
    geography: str
    affected_industries: tuple[str, ...]
    regime_tags: tuple[str, ...]
    is_counterexample: bool
    onset_summary: str
    onset_indicators: dict[str, Any]
    outcome_summary: str
    outcomes: tuple[str, ...]
    resolution_mechanism: str
    #: ``{"sources": [...], "review": {...}}`` -- the JSONB column carries the provenance *and*
    #: the review state, so an operator can audit a served analogy without the corpus file.
    source_refs: dict[str, Any]
    license_note: str
    version: int
    updated_at: datetime.date
    review_due_at: datetime.date
    review: dict[str, Any]
    parent_slug: str | None = None
    parent_episode_id: uuid.UUID | None = None
    source_file: str = ""

    @property
    def is_human_reviewed(self) -> bool:
        return self.review.get("status") == HUMAN_REVIEWED

    @property
    def is_crisis(self) -> bool:
        """A crisis/stress episode: not a curated near-miss, and it carries an adverse outcome.

        A counterexample is excluded by definition -- the spec calls it a near-miss "that resolved
        benignly", which is the opposite of the bucket -- and it stays excluded even when it carries
        an adverse tag, because an episode can be contained *via* a bailout and still be the benign
        outcome the corpus holds it up as.
        """
        return not self.is_counterexample and bool(ADVERSE_OUTCOMES & set(self.outcomes))

    def to_record(self) -> EpisodeRecord:
        """The item-1 write contract. Everything curation owns, and nothing it does not."""
        return EpisodeRecord(
            episode_id=self.episode_id,
            name=self.name,
            episode_type=self.episode_type,
            onset_date=self.onset_date,
            peak_date=self.peak_date,
            end_date=self.end_date,
            onset_summary=self.onset_summary,
            onset_indicators=self.onset_indicators,
            outcome_summary=self.outcome_summary,
            outcomes=list(self.outcomes),
            resolution_mechanism=self.resolution_mechanism,
            geography=self.geography,
            affected_industries=list(self.affected_industries),
            regime_tags=list(self.regime_tags),
            parent_episode_id=self.parent_episode_id,
            is_counterexample=self.is_counterexample,
            source_refs=self.source_refs,
            license_note=self.license_note,
            version=self.version,
            review_due_at=self.review_due_at,
        )


@dataclass(frozen=True)
class CorpusQuotas:
    """The audit the spec's curation plan asks for. Reported on every seed and every validate."""

    total: int
    crisis: int
    counterexamples: int
    counterexample_rate: float
    non_us: int
    parents: int
    children: int
    human_reviewed: int
    by_type: dict[str, int] = field(default_factory=dict)
    by_geography: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "crisis": self.crisis,
            "counterexamples": self.counterexamples,
            "counterexample_rate": round(self.counterexample_rate, 4),
            "non_us": self.non_us,
            "parents": self.parents,
            "children": self.children,
            "human_reviewed": self.human_reviewed,
            "by_type": dict(sorted(self.by_type.items())),
            "by_geography": dict(sorted(self.by_geography.items())),
        }


@dataclass(frozen=True)
class EpisodeCorpus:
    """The whole corpus in seed order: parents first, then children, deterministically."""

    episodes: tuple[CuratedEpisode, ...]
    quotas: CorpusQuotas

    @property
    def by_id(self) -> dict[uuid.UUID, CuratedEpisode]:
        return {episode.episode_id: episode for episode in self.episodes}

    @property
    def by_slug(self) -> dict[str, CuratedEpisode]:
        return {episode.slug: episode for episode in self.episodes}

    @property
    def parent_ids(self) -> frozenset[uuid.UUID]:
        """Episodes that parent another row. Retrieval never ranks these."""
        return frozenset(
            episode.parent_episode_id
            for episode in self.episodes
            if episode.parent_episode_id is not None
        )

    def records(self) -> tuple[EpisodeRecord, ...]:
        return tuple(episode.to_record() for episode in self.episodes)


@dataclass(frozen=True)
class GoldPair:
    """One labelled (current event -> correct analogy) pair. Evaluation data, never training."""

    pair_id: str
    title: str
    summary: str
    as_of: datetime.date
    episode_types: tuple[str, ...]
    regime_tags: tuple[str, ...]
    geographies: tuple[str, ...] | None
    industries: tuple[str, ...] | None
    expected_episode_ids: tuple[uuid.UUID, ...]
    acceptable_episode_ids: tuple[uuid.UUID, ...]
    expects_counterexample: bool
    notes: str

    @property
    def correct_episode_ids(self) -> frozenset[uuid.UUID]:
        """Primary and acceptable together: a hit on any of them is a hit."""
        return frozenset(self.expected_episode_ids) | frozenset(self.acceptable_episode_ids)


@dataclass(frozen=True)
class GoldSet:
    pairs: tuple[GoldPair, ...]

    @property
    def covered_types(self) -> frozenset[str]:
        return frozenset(t for pair in self.pairs for t in pair.episode_types)

    def coverage(self) -> dict[str, Any]:
        return {
            "pairs": len(self.pairs),
            "episode_types": sorted(self.covered_types),
            "counterexample_pairs": sum(1 for p in self.pairs if p.expects_counterexample),
            "expected_ids": len({i for p in self.pairs for i in p.expected_episode_ids}),
        }


# --- primitive checks ----------------------------------------------------------------------
def _date(value: Any, label: str, problems: list[str]) -> datetime.date | None:
    if not isinstance(value, str):
        problems.append(f"{label}: expected an ISO date string, got {type(value).__name__}")
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        problems.append(f"{label}: {value!r} is not an ISO date")
        return None


def _tokens(
    value: Any, label: str, vocabulary: frozenset[str], problems: list[str], *, minimum: int = 1
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{label}: expected a list of strings")
        return ()
    unknown = [v for v in value if v not in vocabulary]
    if unknown:
        problems.append(f"{label}: unknown value(s) {', '.join(sorted(unknown))}")
    if len(value) < minimum:
        problems.append(f"{label}: needs at least {minimum} value(s)")
    if len(set(value)) != len(value):
        problems.append(f"{label}: contains duplicates")
    return tuple(value)


def find_hindsight_phrases(text: str) -> tuple[str, ...]:
    """Hindsight phrases present in ``text``. Public: the same check guards gold query text."""
    lowered = text.lower()
    return tuple(phrase for phrase in HINDSIGHT_PHRASES if phrase in lowered)


def find_future_years(text: str, not_after: int) -> tuple[int, ...]:
    """Years in ``text`` later than ``not_after``. A live observer cannot cite next year."""
    return tuple(
        sorted({year for year in (int(m) for m in _YEAR.findall(text)) if year > not_after})
    )


def _check_as_if_live(text: str, onset_year: int, label: str, problems: list[str]) -> None:
    for phrase in find_hindsight_phrases(text):
        problems.append(f"{label}: hindsight phrase {phrase!r} in text that is embedded as onset")
    for year in find_future_years(text, onset_year):
        problems.append(f"{label}: mentions {year}, later than the onset year {onset_year}")


def _check_sources(raw: Any, label: str, problems: list[str]) -> tuple[list[dict], set[str]]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{label}: source_refs must be a non-empty list")
        return [], set()

    sources: list[dict] = []
    ids: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{label}.source_refs[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where}: must be an object")
            continue
        missing = {"id", "title", "publisher", "url", "accessed", "kind"} - set(item)
        if missing:
            problems.append(f"{where}: missing {', '.join(sorted(missing))}")
            continue
        source_id = item["id"]
        if source_id in ids:
            problems.append(f"{where}: duplicate source id {source_id!r}")
        ids.add(source_id)
        if item["kind"] not in SOURCE_KINDS:
            problems.append(f"{where}: unknown source kind {item['kind']!r}")
        parsed = urlparse(str(item["url"]))
        host = parsed.netloc.lower().removeprefix("www.")
        if parsed.scheme != "https":
            problems.append(f"{where}: url must be https")
        if not any(host == d or host.endswith(f".{d}") for d in SOURCE_DOMAINS):
            problems.append(f"{where}: {host!r} is not an allowlisted institutional source domain")
        _date(item["accessed"], f"{where}.accessed", problems)
        sources.append(dict(item))
    return sources, ids


def _check_indicators(
    raw: Any,
    *,
    label: str,
    onset: datetime.date,
    source_ids: set[str],
    problems: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, list) or len(raw) < 2:
        problems.append(f"{label}: onset_indicators must be a list of at least 2 typed signals")
        return {"schema": INDICATORS_SCHEMA, "indicators": []}

    earliest = onset.replace(year=onset.year - INDICATOR_LOOKBACK_YEARS)
    latest = onset + datetime.timedelta(days=INDICATOR_WINDOW_DAYS)
    indicators: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{label}.onset_indicators[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where}: must be an object")
            continue
        missing = {"key", "label", "value", "unit", "as_of"} - set(item)
        if missing:
            problems.append(f"{where}: missing {', '.join(sorted(missing))}")
            continue
        if set(item) - {"key", "label", "value", "unit", "as_of", "source_id"}:
            problems.append(f"{where}: unknown indicator field(s)")
        key = item["key"]
        if not isinstance(key, str) or not _SNAKE.match(key):
            problems.append(f"{where}: key {key!r} is not snake_case")
        elif key in keys:
            problems.append(f"{where}: duplicate indicator key {key!r}")
        keys.add(key)
        if item["unit"] not in INDICATOR_UNITS:
            problems.append(f"{where}: unknown unit {item['unit']!r}")
        if not isinstance(item["value"], bool | int | float | str):
            problems.append(f"{where}: value must be a number, string or boolean")
        as_of = _date(item["as_of"], f"{where}.as_of", problems)
        if as_of is not None and not earliest <= as_of <= latest:
            problems.append(
                f"{where}: as_of {as_of} is outside the onset window "
                f"({earliest}..{latest}); an onset indicator cannot be dated after the episode"
            )
        source_id = item.get("source_id")
        if source_id is not None and source_id not in source_ids:
            problems.append(f"{where}: source_id {source_id!r} is not one of this episode's sources")
        text = f"{item['label']} {item['value'] if isinstance(item['value'], str) else ''}"
        _check_as_if_live(text, onset.year, where, problems)
        indicators.append(
            {
                "key": item["key"],
                "label": item["label"],
                "value": item["value"],
                "unit": item["unit"],
                "as_of": item["as_of"],
                "source_id": source_id,
            }
        )
    # A fixed wrapper key, so the serialized text that gets embedded is stable and self-describing.
    return {"schema": INDICATORS_SCHEMA, "indicators": indicators}


def _check_review(raw: Any, label: str, problems: list[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        problems.append(f"{label}: review block is missing")
        return {}
    status = raw.get("status")
    if status not in REVIEW_STATUSES:
        problems.append(f"{label}: unknown review status {status!r}")
    checklist = raw.get("checklist")
    if not isinstance(checklist, Mapping) or set(checklist) != set(REVIEW_CHECKLIST_KEYS):
        problems.append(
            f"{label}: review checklist must hold exactly {', '.join(REVIEW_CHECKLIST_KEYS)}"
        )
        checklist = {}
    elif not all(isinstance(v, bool) for v in checklist.values()):
        problems.append(f"{label}: review checklist values must be booleans")

    signoff = raw.get("human_signoff")
    if not isinstance(signoff, Mapping) or set(signoff) != {"reviewer", "reviewed_at", "decision"}:
        problems.append(f"{label}: human_signoff must hold reviewer, reviewed_at and decision")
        return dict(raw)

    if status == HUMAN_REVIEWED:
        # The one rule that stops the format being used to fake a sign-off.
        if not signoff.get("reviewer") or not signoff.get("reviewed_at"):
            problems.append(
                f"{label}: status is {HUMAN_REVIEWED!r} but no reviewer/date is recorded"
            )
        if checklist and not all(checklist.values()):
            problems.append(
                f"{label}: status is {HUMAN_REVIEWED!r} but the checklist is not complete"
            )
    elif signoff.get("reviewer"):
        problems.append(f"{label}: a reviewer is named but the status is not {HUMAN_REVIEWED!r}")
    return dict(raw)


def _check_episode(
    raw: Mapping[str, Any], defaults: Mapping[str, Any], source_file: str, problems: list[str]
) -> CuratedEpisode | None:
    slug = raw.get("slug", "<no slug>")
    label = f"{source_file}:{slug}"

    merged: dict[str, Any] = {key: defaults[key] for key in _INHERITABLE if key in defaults}
    merged.update(raw)

    unknown = set(merged) - _EPISODE_KEYS
    if unknown:
        problems.append(f"{label}: unknown field(s) {', '.join(sorted(unknown))}")
    missing = _REQUIRED_EPISODE_KEYS - set(merged)
    if missing:
        problems.append(f"{label}: missing field(s) {', '.join(sorted(missing))}")
        return None

    try:
        episode_id = uuid.UUID(str(merged["id"]))
    except ValueError:
        problems.append(f"{label}: id {merged['id']!r} is not a UUID")
        return None

    if not isinstance(slug, str) or not _SLUG.match(slug):
        problems.append(f"{label}: slug is not kebab-case")
    if merged["episode_type"] not in EPISODE_TYPES:
        problems.append(f"{label}: unknown episode_type {merged['episode_type']!r}")
    if merged["geography"] not in GEOGRAPHIES:
        problems.append(f"{label}: unknown geography {merged['geography']!r}")
    if not isinstance(merged["is_counterexample"], bool):
        problems.append(f"{label}: is_counterexample must be a boolean")
    if not isinstance(merged["version"], int) or merged["version"] < 1:
        problems.append(f"{label}: version must be a positive integer")

    onset = _date(merged["onset_date"], f"{label}.onset_date", problems)
    peak = _date(merged["peak_date"], f"{label}.peak_date", problems) if merged.get("peak_date") else None
    end = _date(merged["end_date"], f"{label}.end_date", problems) if merged.get("end_date") else None
    updated_at = _date(merged["updated_at"], f"{label}.updated_at", problems)
    review_due = _date(merged["review_due_at"], f"{label}.review_due_at", problems)
    if onset is None or updated_at is None or review_due is None:
        return None
    if peak is not None and peak < onset:
        problems.append(f"{label}: peak_date {peak} precedes onset_date {onset}")
    if end is not None and end < onset:
        problems.append(f"{label}: end_date {end} precedes onset_date {onset}")
    if peak is not None and end is not None and peak > end:
        problems.append(f"{label}: peak_date {peak} follows end_date {end}")

    industries = _tokens(merged["affected_industries"], f"{label}.affected_industries", INDUSTRIES, problems)
    regimes = _tokens(merged["regime_tags"], f"{label}.regime_tags", REGIME_TAGS, problems)
    outcomes = _tokens(merged["outcomes"], f"{label}.outcomes", frozenset(EPISODE_OUTCOMES), problems)

    onset_summary = merged["onset_summary"]
    outcome_summary = merged["outcome_summary"]
    resolution = merged["resolution_mechanism"]
    for name, text in (
        ("onset_summary", onset_summary),
        ("outcome_summary", outcome_summary),
        ("resolution_mechanism", resolution),
    ):
        if not isinstance(text, str) or len(text.strip()) < 40:
            problems.append(f"{label}.{name}: must be substantive prose")

    if isinstance(onset_summary, str):
        _check_as_if_live(onset_summary, onset.year, f"{label}.onset_summary", problems)
        if isinstance(merged["name"], str) and merged["name"].lower() in onset_summary.lower():
            # The name is hindsight-laden and is deliberately never embedded; it must not sneak in.
            problems.append(f"{label}.onset_summary: repeats the episode name")

    license_note = merged["license_note"]
    if not isinstance(license_note, str) or "original prose" not in license_note.lower():
        problems.append(f"{label}: license_note must state the narrative is original prose")

    sources, source_ids = _check_sources(merged["source_refs"], label, problems)
    indicators = _check_indicators(
        merged["onset_indicators"], label=label, onset=onset, source_ids=source_ids, problems=problems
    )
    review = _check_review(merged.get("review"), label, problems)

    parent_slug = merged.get("parent_slug")
    if parent_slug is not None and not isinstance(parent_slug, str):
        problems.append(f"{label}: parent_slug must be a slug or absent")
        parent_slug = None

    return CuratedEpisode(
        episode_id=episode_id,
        slug=slug,
        name=merged["name"],
        episode_type=merged["episode_type"],
        onset_date=onset,
        peak_date=peak,
        end_date=end,
        geography=merged["geography"],
        affected_industries=industries,
        regime_tags=regimes,
        is_counterexample=bool(merged["is_counterexample"]),
        onset_summary=onset_summary,
        onset_indicators=indicators,
        outcome_summary=outcome_summary,
        outcomes=outcomes,
        resolution_mechanism=resolution,
        source_refs={"sources": sources, "review": review},
        license_note=license_note,
        version=int(merged["version"]),
        updated_at=updated_at,
        review_due_at=review_due,
        review=review,
        parent_slug=parent_slug,
        source_file=source_file,
    )


def _link_parents(episodes: list[CuratedEpisode], problems: list[str]) -> list[CuratedEpisode]:
    """Resolve parent_slug to an id, enforcing existence, seed order and acyclicity."""
    by_slug = {episode.slug: episode for episode in episodes}
    seen: set[str] = set()
    linked: list[CuratedEpisode] = []
    for episode in episodes:
        parent_id: uuid.UUID | None = None
        if episode.parent_slug is not None:
            parent = by_slug.get(episode.parent_slug)
            if parent is None:
                problems.append(f"{episode.slug}: parent {episode.parent_slug!r} does not exist")
            elif parent.slug == episode.slug:
                problems.append(f"{episode.slug}: is its own parent")
            elif parent.slug not in seen:
                # Foreign key on a fresh database: the parent row must already be inserted.
                problems.append(
                    f"{episode.slug}: parent {parent.slug!r} is seeded after it "
                    "(parents belong in 00_arcs.json)"
                )
            elif parent.parent_slug is not None:
                # One level only: retrieval excludes any row that parents another, so a
                # grandparent would silently remove its child from ranking too.
                problems.append(f"{episode.slug}: parent {parent.slug!r} is itself a child")
            if parent is not None:
                parent_id = parent.episode_id
        seen.add(episode.slug)
        linked.append(
            episode if parent_id is None else _with_parent(episode, parent_id)
        )
    return linked


def _with_parent(episode: CuratedEpisode, parent_id: uuid.UUID) -> CuratedEpisode:
    return CuratedEpisode(
        **{
            **{f: getattr(episode, f) for f in episode.__dataclass_fields__},
            "parent_episode_id": parent_id,
        }
    )


def _quotas(episodes: Sequence[CuratedEpisode]) -> CorpusQuotas:
    parents = {e.parent_episode_id for e in episodes if e.parent_episode_id is not None}
    counterexamples = sum(1 for e in episodes if e.is_counterexample)
    total = len(episodes)
    return CorpusQuotas(
        total=total,
        # Derived from the outcome tags each row carries, not from `total - counterexamples`.
        crisis=sum(1 for e in episodes if e.is_crisis),
        counterexamples=counterexamples,
        counterexample_rate=(counterexamples / total) if total else 0.0,
        non_us=sum(1 for e in episodes if e.geography not in NON_US_EXCLUDED),
        parents=len(parents),
        children=sum(1 for e in episodes if e.parent_episode_id is not None),
        human_reviewed=sum(1 for e in episodes if e.is_human_reviewed),
        by_type=dict(Counter(e.episode_type for e in episodes)),
        by_geography=dict(Counter(e.geography for e in episodes)),
    )


def _check_quotas(quotas: CorpusQuotas, problems: list[str]) -> None:
    if not CORPUS_MIN <= quotas.total <= CORPUS_MAX:
        problems.append(
            f"corpus holds {quotas.total} episodes; the spec requires {CORPUS_MIN}-{CORPUS_MAX}"
        )
    if quotas.crisis < MIN_CRISIS_EPISODES:
        problems.append(
            f"corpus holds {quotas.crisis} crisis/stress episodes (not a counterexample, and "
            f"carrying at least one of: {', '.join(sorted(ADVERSE_OUTCOMES))}); at least "
            f"{MIN_CRISIS_EPISODES} are required"
        )
    if quotas.counterexample_rate < MIN_COUNTEREXAMPLE_RATE:
        problems.append(
            f"counterexamples are {quotas.counterexample_rate:.1%} of the corpus; at least "
            f"{MIN_COUNTEREXAMPLE_RATE:.0%} are required to fight doom bias"
        )
    if quotas.non_us < MIN_NON_US_EPISODES:
        problems.append(
            f"corpus holds {quotas.non_us} episodes with a specific non-US geography; "
            f"at least {MIN_NON_US_EPISODES} are required"
        )
    for episode_type in EPISODE_TYPES:
        count = quotas.by_type.get(episode_type, 0)
        if count < MIN_EPISODES_PER_TYPE:
            problems.append(
                f"episode type {episode_type!r} has {count} episode(s); at least "
                f"{MIN_EPISODES_PER_TYPE} are required for meaningful breadth"
            )


def load_corpus(directory: Path = CORPUS_DIR) -> EpisodeCorpus:
    """Load, validate and order the curated corpus. Pure: no database, no network, no API key."""
    problems: list[str] = []
    files = sorted(directory.glob(CORPUS_GLOB))
    if not files:
        raise CorpusValidationError([f"no corpus files matching {CORPUS_GLOB} in {directory}"])

    episodes: list[CuratedEpisode] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: unreadable ({exc})")
            continue
        if payload.get("schema_version") != SCHEMA_VERSION:
            problems.append(f"{path.name}: schema_version must be {SCHEMA_VERSION}")
        defaults = payload.get("defaults", {})
        for raw in payload.get("episodes", []):
            episode = _check_episode(raw, defaults, path.name, problems)
            if episode is not None:
                episodes.append(episode)

    seen_ids: dict[uuid.UUID, str] = {}
    seen_slugs: set[str] = set()
    seen_names: set[str] = set()
    for episode in episodes:
        if episode.episode_id in seen_ids:
            problems.append(
                f"{episode.slug}: duplicate id, already used by {seen_ids[episode.episode_id]}"
            )
        seen_ids[episode.episode_id] = episode.slug
        if episode.slug in seen_slugs:
            problems.append(f"{episode.slug}: duplicate slug")
        seen_slugs.add(episode.slug)
        if episode.name in seen_names:
            problems.append(f"{episode.slug}: duplicate name {episode.name!r}")
        seen_names.add(episode.name)

    episodes = _link_parents(episodes, problems)
    quotas = _quotas(episodes)
    _check_quotas(quotas, problems)
    if problems:
        raise CorpusValidationError(problems)
    return EpisodeCorpus(episodes=tuple(episodes), quotas=quotas)


def _check_gold_pair(
    raw: Mapping[str, Any], corpus: EpisodeCorpus, problems: list[str]
) -> GoldPair | None:
    pair_id = raw.get("pair_id", "<no id>")
    unknown = set(raw) - _GOLD_KEYS
    missing = _GOLD_KEYS - set(raw)
    if unknown:
        problems.append(f"{pair_id}: unknown field(s) {', '.join(sorted(unknown))}")
    if missing:
        problems.append(f"{pair_id}: missing field(s) {', '.join(sorted(missing))}")
        return None

    as_of = _date(raw["as_of"], f"{pair_id}.as_of", problems)
    if as_of is None:
        return None

    types = _tokens(raw["episode_types"], f"{pair_id}.episode_types", frozenset(EPISODE_TYPES), problems)
    regimes = _tokens(raw["regime_tags"], f"{pair_id}.regime_tags", REGIME_TAGS, problems)
    geographies = (
        None
        if raw["geographies"] is None
        else _tokens(raw["geographies"], f"{pair_id}.geographies", GEOGRAPHIES, problems)
    )
    industries = (
        None
        if raw["industries"] is None
        else _tokens(raw["industries"], f"{pair_id}.industries", INDUSTRIES, problems)
    )

    for name in ("title", "summary"):
        text = raw[name]
        if not isinstance(text, str) or len(text.strip()) < 20:
            problems.append(f"{pair_id}.{name}: must be substantive as-if-live text")
            continue
        # The query text is embedded. Outcome leakage here would score the retriever on a question
        # no live event can ask.
        for phrase in find_hindsight_phrases(text):
            problems.append(f"{pair_id}.{name}: hindsight phrase {phrase!r} leaks the outcome")
        for year in find_future_years(text, as_of.year):
            problems.append(f"{pair_id}.{name}: mentions {year}, later than the query year")

    known = corpus.by_id
    parents = corpus.parent_ids
    ids: dict[str, tuple[uuid.UUID, ...]] = {}
    for name in ("expected_episode_ids", "acceptable_episode_ids"):
        raw_ids = raw[name]
        if not isinstance(raw_ids, list):
            problems.append(f"{pair_id}.{name}: must be a list")
            return None
        parsed: list[uuid.UUID] = []
        for value in raw_ids:
            try:
                episode_id = uuid.UUID(str(value))
            except ValueError:
                problems.append(f"{pair_id}.{name}: {value!r} is not a UUID")
                continue
            episode = known.get(episode_id)
            if episode is None:
                problems.append(f"{pair_id}.{name}: {episode_id} is not in the corpus")
                continue
            if episode_id in parents:
                # Retrieval ranks leaves only; a parent could never be returned, so labelling one
                # as the right answer would make the pair permanently unwinnable.
                problems.append(
                    f"{pair_id}.{name}: {episode.slug!r} is a parent arc and is never ranked"
                )
            if episode.episode_type not in types:
                problems.append(
                    f"{pair_id}.{name}: {episode.slug!r} is a {episode.episode_type} episode, "
                    f"which the pair's filter ({', '.join(types)}) excludes"
                )
            parsed.append(episode_id)
        ids[name] = tuple(parsed)

    if not ids["expected_episode_ids"]:
        problems.append(f"{pair_id}: needs at least one expected episode")
    overlap = set(ids["expected_episode_ids"]) & set(ids["acceptable_episode_ids"])
    if overlap:
        problems.append(f"{pair_id}: episode(s) listed as both expected and acceptable")
    if not isinstance(raw["expects_counterexample"], bool):
        problems.append(f"{pair_id}: expects_counterexample must be a boolean")

    return GoldPair(
        pair_id=pair_id,
        title=raw["title"],
        summary=raw["summary"],
        as_of=as_of,
        episode_types=types,
        regime_tags=regimes,
        geographies=geographies,
        industries=industries,
        expected_episode_ids=ids["expected_episode_ids"],
        acceptable_episode_ids=ids["acceptable_episode_ids"],
        expects_counterexample=bool(raw["expects_counterexample"]),
        notes=raw["notes"],
    )


def load_gold_set(corpus: EpisodeCorpus, path: Path = GOLD_PATH) -> GoldSet:
    """Load and validate the labelled retrieval pairs against a corpus. Pure."""
    problems: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusValidationError([f"{path.name}: unreadable ({exc})"]) from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{path.name}: schema_version must be {SCHEMA_VERSION}")

    pairs: list[GoldPair] = []
    seen: set[str] = set()
    for raw in payload.get("pairs", []):
        pair = _check_gold_pair(raw, corpus, problems)
        if pair is None:
            continue
        if pair.pair_id in seen:
            problems.append(f"{pair.pair_id}: duplicate pair id")
        seen.add(pair.pair_id)
        pairs.append(pair)

    if not GOLD_MIN <= len(pairs) <= GOLD_MAX:
        problems.append(
            f"gold set holds {len(pairs)} pairs; the spec requires {GOLD_MIN}-{GOLD_MAX}"
        )
    covered = {t for pair in pairs for t in pair.episode_types}
    uncovered = set(EPISODE_TYPES) - covered
    if uncovered:
        problems.append(f"gold set covers no {', '.join(sorted(uncovered))} pair")
    if not any(pair.expects_counterexample for pair in pairs):
        problems.append("gold set contains no counterexample pair")
    if not any(not pair.expects_counterexample for pair in pairs):
        problems.append("gold set contains no crisis pair")

    targets = {i for pair in pairs for i in pair.correct_episode_ids}
    by_id = corpus.by_id
    if not any(by_id[i].geography in NON_US_EXCLUDED for i in targets):
        problems.append("gold set targets no US or global episode")
    if not any(by_id[i].geography not in NON_US_EXCLUDED for i in targets):
        problems.append("gold set targets no non-US episode")

    if problems:
        raise CorpusValidationError(problems)
    return GoldSet(pairs=tuple(pairs))


def load_corpus_and_gold(
    directory: Path = CORPUS_DIR, gold_path: Path = GOLD_PATH
) -> tuple[EpisodeCorpus, GoldSet]:
    """The offline entry point the seeder, the evaluator and the tests all share."""
    corpus = load_corpus(directory)
    return corpus, load_gold_set(corpus, gold_path)


def unreviewed_episodes(corpus: EpisodeCorpus) -> tuple[CuratedEpisode, ...]:
    """The rows no human has verified. The seed gate's input (spec, "Curation plan (MVP)").

    The spec's workflow has three steps -- LLM drafts, *a human verifies*, then it is committed via
    the seed script -- and the middle one is the only thing standing between a plausible-sounding
    generated onset paragraph and a production corpus that will be served as historical fact. So the
    step is reified: a row is unreviewed until its own ``review`` block says otherwise, and
    :func:`_check_review` will not let that block claim a sign-off without a real reviewer, a date
    and a complete checklist.
    """
    return tuple(episode for episode in corpus.episodes if not episode.is_human_reviewed)


def quota_report(corpus: EpisodeCorpus, gold: GoldSet | None = None) -> dict[str, Any]:
    """A deterministic, JSON-serializable audit of the committed data."""
    report: dict[str, Any] = {"corpus": corpus.quotas.as_dict()}
    report["corpus"]["review_status"] = dict(
        sorted(Counter(e.review.get("status") for e in corpus.episodes).items())
    )
    if gold is not None:
        report["gold"] = gold.coverage()
    return report


__all__ = [
    "ADVERSE_OUTCOMES",
    "BENIGN_OUTCOMES",
    "CORPUS_DIR",
    "CORPUS_MAX",
    "CORPUS_MIN",
    "CORPUS_TARGET",
    "GOLD_MAX",
    "GOLD_MIN",
    "GOLD_PATH",
    "HUMAN_REVIEWED",
    "INDICATORS_SCHEMA",
    "INDICATOR_UNITS",
    "MIN_COUNTEREXAMPLE_RATE",
    "MIN_CRISIS_EPISODES",
    "MIN_EPISODES_PER_TYPE",
    "MIN_NON_US_EPISODES",
    "NON_US_EXCLUDED",
    "REVIEW_CHECKLIST_KEYS",
    "REVIEW_STATUSES",
    "TEMPLATE_PATH",
    "CorpusQuotas",
    "CorpusValidationError",
    "CuratedEpisode",
    "EpisodeCorpus",
    "GoldPair",
    "GoldSet",
    "find_future_years",
    "find_hindsight_phrases",
    "load_corpus",
    "load_corpus_and_gold",
    "load_gold_set",
    "quota_report",
    "unreviewed_episodes",
]
