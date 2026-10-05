"""Composition context: evidence, historical parallels, and forecasts, as pure values.

The second half of Stage 6's deterministic input work. :mod:`services.reports.selection`
decided *which events* the brief covers; this module decides *what may be said about them* --
and, just as importantly, what may not.

Three separations are enforced by the shape of the types here, not by discipline at the call
site:

* **Evidence is linked, supportive, and excerpted.** A claim reaches the brief only along the
  path `Event -> Article -> EvidenceItem -> ClaimEvidence -> Claim`. A claim nothing links to
  an article of a selected event is not this brief's claim, a `contradicts` link is not
  support, and no article body is ever carried whole -- only a bounded excerpt that records
  where it came from and whether it was cut.
* **Historical onset is not historical outcome.** :class:`HistoricalOnset` has no field an
  outcome could be put in and :class:`HistoricalOutcomeContext` has no field a forecast could
  be put in, mirroring the discipline `services.analogies.contracts` enforces on the retrieval
  path. What happened in 1998 is hindsight about 1998; it is never this event's forecast.
* **An episode id is not a claim id.** They are different types of citation and the brief's
  claim-level citations (report-generation spec) resolve through `claim_evidence` to articles.
  An episode id put in that list would resolve to nothing -- so no analogy value here carries
  a claim-id field at all.

Nothing in this module imports SQLAlchemy. :mod:`services.reports.context_repository` loads
rows into these shapes; the grouping, de-duplication, ordering, bounding and validation below
are pure functions over them, testable against hand-built rows with no database in sight.

This stage selects context. It does not compose: no prose, no LLM, no grounding, no claim of
being grounded.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, Protocol, TypeAlias, TypeVar, runtime_checkable

from db.models.core import SCENARIO_NAMES
from services.reports.contracts import DataQuality, DataQualityNote, SelectedEvent

_T = TypeVar("_T")

#: A JSON value after immutable normalization: scalars unchanged, arrays are tuples, objects are
#: read-only mappings, all the way down. Every value a consumer can reach through a normalized
#: `evidence_refs`/`source_refs` has this shape, so a composer cannot mutate one in place.
ImmutableJSON: TypeAlias = (
    str | int | float | bool | None | tuple["ImmutableJSON", ...] | Mapping[str, "ImmutableJSON"]
)


def freeze_json(value: object) -> ImmutableJSON:
    """Recursively deep-freeze a JSON-shaped value read from the store.

    The composition context is frozen dataclasses, but a frozen dataclass whose field holds a
    ``dict`` or ``list`` is frozen only at the top: the composer could still mutate the JSONB
    provenance the loader handed it, and -- worse -- that container may be the ORM's own,
    mutating a row the context promised never to write through. This normalizes the value into
    something with no mutable handle at any depth:

    * scalars (``str``/``int``/``float``/``bool``/``None``) pass through unchanged;
    * sequences become ``tuple``s, order preserved (a JSON array is ordered);
    * mappings become read-only :class:`types.MappingProxyType`, keys sorted so that two rows
      with the same content but different key insertion order normalize identically.

    The result shares no mutable container with the input, so nothing the loader read can be
    reached and changed through the returned value.
    """
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return value  # type: ignore[return-value]
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_json(item) for key, item in sorted(value.items(), key=lambda kv: kv[0])}
        )
    if isinstance(value, Iterable):
        return tuple(freeze_json(item) for item in value)
    # An unknown, non-JSON object (e.g. a Decimal): leave it, rather than guessing a coercion.
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class BoundedRead(Generic[_T]):
    """A deterministic, bounded slice of a global scan, and whether the bound cut it off.

    A repository read that simply returned "the rows" would give the reducer no way to tell a
    complete result from one the scan's backstop ``LIMIT`` silently clipped -- and a clipped
    context reads exactly like a small one. This carries the answer explicitly.

    ``truncated`` is *not* inferred from ``len(rows) == limit`` -- a scan that returns exactly
    ``limit`` rows is complete, not truncated. The loader over-reads by one row and reports the
    bound as reached only when that extra row actually exists, then drops it, so ``rows`` never
    holds more than ``limit`` and a full-but-not-truncated scan is never mislabelled.
    """

    rows: tuple[_T, ...]
    limit: int
    truncated: bool

    @classmethod
    def of(cls, fetched: Sequence[_T], limit: int) -> BoundedRead[_T]:
        """Build from an over-read of up to ``limit + 1`` rows: keep ``limit``, flag the rest."""
        return cls(rows=tuple(fetched[:limit]), limit=limit, truncated=len(fetched) > limit)


# --------------------------------------------------------------------------------------
# Vocabularies the loaders join on
# --------------------------------------------------------------------------------------

#: The `claim_evidence.support_type` values that count as *support* for a claim.
#:
#: The column is free text -- `String(32)`, no CHECK -- and the vocabulary is documented rather
#: than constrained ("supports, contradicts, contextual"; database-setup plan, phase-4 log). So
#: the brief pins the values it trusts here instead of trusting the column.
#:
#: `contextual` is deliberately *not* support: evidence that supplies background for a claim is
#: not evidence that the claim is true, and a brief that cited it as support would be claiming
#: a grounding it does not have. `contradicts` is excluded for the obvious reason -- it is the
#: opposite of the thing being selected, and letting it in would let a refutation be printed as
#: a citation.
SUPPORTIVE_SUPPORT_TYPES: frozenset[str] = frozenset({"supports"})

#: The single `evidence_items.source_type` value that means "this evidence item *is* a news
#: article", whose `source_id` is therefore an `articles.id`.
#:
#: Exact by requirement: only `article` qualifies. The join `evidence_items.source_id =
#: articles.id` narrows to article-backed items, but it is not the discriminator on its own --
#: an `rss` (or any other typed) evidence item whose `source_id` happens to equal a linked
#: article's UUID would satisfy the cast join, so the brief would cite it as an article it is
#: not. The `source_type == 'article'` equality is what keeps that out. The canonical schema's
#: legacy `article -> rss` mapping is deliberately not honoured here: this pins the exact value
#: the claim-extraction writer must use, and the loader matches it and nothing else.
ARTICLE_EVIDENCE_SOURCE_TYPE = "article"

#: `forecast_scenarios` probabilities are MECE: "probabilities sum to 1.0 +/- 0.01"
#: (`services.llm.contracts`, which validates the same rule on the way in). Totalled as
#: `Decimal` for the same reason it is there: 0.35 + 0.35 + 0.2 + 0.1 is 0.9999999999999999 in
#: binary float, and a set the writer accepted must not be rejected on the way out by a
#: rounding error the writer did not have.
PROBABILITY_SUM = Decimal("1.0")
PROBABILITY_TOLERANCE = Decimal("0.01")

#: A complete set names every scenario exactly once. `forecast_scenarios` already has a unique
#: constraint on `(scenario_set_id, scenario_name)`, so "every name present" is the only part
#: that can fail in the database -- but the set is validated here anyway, because a partially
#: written set (the writer crashed between rows) is exactly the shape that constraint permits.
REQUIRED_SCENARIO_NAMES: frozenset[str] = frozenset(SCENARIO_NAMES)


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------

#: Copyright rule, report-generation spec: exports carry "snippets <= 200 chars" and never
#: article full text. The brief's own excerpts are held to the export's bound, so that nothing
#: can enter the pipeline that the export would have to strip back out.
MAX_EXCERPT_CHARS = 200

#: Per-event and global bounds. Explicit, because an unbounded context is how a composer prompt
#: silently becomes a 200k-token bill, and because a bound that is reached must be *reported*
#: (`EVIDENCE_TRUNCATED`) -- a truncated context that says nothing reads exactly like a
#: complete one.
MAX_CLAIMS_PER_EVENT = 10
MAX_LINKS_PER_CLAIM = 5
MAX_ANALOGIES_PER_EVENT = 3


class ExcerptOrigin(StrEnum):
    """Where a source excerpt was taken from. Recorded, never inferred.

    The distinction matters to the grounding gate: a summary is *our* compression of an
    article and a body excerpt is the publisher's own words, and the >15-consecutive-words
    copyright check (report-generation spec) means the difference between them is not cosmetic.
    """

    SUMMARY = "summary"
    BODY = "body"
    #: Publisher wording retained from an RSS/Atom feed for a personal snapshot.
    PUBLISHER_RSS = "publisher_rss"
    #: The article carries neither a summary nor a body. An honest empty, not an empty string.
    NONE = "none"


@dataclass(frozen=True)
class SourceExcerpt:
    """A bounded quotation from one article, with its provenance and whether it was cut."""

    text: str
    origin: ExcerptOrigin
    truncated: bool

    @property
    def is_empty(self) -> bool:
        return self.origin is ExcerptOrigin.NONE


def build_source_excerpt(
    summary_head: str | None,
    body_head: str | None,
    *,
    summary_length: int | None = None,
    body_length: int | None = None,
    limit: int = MAX_EXCERPT_CHARS,
) -> SourceExcerpt:
    """Prefer the summary; fall back to a bounded head of the body; never the whole body.

    Both inputs are already short -- the repository reads `left(col, limit + 1)` so that a full
    article body never crosses the database boundary in the first place, and therefore can never
    be held in memory or leaked into a prompt.

    The ``*_length`` arguments are the *true* column lengths, and truncation is decided on them
    rather than on the slice. Deciding it on the slice would be wrong in a way that is easy to
    miss: a body of 300 characters that begins with three spaces arrives as a 201-character head
    whose stripped length is 198, which is not longer than the limit -- so the excerpt would be
    silently cut and reported as complete. The length comes from the database because only the
    database can see the whole column.

    The cut itself is deterministic: a hard character bound at ``limit``, no word-boundary search
    and no ellipsis. A word-boundary cut would make the excerpt a function of the text's spacing,
    and two runs over the same article must produce the same excerpt.
    """
    cleaned_summary = (summary_head or "").strip()
    if cleaned_summary:
        full = summary_length if summary_length is not None else len(cleaned_summary)
        return _cut(cleaned_summary, ExcerptOrigin.SUMMARY, limit, full_length=full)

    cleaned_body = (body_head or "").strip()
    if cleaned_body:
        full = body_length if body_length is not None else len(cleaned_body)
        return _cut(cleaned_body, ExcerptOrigin.BODY, limit, full_length=full)

    return SourceExcerpt(text="", origin=ExcerptOrigin.NONE, truncated=False)


def _cut(text: str, origin: ExcerptOrigin, limit: int, *, full_length: int) -> SourceExcerpt:
    return SourceExcerpt(
        text=text[:limit],
        origin=origin,
        truncated=full_length > limit,
    )


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceArticle:
    """The article a claim's supporting evidence item points at. Never its body."""

    article_id: uuid.UUID
    title: str
    publisher: str | None
    url: str | None
    published_at: datetime.datetime | None
    source_credibility: float | None
    excerpt: SourceExcerpt


@dataclass(frozen=True)
class EvidenceLink:
    """One `claim_evidence` row, resolved: this claim is supported by this article."""

    article: EvidenceArticle
    support_type: str
    support_confidence: float | None


@dataclass(frozen=True)
class ClaimContext:
    """One claim, with every article that supports it.

    ``links`` is plural on purpose. A claim two outlets both reported is better evidenced than
    one only a single outlet carried, and collapsing its links to one would erase exactly that
    difference -- so de-duplication removes *identical* links, never the second article.
    """

    claim_id: uuid.UUID
    claim_text: str
    claim_type: str | None
    claim_confidence: float | None
    links: tuple[EvidenceLink, ...]

    @property
    def article_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(link.article.article_id for link in self.links)


@dataclass(frozen=True)
class EventEvidenceContext:
    """Every supportive, article-linked claim the brief may cite for one selected event."""

    event_id: uuid.UUID
    claims: tuple[ClaimContext, ...]
    #: The per-event claim bound was reached and claims were dropped.
    truncated: bool = False

    @property
    def claim_ids(self) -> tuple[uuid.UUID, ...]:
        """The brief's claim-level citations for this event (report-generation spec).

        Claim ids only -- never an episode id. They resolve through `claim_evidence` to
        articles, which an episode id cannot do.
        """
        return tuple(claim.claim_id for claim in self.claims)


@dataclass(frozen=True)
class EvidenceRow:
    """One flat `claim -> support -> article` link for one event, as loaded.

    The repository emits these; :func:`build_evidence_context` groups, de-duplicates, orders
    and bounds them. Flat, because the join is flat: a claim supported by three articles of two
    events arrives as six rows, and every reduction of that is a decision made here.
    """

    event_id: uuid.UUID
    claim_id: uuid.UUID
    claim_text: str
    claim_type: str | None
    claim_confidence: float | None
    support_type: str
    support_confidence: float | None
    article_id: uuid.UUID
    article_title: str
    publisher: str | None
    url: str | None
    published_at: datetime.datetime | None
    source_credibility: float | None
    excerpt: SourceExcerpt


def is_supportive(support_type: str) -> bool:
    """Only `supports` supports. See :data:`SUPPORTIVE_SUPPORT_TYPES`."""
    return support_type in SUPPORTIVE_SUPPORT_TYPES


_FAR_PAST = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def _link_order(link: EvidenceLink) -> tuple[float, str]:
    """Newest supporting article first; ties by article id, which is stable across runs."""
    published = link.article.published_at
    when = published.timestamp() if published is not None else _FAR_PAST.timestamp()
    return (-when, str(link.article.article_id))


def _claim_order(claim: ClaimContext) -> tuple[float, int, str]:
    """Most-confident claims first, then the best-evidenced, then by id.

    A NULL confidence sorts last rather than as zero: an unscored claim is not a claim that
    scored badly, but the brief still cannot rank it above one that scored well.
    """
    confidence = claim.claim_confidence
    return (
        -(confidence if confidence is not None else -1.0),
        -len(claim.links),
        str(claim.claim_id),
    )


def build_evidence_context(
    rows: Iterable[EvidenceRow],
    *,
    max_claims_per_event: int = MAX_CLAIMS_PER_EVENT,
    max_links_per_claim: int = MAX_LINKS_PER_CLAIM,
) -> tuple[tuple[EventEvidenceContext, ...], tuple[DataQualityNote, ...]]:
    """Group flat evidence rows into one bounded, ordered context per event.

    Filters (defence in depth -- the repository's SQL already applies the same rule, and this
    re-applies it so that a hand-built row, a widened query, or a future caller cannot slip a
    contradiction into a citation): only supportive links survive.

    De-duplication is on ``(event_id, claim_id, article_id, support_type)``. The `event_id` is
    in the identity on purpose: one claim/article link can legitimately belong to two selected
    events, and each event's context must keep it. Keying on `claim_evidence`'s primary key
    alone -- ``(claim_id, article_id, support_type)`` -- would let the first event's row swallow
    the second's, so the same evidence would silently vanish from the second event's context.
    Two rows that differ only because the claim reached the event through two different articles
    are *not* duplicates and both are kept.
    """
    by_event: dict[uuid.UUID, dict[uuid.UUID, list[EvidenceRow]]] = {}
    seen: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]] = set()

    for row in rows:
        if not is_supportive(row.support_type):
            continue
        key = (row.event_id, row.claim_id, row.article_id, row.support_type)
        if key in seen:
            continue
        seen.add(key)
        by_event.setdefault(row.event_id, {}).setdefault(row.claim_id, []).append(row)

    contexts: list[EventEvidenceContext] = []
    notes: list[DataQualityNote] = []

    for event_id in sorted(by_event, key=str):
        # Each claim carries its dropped-link count alongside it: a link cut by the per-claim
        # bound must be disclosed, not just the claims cut by the per-event bound. The count is
        # tallied only for claims that *survive* the per-event bound -- a dropped claim's links
        # are already accounted for by the claim being dropped, and counting them twice would
        # overstate the loss.
        built: list[tuple[ClaimContext, int]] = []
        for claim_id, claim_rows in by_event[event_id].items():
            head = claim_rows[0]
            links = [
                EvidenceLink(
                    article=EvidenceArticle(
                        article_id=row.article_id,
                        title=row.article_title,
                        publisher=row.publisher,
                        url=row.url,
                        published_at=row.published_at,
                        source_credibility=row.source_credibility,
                        excerpt=row.excerpt,
                    ),
                    support_type=row.support_type,
                    support_confidence=row.support_confidence,
                )
                for row in claim_rows
            ]
            links.sort(key=_link_order)
            links_dropped = max(0, len(links) - max_links_per_claim)
            built.append(
                (
                    ClaimContext(
                        claim_id=claim_id,
                        claim_text=head.claim_text,
                        claim_type=head.claim_type,
                        claim_confidence=head.claim_confidence,
                        links=tuple(links[:max_links_per_claim]),
                    ),
                    links_dropped,
                )
            )
        built.sort(key=lambda item: _claim_order(item[0]))
        kept = built[:max_claims_per_event]
        claims_dropped = len(built) - len(kept)
        links_dropped = sum(dropped for _, dropped in kept)
        truncated = claims_dropped > 0 or links_dropped > 0
        if truncated:
            parts: list[str] = []
            if claims_dropped:
                parts.append(
                    f"{claims_dropped} lower-confidence claim(s) beyond the "
                    f"{max_claims_per_event}-claim bound"
                )
            if links_dropped:
                parts.append(
                    f"{links_dropped} supporting link(s) beyond the "
                    f"{max_links_per_claim}-link-per-claim bound"
                )
            notes.append(
                DataQualityNote(
                    DataQuality.EVIDENCE_TRUNCATED,
                    f"Event {event_id}: {' and '.join(parts)} were dropped from the evidence "
                    "context.",
                )
            )
        contexts.append(
            EventEvidenceContext(
                event_id=event_id, claims=tuple(claim for claim, _ in kept), truncated=truncated
            )
        )

    return tuple(contexts), tuple(notes)


def evidence_notes_for_events(
    events: Sequence[SelectedEvent], contexts: Sequence[EventEvidenceContext]
) -> tuple[DataQualityNote, ...]:
    """Declare every selected event the brief has no citable evidence for.

    A Top Event with no supportive linked claim is not a bug in itself -- the claim-extraction
    stage may simply not have run on it -- but a brief that writes about it anyway would be
    writing ungrounded prose about its lead story. ADR 0009: never present partial data as
    complete.
    """
    with_evidence = {context.event_id for context in contexts if context.claims}
    missing = [event for event in events if event.event_id not in with_evidence]
    if not missing:
        return ()
    titles = ", ".join(f"{event.title!r}" for event in missing)
    return (
        DataQualityNote(
            DataQuality.NO_EVIDENCE_FOR_EVENT,
            f"{len(missing)} selected event(s) have no supportive article-linked claim and "
            f"cannot be cited: {titles}.",
        ),
    )


# --------------------------------------------------------------------------------------
# Historical parallels
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParentEpisode:
    """The arc a matched child episode sits inside. Context, never itself a parallel."""

    episode_id: uuid.UUID
    name: str
    onset_summary: str


@dataclass(frozen=True)
class HistoricalOnset:
    """How the episode *began* -- the only half of it that is comparable to a live event.

    There is deliberately no outcome field. The comparison the brief draws is onset-to-onset;
    an onset that arrived carrying its own ending would invite the composer to write the ending
    as though it were this event's.
    """

    episode_id: uuid.UUID
    name: str
    episode_type: str
    onset_date: datetime.date
    onset_summary: str
    geography: str | None
    regime_tags: tuple[str, ...]
    #: The episode is curated as a case that looked similar and *did not* end the same way.
    #: A load-bearing marker: it is the one analogy whose outcome argues against the parallel.
    is_counterexample: bool
    #: Provenance JSON, deep-frozen on construction (:func:`freeze_json`) so the composer holds
    #: no mutable handle into it and cannot mutate the ORM row it was read from.
    source_refs: ImmutableJSON | None
    parent: ParentEpisode | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", freeze_json(self.source_refs))


@dataclass(frozen=True)
class HistoricalOutcomeContext:
    """What became of the episode. Hindsight about the past, labelled as such.

    Never a forecast. The forecast of the live event is :class:`EventForecast`, which comes from
    `forecast_scenarios` and from nowhere else; that this type has no probability, no horizon
    and no scenario name is what keeps the two from being confused.
    """

    outcome_summary: str | None
    outcomes: tuple[str, ...]
    resolution_mechanism: str | None
    peak_date: datetime.date | None
    end_date: datetime.date | None


@dataclass(frozen=True)
class AnalogyContext:
    """One persisted `event_analogies` row, resolved against its episode.

    Carries no claim-id field, and that is the point: the brief's citations are claim-level and
    resolve through `claim_evidence` to articles. An episode id in that list would resolve to
    nothing, so it is never possible to put one there.
    """

    event_id: uuid.UUID
    similarity: float
    rationale: str
    limitations: tuple[str, ...]
    shared_causes: tuple[str, ...]
    regime_caveats: tuple[str, ...]
    #: Provenance JSON, deep-frozen on construction (:func:`freeze_json`).
    evidence_refs: ImmutableJSON | None
    onset: HistoricalOnset
    outcome: HistoricalOutcomeContext

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", freeze_json(self.evidence_refs))

    @property
    def episode_id(self) -> uuid.UUID:
        """The *episode*'s id. Not a claim id -- see the class docstring."""
        return self.onset.episode_id


@dataclass(frozen=True)
class EventAnalogyContext:
    """The historical parallels for one selected event, bounded and ordered."""

    event_id: uuid.UUID
    analogies: tuple[AnalogyContext, ...]


def build_analogy_context(
    rows: Iterable[AnalogyContext],
    events: Sequence[SelectedEvent],
    *,
    max_per_event: int = MAX_ANALOGIES_PER_EVENT,
) -> tuple[tuple[EventAnalogyContext, ...], tuple[DataQualityNote, ...]]:
    """Bound and order each event's parallels; declare the events history offers none for.

    Empty is a valid, expected answer -- the historical-episode spec calls "no reliable analogy"
    an explicit allowed result, not a failure -- so an event with no persisted analogy yields no
    section and an omission note, never a fabricated parallel.
    """
    by_event: dict[uuid.UUID, list[AnalogyContext]] = {}
    for row in rows:
        by_event.setdefault(row.event_id, []).append(row)

    contexts: list[EventAnalogyContext] = []
    for event in events:
        found = by_event.get(event.event_id, [])
        # Closest first; ties by episode id, which settles nothing and is chosen because it
        # cannot -- it is stable across runs, which insertion order is not.
        found.sort(key=lambda row: (-row.similarity, str(row.episode_id)))
        if found:
            contexts.append(
                EventAnalogyContext(event_id=event.event_id, analogies=tuple(found[:max_per_event]))
            )

    without = [event for event in events if event.event_id not in {c.event_id for c in contexts}]
    notes: list[DataQualityNote] = []
    if without:
        titles = ", ".join(f"{event.title!r}" for event in without)
        notes.append(
            DataQualityNote(
                DataQuality.NO_RELIABLE_ANALOGY,
                f"No reliable historical analogy is on record for {len(without)} selected "
                f"event(s): {titles}. Historical Parallels is omitted for them rather than "
                "drawing a parallel the corpus does not support.",
            )
        )
    return tuple(contexts), tuple(notes)


# --------------------------------------------------------------------------------------
# Forecasts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastScenarioRow:
    """One `forecast_scenarios` row, as loaded."""

    event_id: uuid.UUID
    scenario_set_id: uuid.UUID
    scenario_name: str
    probability: float
    risk_score: float
    severity: str
    horizon: str
    confidence: float | None
    #: Provenance JSON, deep-frozen on construction (:func:`freeze_json`).
    evidence_refs: ImmutableJSON | None
    created_at: datetime.datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", freeze_json(self.evidence_refs))


@dataclass(frozen=True)
class EventForecast:
    """One *complete, valid* MECE scenario set for one event. Never a blend of two sets."""

    event_id: uuid.UUID
    scenario_set_id: uuid.UUID
    scenarios: tuple[ForecastScenarioRow, ...]
    created_at: datetime.datetime

    @property
    def probability_total(self) -> float:
        return float(sum(Decimal(str(row.probability)) for row in self.scenarios))


#: Canonical scenario order (`db.models.core.SCENARIO_NAMES`), so the forecast table's rows
#: always come out in the same order rather than in probability order, which would reshuffle
#: the table every day and make it unreadable as a time series.
_SCENARIO_RANK = {name: index for index, name in enumerate(SCENARIO_NAMES)}


def _probability_total(rows: Sequence[ForecastScenarioRow]) -> Decimal:
    """Total exactly, in Decimal -- see :data:`PROBABILITY_SUM`."""
    return sum((Decimal(str(row.probability)) for row in rows), Decimal(0))


def _set_is_valid(rows: Sequence[ForecastScenarioRow]) -> tuple[bool, str]:
    names = [row.scenario_name for row in rows]
    present = set(names)
    if len(names) != len(present):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        return False, f"duplicate scenario_name(s): {', '.join(duplicates)}"
    missing = REQUIRED_SCENARIO_NAMES - present
    if missing:
        return False, f"incomplete set, missing: {', '.join(sorted(missing))}"
    unknown = present - REQUIRED_SCENARIO_NAMES
    if unknown:
        return False, f"unknown scenario_name(s): {', '.join(sorted(unknown))}"
    total = _probability_total(rows)
    if abs(total - PROBABILITY_SUM) > PROBABILITY_TOLERANCE:
        return False, f"probabilities sum to {total}, not 1.0 +/- {PROBABILITY_TOLERANCE}"
    return True, ""


def select_event_forecasts(
    rows: Iterable[ForecastScenarioRow], events: Sequence[SelectedEvent]
) -> tuple[tuple[EventForecast, ...], tuple[DataQualityNote, ...]]:
    """Take each event's *newest complete and valid* scenario set, and never mix two sets.

    Grouping by ``scenario_set_id`` first is the whole safeguard: a forecast run emits one
    MECE set, and picking "the four latest scenario rows for this event" would happily assemble
    a base case from today's run and a tail risk from last week's, producing probabilities that
    sum to 1.0 by accident while describing two different worlds.

    Sets are walked newest first -- newest ``created_at``, tie-broken by ``scenario_set_id`` so
    that two sets written in the same instant still order the same way on every run -- and the
    *first valid* one is chosen. An invalid or half-written newer set is skipped with a note,
    but it does **not** hide an older complete set: rejecting the newest run must not blind the
    brief to the last good forecast. The walk stops at the first valid set, so a complete set
    older still than the one chosen is never examined and never noted. If no set is valid, the
    event has no forecast and every invalid set it did have is on record as to why.
    """
    by_event: dict[uuid.UUID, dict[uuid.UUID, list[ForecastScenarioRow]]] = {}
    for row in rows:
        by_event.setdefault(row.event_id, {}).setdefault(row.scenario_set_id, []).append(row)

    forecasts: list[EventForecast] = []
    notes: list[DataQualityNote] = []

    for event in events:
        sets = by_event.get(event.event_id, {})
        if not sets:
            notes.append(
                DataQualityNote(
                    DataQuality.NO_FORECAST,
                    f"No forecast scenarios are on record for event {event.title!r}; the "
                    "Forecasts table omits it.",
                )
            )
            continue

        candidates = sorted(
            sets.items(),
            key=lambda item: (max(row.created_at for row in item[1]), item[0]),
            reverse=True,
        )
        chosen: tuple[uuid.UUID, list[ForecastScenarioRow]] | None = None
        for set_id, set_rows in candidates:
            valid, reason = _set_is_valid(set_rows)
            if valid:
                chosen = (set_id, set_rows)
                break
            notes.append(
                DataQualityNote(
                    DataQuality.FORECAST_SET_INVALID,
                    f"Forecast set ({set_id}) for event {event.title!r} is unusable -- "
                    f"{reason}; it was skipped in favour of the newest complete set (if any).",
                )
            )

        if chosen is None:
            continue

        chosen_set_id, chosen_rows = chosen
        ordered = sorted(chosen_rows, key=lambda row: _SCENARIO_RANK[row.scenario_name])
        forecasts.append(
            EventForecast(
                event_id=event.event_id,
                scenario_set_id=chosen_set_id,
                scenarios=tuple(ordered),
                created_at=max(row.created_at for row in chosen_rows),
            )
        )

    return tuple(forecasts), tuple(notes)


# --------------------------------------------------------------------------------------
# The aggregate, and its loader
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefContext:
    """Everything the composer may say about the selected events, and nothing else.

    Holds no ORM object and no session: every value in here was copied out of its row and
    frozen, so the composition stage cannot lazy-load, mutate, or accidentally write anything
    through the context it was handed.
    """

    evidence: tuple[EventEvidenceContext, ...] = ()
    analogies: tuple[EventAnalogyContext, ...] = ()
    forecasts: tuple[EventForecast, ...] = ()
    data_quality_notes: tuple[DataQualityNote, ...] = field(default=())

    def evidence_for(self, event_id: uuid.UUID) -> EventEvidenceContext | None:
        return next((item for item in self.evidence if item.event_id == event_id), None)

    def analogies_for(self, event_id: uuid.UUID) -> EventAnalogyContext | None:
        return next((item for item in self.analogies if item.event_id == event_id), None)

    def forecast_for(self, event_id: uuid.UUID) -> EventForecast | None:
        return next((item for item in self.forecasts if item.event_id == event_id), None)


@runtime_checkable
class BriefContextRepository(Protocol):
    """The persistence surface the context needs, and nothing more.

    Declared among the pure values so that :func:`build_brief_context` can depend on the
    *shape* of its loader without importing SQLAlchemy. Every read returns a
    :class:`BoundedRead`: the reducer sees at most the documented bound, and learns explicitly
    whether the scan's backstop clipped anything -- never left to infer it from a row count.
    """

    def evidence_for_events(self, event_ids: Sequence[uuid.UUID]) -> BoundedRead[EvidenceRow]: ...

    def analogies_for_events(
        self, event_ids: Sequence[uuid.UUID]
    ) -> BoundedRead[AnalogyContext]: ...

    def forecasts_for_events(
        self, event_ids: Sequence[uuid.UUID]
    ) -> BoundedRead[ForecastScenarioRow]: ...


def _scan_truncation_notes(
    evidence: BoundedRead[EvidenceRow],
    analogies: BoundedRead[AnalogyContext],
    forecasts: BoundedRead[ForecastScenarioRow],
) -> tuple[DataQualityNote, ...]:
    """Declare every global context scan that hit its backstop bound.

    Separate from the per-event/per-claim `EVIDENCE_TRUNCATED`: this is the whole-scan ``LIMIT``
    that guards against a runaway query, and a scan that reached it may have dropped rows the
    reducer never saw -- which, unreported, is indistinguishable from those rows not existing.
    """
    notes: list[DataQualityNote] = []
    if evidence.truncated:
        notes.append(
            DataQualityNote(
                DataQuality.EVIDENCE_SCAN_TRUNCATED,
                f"The evidence scan hit its {evidence.limit}-row bound; some supporting "
                "claim-links across the selected events were not read.",
            )
        )
    if analogies.truncated:
        notes.append(
            DataQualityNote(
                DataQuality.ANALOGY_SCAN_TRUNCATED,
                f"The historical-analogy scan hit its {analogies.limit}-row bound; some "
                "persisted analogies across the selected events were not read.",
            )
        )
    if forecasts.truncated:
        notes.append(
            DataQualityNote(
                DataQuality.FORECAST_SCAN_TRUNCATED,
                f"The forecast-scenario scan hit its {forecasts.limit}-row bound; some scenario "
                "sets across the selected events were not read, so a set shown as newest-complete "
                "may not be.",
            )
        )
    return tuple(notes)


def build_brief_context(
    repository: BriefContextRepository,
    events: Sequence[SelectedEvent],
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> BriefContext:
    """Load and reduce every context the selected events support.

    A quiet day short-circuits: with no selected events there is nothing to load evidence,
    parallels or forecasts *for*, and issuing three unbounded-looking `IN ()` queries to prove
    it would be three round trips to learn what the caller already knows.
    """
    if not events:
        return BriefContext()

    event_ids = [event.event_id for event in events]

    evidence_read = repository.evidence_for_events(event_ids)
    analogy_read = repository.analogies_for_events(event_ids)
    # Gate G is a read boundary, not merely a rendering preference. When it is closed the
    # ForecastScenario repository method is never called, so probabilities/provenance cannot
    # enter memory, a prompt, a deterministic section, or a persisted report by accident.
    forecast_read: BoundedRead[ForecastScenarioRow]
    if prediction_backed_outputs_enabled:
        forecast_read = repository.forecasts_for_events(event_ids)
    else:
        forecast_read = BoundedRead(rows=(), limit=0, truncated=False)

    evidence, evidence_notes = build_evidence_context(evidence_read.rows)
    analogies, analogy_notes = build_analogy_context(analogy_read.rows, events)
    if prediction_backed_outputs_enabled:
        forecasts, forecast_notes = select_event_forecasts(forecast_read.rows, events)
    else:
        forecasts, forecast_notes = (), ()

    return BriefContext(
        evidence=evidence,
        analogies=analogies,
        forecasts=forecasts,
        data_quality_notes=(
            *_scan_truncation_notes(evidence_read, analogy_read, forecast_read),
            *evidence_notes,
            *evidence_notes_for_events(events, evidence),
            *analogy_notes,
            *forecast_notes,
        ),
    )
