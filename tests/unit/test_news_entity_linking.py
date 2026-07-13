"""Deterministic news-mention entity linking (ADR 0005 stage 2).

Every test runs against a dict-backed fake session: no database, no network, no spaCy. The
mentions are built the way stage 1 emits them, so offsets, sentence windows, and assertion
status are the real ones.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BooleanClauseList

from db.models import EntityAlias, EntityProfile, EntityRedirect, EntityResolutionRun
from packages.config.settings import Settings, get_settings
from services.entities.news_linking import (
    ACCEPT_THRESHOLD,
    ADJUDICATE_THRESHOLD,
    LINKABLE_ALIAS_TYPES,
    NEWS_MENTION_TARGET_TYPE,
    REASON_ACCEPT_TIE,
    REASON_ACCEPTED,
    REASON_BELOW_ADJUDICATE,
    REASON_BRAND_GATE,
    REASON_NO_CANDIDATE,
    REDIRECT_AMBIGUOUS,
    REDIRECT_CYCLE,
    REDIRECT_MAX_DEPTH,
    REDIRECT_MISSING_TARGET,
    REDIRECT_TOO_DEEP,
    SIGNAL_WEIGHTS,
    WIKIDATA_ALIAS_PRIOR,
    AliasEvidence,
    ArticleLinkingContext,
    LinkBand,
    LinkCandidate,
    LinkSignal,
    NewsLinkingError,
    band_for_score,
    band_of_run,
    decide_band,
    link_mention,
    link_mentions,
    parse_link_explanation,
    round_score,
)
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import EntityLabel, EntityMention, SentenceContext
from services.provider_data.common import normalize_alias, normalize_name

ARTICLE = "article-1"
PUBLISHED = datetime.date(2024, 6, 1)
# A sentence with none of the ADR company-context cues in it, so the mention-context signal is
# silent unless a test deliberately turns it on.
NEUTRAL_SENTENCE = "Acme appeared in the piece today."
CUE_SENTENCE = "Acme reported revenue for the period."


class FakeSession:
    """The dict-backed session the identity tests use: add, find_one, all_of."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def add(self, obj: Any) -> None:
        self.items.append(obj)

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        for item in self.items:
            if isinstance(item, model) and all(
                getattr(item, key) == value for key, value in criteria.items()
            ):
                return item
        return None

    def all_of(self, model: type[Any]) -> list[Any]:
        return [item for item in self.items if isinstance(item, model)]


class SqlSession:
    """A session exposing only the SQLAlchemy surface, so the reads take the real code path.

    ``FakeSession`` is matched by its ``all_of`` hook, which short-circuits query building
    altogether — every assertion made through it is blind to whether the SQL the services emit is
    even well-formed. This one offers no such hook, so ``NewsLinkingRepository`` and the review
    queue fall through to ``select(...)`` / ``session.execute(...)`` exactly as they do against a
    live ``Session``. Statements are evaluated over in-memory rows and recorded, so a test can
    assert both the outcome and the shape and *count* of the queries that produced it.
    """

    def __init__(self, *rows: Any) -> None:
        self.rows: list[Any] = list(rows)
        self.statements: list[Any] = []

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    def flush(self) -> None:
        """A real Session is built with ``autoflush=False``; added rows are already visible here."""

    def execute(self, statement: Any) -> _SqlResult:
        self.statements.append(statement)
        model = statement.column_descriptions[0]["entity"]
        predicates = _flatten_predicates(statement.whereclause)
        return _SqlResult(
            [
                row
                for row in self.rows
                if isinstance(row, model)
                and all(_row_matches(row, predicate) for predicate in predicates)
            ]
        )


class _SqlResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _SqlResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


def _flatten_predicates(clause: Any) -> list[Any]:
    if clause is None:
        return []
    if isinstance(clause, BooleanClauseList):
        return [item for child in clause.clauses for item in _flatten_predicates(child)]
    return [clause]


def _row_matches(row: Any, predicate: Any) -> bool:
    """Evaluate the predicate shapes these services emit, and refuse any other.

    The refusal is the point: if a service ever grows a ``LIKE`` scan or an unbounded read, this
    raises here instead of quietly passing on rows a fake happened to hand back.
    """
    actual = getattr(row, predicate.left.key)
    operator = predicate.operator
    if operator is operators.in_op:
        return actual in predicate.right.value
    if operator is operators.is_:
        return actual is None
    expected = predicate.right.value
    if operator is operators.eq:
        return actual == expected
    if operator is operators.ge:
        return actual is not None and actual >= expected
    if operator is operators.le:
        return actual is not None and actual <= expected
    msg = f"unsupported predicate for a bounded read: {predicate}"
    raise AssertionError(msg)


def _profile(name: str, **kwargs: Any) -> EntityProfile:
    return EntityProfile(
        id=uuid.uuid4(),
        canonical_name=name,
        normalized_name=normalize_name(name),
        entity_type=kwargs.pop("entity_type", None),
        **kwargs,
    )


def _alias(
    profile: EntityProfile,
    alias: str,
    *,
    alias_type: str = "legal_name",
    source: str = "sec-edgar",
    valid_from: datetime.date | None = None,
    valid_to: datetime.date | None = None,
) -> EntityAlias:
    return EntityAlias(
        id=uuid.uuid4(),
        entity_id=profile.id,
        alias=alias,
        normalized_alias=normalize_alias(alias),
        alias_type=alias_type,
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _redirect(
    old: EntityProfile | uuid.UUID,
    new: EntityProfile | uuid.UUID,
    effective_date: datetime.date,
) -> EntityRedirect:
    return EntityRedirect(
        id=uuid.uuid4(),
        old_entity_id=old if isinstance(old, uuid.UUID) else old.id,
        new_entity_id=new if isinstance(new, uuid.UUID) else new.id,
        effective_date=effective_date,
    )


def _mention(
    surface: str = "Acme",
    *,
    sentence: str = NEUTRAL_SENTENCE,
    label: EntityLabel = EntityLabel.ORG,
    assertion_status: AssertionStatus = AssertionStatus.ASSERTED,
    start_char: int = 0,
    article_key: str = ARTICLE,
) -> EntityMention:
    context = SentenceContext(
        index=0,
        text=sentence,
        start_char=0,
        end_char=len(sentence),
        previous_text=None,
        next_text=None,
    )
    return EntityMention(
        article_key=article_key,
        text=surface,
        label=label,
        start_char=start_char,
        end_char=start_char + len(surface),
        sentence=context,
        assertion_status=assertion_status,
    )


def _context(**kwargs: Any) -> ArticleLinkingContext:
    kwargs.setdefault("published_on", PUBLISHED)
    return ArticleLinkingContext(article_key=ARTICLE, **kwargs)


def _seed(session: FakeSession, *rows: Any) -> None:
    for row in rows:
        session.add(row)


def _signal(candidate: LinkCandidate, signal: LinkSignal) -> Any:
    return next(item for item in candidate.signals if item.signal is signal)


# --- Weights and bands ---------------------------------------------------------
def test_signal_weights_are_the_adr_values_and_sum_to_one() -> None:
    assert SIGNAL_WEIGHTS == {
        LinkSignal.EXACT_TICKER: 0.25,
        LinkSignal.CO_MENTIONS: 0.25,
        LinkSignal.MENTION_CONTEXT: 0.20,
        LinkSignal.URL_DOMAIN: 0.15,
        LinkSignal.INDUSTRY_TERMS: 0.05,
        LinkSignal.SOURCE_CATEGORY: 0.05,
        LinkSignal.LOCATION_CUES: 0.05,
    }
    assert sum(SIGNAL_WEIGHTS.values()) == 1.0


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (1.0, LinkBand.ACCEPT),
        (0.8501, LinkBand.ACCEPT),
        (ACCEPT_THRESHOLD, LinkBand.ACCEPT),  # exactly 0.85 accepts
        (0.8499, LinkBand.ADJUDICATE),
        (ADJUDICATE_THRESHOLD, LinkBand.ADJUDICATE),  # exactly 0.50 adjudicates
        (0.4999, LinkBand.NIL),
        (0.0, LinkBand.NIL),
    ],
)
def test_confidence_bands_are_exact_at_the_thresholds(score: float, band: LinkBand) -> None:
    assert band_for_score(score) is band


# --- Candidate generation ------------------------------------------------------
@pytest.mark.parametrize("alias_type", sorted(LINKABLE_ALIAS_TYPES))
def test_every_adr_alias_type_generates_a_candidate(alias_type: str) -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme", alias_type=alias_type))

    result = link_mention(session, _mention("Acme"), _context())

    assert [candidate.entity_id for candidate in result.candidates] == [profile.id]
    assert result.candidates[0].evidence.alias_type == alias_type


def test_an_alias_type_outside_the_adr_vocabulary_never_generates_a_candidate() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme", alias_type="internal_code"))

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates == ()
    assert result.band is LinkBand.NIL
    assert result.reason == REASON_NO_CANDIDATE


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "expected"),
    [
        (None, None, True),
        (datetime.date(2024, 1, 1), None, True),
        (datetime.date(2024, 6, 1), None, True),  # valid_from is inclusive
        (datetime.date(2024, 6, 2), None, False),  # not yet in use on the publication date
        (None, datetime.date(2024, 6, 1), True),  # valid_to is inclusive
        (None, datetime.date(2024, 5, 31), False),  # retired before the article ran
        (datetime.date(2024, 1, 1), datetime.date(2024, 12, 31), True),
    ],
)
def test_alias_validity_is_evaluated_at_the_publication_date(
    valid_from: datetime.date | None, valid_to: datetime.date | None, expected: bool
) -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(
        session,
        profile,
        _alias(profile, "Acme", alias_type="former_name", valid_from=valid_from, valid_to=valid_to),
    )

    result = link_mention(session, _mention("Acme"), _context())

    assert bool(result.candidates) is expected


def test_an_undated_article_applies_no_alias_validity_filter() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(
        session,
        profile,
        _alias(profile, "Acme", alias_type="former_name", valid_to=datetime.date(2020, 1, 1)),
    )

    result = link_mention(session, _mention("Acme"), _context(published_on=None))

    assert [candidate.entity_id for candidate in result.candidates] == [profile.id]


def test_a_profile_without_aliases_falls_back_to_its_canonical_name() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile)

    result = link_mention(session, _mention("Acme Corporation"), _context())

    candidate = result.candidates[0]
    assert candidate.entity_id == profile.id
    assert candidate.evidence.alias_type == "canonical_name"
    assert candidate.evidence.source == "entity_profiles.normalized_name"


def test_the_canonical_fallback_never_runs_when_an_alias_matched() -> None:
    session = FakeSession()
    aliased = _profile("Acme Corporation")
    bare = _profile("Acme")
    _seed(session, aliased, bare, _alias(aliased, "Acme"))

    result = link_mention(session, _mention("Acme"), _context())

    assert [candidate.entity_id for candidate in result.candidates] == [aliased.id]


def test_a_mention_surface_with_no_normalized_form_yields_no_candidates() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("!!! ???"), _context())

    assert result.band is LinkBand.NIL
    assert result.candidates == ()
    assert result.confidence_score is None


def test_a_mention_from_another_article_is_refused() -> None:
    session = FakeSession()
    with pytest.raises(NewsLinkingError):
        link_mention(session, _mention(article_key="other"), _context())


# --- Individual signals and their exact weights --------------------------------
def _one_signal_case(
    signal: LinkSignal,
) -> tuple[EntityProfile, EntityMention, ArticleLinkingContext]:
    """A scenario in which exactly one signal fires, so its contribution is its whole weight.

    ``entity_type`` stays unset wherever a company type would also satisfy the mention-context
    label check, and the source-category case uses a GPE mention so the label check disagrees.
    """
    if signal is LinkSignal.EXACT_TICKER:
        profile = _profile("Acme Corporation", primary_ticker="ACME")
        return profile, _mention("Acme"), _context(text="Shares of ACME moved.")
    if signal is LinkSignal.CO_MENTIONS:
        profile = _profile("Acme Corporation")
        return (
            profile,
            _mention("Acme"),
            _context(co_mention_surfaces=("Acme Labs", "Acme Widgets"), text=NEUTRAL_SENTENCE),
        )
    if signal is LinkSignal.MENTION_CONTEXT:
        profile = _profile("Acme Corporation")
        return profile, _mention("Acme", sentence=CUE_SENTENCE), _context(text=CUE_SENTENCE)
    if signal is LinkSignal.URL_DOMAIN:
        profile = _profile("Acme Corporation", website="https://www.acme.example")
        return profile, _mention("Acme"), _context(url="https://acme.example/news/1")
    if signal is LinkSignal.INDUSTRY_TERMS:
        profile = _profile(
            "Acme Corporation",
            profile_metadata={"wikidata": {"industries": [{"label": "semiconductor"}]}},
        )
        return profile, _mention("Acme"), _context(industry_terms=("semiconductor",))
    if signal is LinkSignal.SOURCE_CATEGORY:
        profile = _profile("Acme Corporation", entity_type="company")
        return (
            profile,
            _mention("Acme", label=EntityLabel.GPE),
            _context(source_category="financial"),
        )
    profile = _profile("Acme Corporation", country="US")
    return profile, _mention("Acme"), _context(location_terms=("US",))


@pytest.mark.parametrize("signal", list(LinkSignal))
def test_each_signal_contributes_exactly_its_adr_weight(signal: LinkSignal) -> None:
    session = FakeSession()
    profile, mention, context = _one_signal_case(signal)
    _seed(session, profile, _alias(profile, "Acme"))
    if signal is LinkSignal.CO_MENTIONS:
        _seed(
            session,
            _alias(profile, "Acme Labs", alias_type="short_name"),
            _alias(profile, "Acme Widgets", alias_type="brand_product"),
        )

    candidate = link_mention(session, mention, context).candidates[0]

    fired = [item for item in candidate.signals if item.contribution > 0]
    assert [item.signal for item in fired] == [signal]
    assert _signal(candidate, signal).strength == 1.0
    assert _signal(candidate, signal).contribution == SIGNAL_WEIGHTS[signal]
    assert candidate.score == SIGNAL_WEIGHTS[signal]


def test_a_single_co_mention_is_half_strength_and_a_linked_entity_supports_the_candidate() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(
        session,
        profile,
        _alias(profile, "Acme"),
        _alias(profile, "Acme Labs", alias_type="short_name"),
    )

    single = link_mention(session, _mention("Acme"), _context(co_mention_surfaces=("Acme Labs",)))
    with_link = link_mention(
        session,
        _mention("Acme"),
        _context(co_mention_surfaces=("Acme Labs",), linked_entity_ids=(profile.id,)),
    )

    assert _signal(single.candidates[0], LinkSignal.CO_MENTIONS).strength == 0.5
    assert single.candidates[0].score == 0.125
    assert _signal(with_link.candidates[0], LinkSignal.CO_MENTIONS).strength == 1.0
    assert f"linked:{profile.id}" in _signal(with_link.candidates[0], LinkSignal.CO_MENTIONS).detail


def test_a_co_mention_of_an_unrelated_surface_supports_nothing() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(co_mention_surfaces=("Globex",)))

    assert _signal(result.candidates[0], LinkSignal.CO_MENTIONS).strength == 0.0


def test_the_label_half_of_the_mention_context_signal_scores_on_its_own() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", entity_type="company")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context())

    candidate = result.candidates[0]
    assert _signal(candidate, LinkSignal.MENTION_CONTEXT).strength == 0.5
    assert candidate.score == 0.10


@pytest.mark.parametrize(
    ("text", "fires"),
    [
        ("Shares of ACME rose.", True),
        ("Traders piled into $ACME today.", True),
        ("Traders piled into $acme today.", True),  # the dollar form is case-insensitive
        ("ACME's outlook improved.", True),
        ("(ACME) closed higher.", True),
        ("The ACMES index rose.", False),  # no substring match
        ("Buy XACME now.", False),
        ("The acme of achievement.", False),  # the bare form is case-sensitive
    ],
)
def test_exact_ticker_matching_respects_token_boundaries(text: str, fires: bool) -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", primary_ticker="ACME")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(text=text))

    assert bool(_signal(result.candidates[0], LinkSignal.EXACT_TICKER).strength) is fires


def test_a_ticker_alias_row_is_matched_in_text_like_the_primary_ticker() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme"), _alias(profile, "ACM", alias_type="ticker"))

    result = link_mention(session, _mention("Acme"), _context(text="ACM traded flat."))

    assert _signal(result.candidates[0], LinkSignal.EXACT_TICKER).detail == ("ACM",)


def test_an_edgar_filing_url_matches_the_candidates_cik() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", primary_cik="0000320193")
    other = _profile("Globex Corporation", primary_cik="0000111111")
    _seed(session, profile, other, _alias(profile, "Acme"), _alias(other, "Acme"))

    result = link_mention(
        session,
        _mention("Acme"),
        _context(url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000069.htm"),
    )

    matched = {candidate.entity_id: candidate for candidate in result.candidates}
    assert _signal(matched[profile.id], LinkSignal.URL_DOMAIN).strength == 1.0
    assert _signal(matched[other.id], LinkSignal.URL_DOMAIN).strength == 0.0


def test_the_url_signal_ignores_an_unrelated_domain() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", website="https://acme.example")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(url="https://news.example/story"))

    assert _signal(result.candidates[0], LinkSignal.URL_DOMAIN).strength == 0.0


def test_the_source_category_signal_needs_a_financial_source_and_a_company() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", entity_type="company")
    _seed(session, profile, _alias(profile, "Acme"))

    general = link_mention(
        session, _mention("Acme", label=EntityLabel.GPE), _context(source_category="general")
    )
    financial = link_mention(
        session, _mention("Acme", label=EntityLabel.GPE), _context(source_category="financial")
    )

    assert _signal(general.candidates[0], LinkSignal.SOURCE_CATEGORY).strength == 0.0
    assert _signal(financial.candidates[0], LinkSignal.SOURCE_CATEGORY).strength == 1.0


def test_industry_terms_also_match_the_article_text_directly() -> None:
    session = FakeSession()
    profile = _profile(
        "Acme Corporation",
        profile_metadata={"wikidata": {"industries": [{"label": "semiconductor"}]}},
    )
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(text="A semiconductor supplier."))

    assert _signal(result.candidates[0], LinkSignal.INDUSTRY_TERMS).detail == ("semiconductor",)


# --- Score, prior, and bands end to end ----------------------------------------
def _all_signals_context(**overrides: Any) -> ArticleLinkingContext:
    text = "ACME leads the semiconductor market. " + CUE_SENTENCE
    defaults: dict[str, Any] = {
        "text": text,
        "url": "https://acme.example/news/1",
        "source_category": "financial",
        "co_mention_surfaces": ("Acme Labs", "Acme Widgets"),
        "industry_terms": ("semiconductor",),
        "location_terms": ("US",),
    }
    defaults.update(overrides)
    return _context(**defaults)


def _all_signals_profile(session: FakeSession, **overrides: Any) -> EntityProfile:
    fields: dict[str, Any] = {
        "entity_type": "company",
        "country": "US",
        "primary_ticker": "ACME",
        "website": "https://acme.example",
        "profile_metadata": {"wikidata": {"industries": [{"label": "semiconductor"}]}},
    }
    fields.update(overrides)
    profile = _profile("Acme Corporation", **fields)
    _seed(
        session,
        profile,
        _alias(profile, "Acme Labs", alias_type="short_name"),
        _alias(profile, "Acme Widgets", alias_type="brand_product"),
    )
    return profile


def test_all_seven_signals_score_one_and_accept() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    assert result.candidates[0].score == 1.0
    assert result.band is LinkBand.ACCEPT
    assert result.matched_entity_id == profile.id
    assert result.should_attach
    assert result.reason == REASON_ACCEPTED


def test_the_four_strongest_signals_land_exactly_on_the_accept_threshold() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session, country=None)
    _seed(session, _alias(profile, "Acme"))
    context = _all_signals_context(source_category="general", industry_terms=())

    result = link_mention(
        session,
        _mention("Acme", sentence=CUE_SENTENCE),
        _context(**_without_industry_text(context)),
    )

    assert result.candidates[0].score == 0.85
    assert result.band is LinkBand.ACCEPT


def _without_industry_text(context: ArticleLinkingContext) -> dict[str, Any]:
    """The all-signals article without the industry term, so only the top four signals fire."""
    return {
        "text": "ACME leads the market. " + CUE_SENTENCE,
        "url": context.url,
        "source_category": "general",
        "co_mention_surfaces": context.co_mention_surfaces,
    }


def test_two_signals_land_exactly_on_the_adjudicate_threshold() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", primary_ticker="ACME")
    _seed(
        session,
        profile,
        _alias(profile, "Acme"),
        _alias(profile, "Acme Labs", alias_type="short_name"),
        _alias(profile, "Acme Widgets", alias_type="brand_product"),
    )
    context = _context(text="ACME moved today.", co_mention_surfaces=("Acme Labs", "Acme Widgets"))

    result = link_mention(session, _mention("Acme"), context)

    assert result.candidates[0].score == 0.50
    assert result.band is LinkBand.ADJUDICATE
    assert result.matched_entity_id is None
    assert not result.should_attach


def test_a_score_below_the_adjudicate_threshold_is_nil() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", website="https://acme.example")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(
        session,
        _mention("Acme", sentence=CUE_SENTENCE),
        _context(url="https://acme.example/news/1", text=CUE_SENTENCE),
    )

    assert result.candidates[0].score == 0.35  # 0.20 mention context + 0.15 URL
    assert result.band is LinkBand.NIL
    assert result.reason == REASON_BELOW_ADJUDICATE
    assert result.confidence_score == 0.35  # still recorded, for calibration


def test_a_bare_alias_match_with_no_signal_scores_zero() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates[0].score == 0.0
    assert result.band is LinkBand.NIL


def test_wikidata_sourced_evidence_carries_the_prior_multiplier() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme", source="wikidata"))

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    candidate = result.candidates[0]
    assert candidate.prior_multiplier == WIKIDATA_ALIAS_PRIOR
    assert candidate.signal_score == 1.0
    assert candidate.score == 0.7
    assert result.band is LinkBand.ADJUDICATE  # the discount pulls a perfect signal set off accept


def test_the_strongest_evidence_for_one_entity_wins_and_the_rest_is_kept() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(
        session,
        _alias(profile, "Acme", alias_type="colloquial", source="wikidata"),
        _alias(profile, "Acme", alias_type="legal_name", source="sec-edgar"),
        _alias(profile, "Acme", alias_type="short_name", source="gleif"),
    )

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    candidate = result.candidates[0]
    assert (candidate.evidence.alias_type, candidate.evidence.source) == ("legal_name", "sec-edgar")
    assert candidate.prior_multiplier == 1.0
    assert candidate.score == 1.0
    assert [(item.alias_type, item.source) for item in candidate.alternate_evidence] == [
        ("short_name", "gleif"),
        ("colloquial", "wikidata"),
    ]


def test_a_full_prior_beats_an_authoritative_wikidata_alias_type() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(
        session,
        profile,
        _alias(profile, "Acme", alias_type="legal_name", source="wikidata"),
        _alias(profile, "Acme", alias_type="former_name", source="gleif"),
    )

    candidate = link_mention(session, _mention("Acme"), _context()).candidates[0]

    assert (candidate.evidence.alias_type, candidate.evidence.source) == ("former_name", "gleif")
    assert candidate.prior_multiplier == 1.0


# --- Brand/product gate --------------------------------------------------------
def test_a_brand_alias_resolves_to_the_entity_that_owns_it_not_its_parent() -> None:
    session = FakeSession()
    parent = _profile("Acme Holdings", entity_type="company")
    subsidiary = _profile("Acme Widgets Inc", entity_type="company")
    _seed(
        session,
        parent,
        subsidiary,
        _alias(parent, "Acme Holdings"),
        _alias(subsidiary, "Widgetron", alias_type="brand_product"),
    )

    result = link_mention(session, _mention("Widgetron", sentence=CUE_SENTENCE), _context())

    assert [candidate.entity_id for candidate in result.candidates] == [subsidiary.id]
    assert result.candidates[0].evidence.alias_type == "brand_product"


def test_a_brand_alias_with_supporting_signals_accepts() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme", alias_type="brand_product"))

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    assert result.candidates[0].has_supporting_signal
    assert result.band is LinkBand.ACCEPT
    assert result.matched_entity_id == profile.id


def test_a_brand_alias_never_auto_accepts_without_an_independent_signal() -> None:
    """The gate itself: an accept-scoring brand candidate with no signal is sent to stage 3.

    Under the ADR's initial weights the linker cannot build this candidate — 0.85 needs four
    signals — so the gate is driven directly, which is exactly the invariant that has to survive
    any later recalibration.
    """
    brand = _brand_candidate(score=0.9, supported=False)
    supported = _brand_candidate(score=0.9, supported=True)

    assert decide_band([brand]) == (LinkBand.ADJUDICATE, REASON_BRAND_GATE)
    assert decide_band([supported]) == (LinkBand.ACCEPT, REASON_ACCEPTED)


def _brand_candidate(*, score: float, supported: bool) -> LinkCandidate:
    from services.entities.news_linking import SignalEvidence

    entity_id = uuid.uuid4()
    signals = tuple(
        SignalEvidence(
            signal=signal,
            weight=weight,
            strength=1.0 if supported and signal is LinkSignal.CO_MENTIONS else 0.0,
            contribution=weight if supported and signal is LinkSignal.CO_MENTIONS else 0.0,
        )
        for signal, weight in SIGNAL_WEIGHTS.items()
    )
    return LinkCandidate(
        entity_id=entity_id,
        canonical_name="Acme Corporation",
        entity_type="company",
        country=None,
        ticker=None,
        industries=(),
        evidence=AliasEvidence(
            entity_id=entity_id,
            alias="Widgetron",
            normalized_alias="widgetron",
            alias_type="brand_product",
            source="wikidata",
            prior_multiplier=WIKIDATA_ALIAS_PRIOR,
        ),
        alternate_evidence=(),
        redirected_from=(),
        redirect_paths=(),
        signals=signals,
        prior_multiplier=WIKIDATA_ALIAS_PRIOR,
        signal_score=score,
        score=score,
        band=band_for_score(score),
    )


# --- Ambiguity and ordering ----------------------------------------------------
def test_an_accept_threshold_tie_is_adjudicated_rather_than_attached() -> None:
    session = FakeSession()
    first = _all_signals_profile(session)
    second = _all_signals_profile(session)
    _seed(session, _alias(first, "Acme"), _alias(second, "Acme"))

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    assert [candidate.score for candidate in result.candidates] == [1.0, 1.0]
    assert result.band is LinkBand.ADJUDICATE
    assert result.reason == REASON_ACCEPT_TIE
    assert result.matched_entity_id is None
    assert not result.should_attach


def test_a_separable_top_candidate_still_accepts() -> None:
    session = FakeSession()
    winner = _all_signals_profile(session)
    runner_up = _profile("Beta Corporation", entity_type="company")
    _seed(session, runner_up, _alias(winner, "Acme"), _alias(runner_up, "Acme"))

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    assert result.band is LinkBand.ACCEPT
    assert result.matched_entity_id == winner.id
    assert result.candidates[0].score > result.candidates[1].score


def test_candidates_are_ordered_by_score_then_name_and_capped() -> None:
    session = FakeSession()
    profiles = [
        _profile(name) for name in ("Zeta Corporation", "Alpha Corporation", "Mid Corporation")
    ]
    _seed(session, *profiles)
    for profile in profiles:
        _seed(session, _alias(profile, "Acme"))
    _seed(session, _alias(profiles[2], "Acme Labs", alias_type="short_name"))

    context = _context(co_mention_surfaces=("Acme Labs",), max_candidates=2)
    result = link_mention(session, _mention("Acme"), context)

    assert [candidate.canonical_name for candidate in result.candidates] == [
        "Mid Corporation",  # the only one with a co-mention behind it
        "Alpha Corporation",  # then the tied pair, alphabetically
    ]
    assert len(result.candidates) == 2


def test_the_candidate_cap_defaults_to_the_configured_bound() -> None:
    assert get_settings().entity_link_max_candidates == 8
    with pytest.raises(ValueError, match="at least one candidate"):
        Settings(entity_link_max_candidates=0)
    with pytest.raises(ValueError, match="at least one candidate"):
        ArticleLinkingContext(article_key=ARTICLE, max_candidates=0)


# --- Redirects -----------------------------------------------------------------
def test_a_redirect_is_followed_at_the_publication_date() -> None:
    session = FakeSession()
    old = _profile("Old Acme")
    new = _profile("New Acme")
    _seed(session, old, new, _alias(old, "Acme"), _redirect(old, new, datetime.date(2024, 1, 1)))

    result = link_mention(session, _mention("Acme"), _context())

    candidate = result.candidates[0]
    assert candidate.entity_id == new.id
    assert candidate.canonical_name == "New Acme"
    assert candidate.redirected_from == (old.id,)
    assert candidate.redirect_paths == ((old.id, new.id),)
    assert candidate.evidence.entity_id == old.id  # the original candidate is kept as evidence


def test_a_redirect_that_had_not_taken_effect_yet_is_not_followed() -> None:
    session = FakeSession()
    old = _profile("Old Acme")
    new = _profile("New Acme")
    _seed(session, old, new, _alias(old, "Acme"), _redirect(old, new, datetime.date(2025, 1, 1)))

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates[0].entity_id == old.id
    assert result.candidates[0].redirect_paths == ()


def test_a_redirect_chain_is_followed_to_its_live_endpoint() -> None:
    session = FakeSession()
    old, middle, new = _profile("Old"), _profile("Middle"), _profile("New")
    _seed(
        session,
        old,
        middle,
        new,
        _alias(old, "Acme"),
        _redirect(old, middle, datetime.date(2022, 1, 1)),
        _redirect(middle, new, datetime.date(2023, 1, 1)),
    )

    candidate = link_mention(session, _mention("Acme"), _context()).candidates[0]

    assert candidate.entity_id == new.id
    assert candidate.redirect_paths == ((old.id, middle.id, new.id),)


def test_a_redirect_cycle_is_flagged_and_never_attached() -> None:
    session = FakeSession()
    first, second = _profile("First"), _profile("Second")
    _seed(
        session,
        first,
        second,
        _alias(first, "Acme"),
        _redirect(first, second, datetime.date(2022, 1, 1)),
        _redirect(second, first, datetime.date(2023, 1, 1)),
    )

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates == ()
    assert result.band is LinkBand.NIL
    assert [item.reason for item in result.rejected] == [REDIRECT_CYCLE]
    assert result.rejected[0].entity_id == first.id


def test_a_redirect_to_a_missing_entity_is_flagged_and_never_attached() -> None:
    session = FakeSession()
    old = _profile("Old Acme")
    _seed(
        session, old, _alias(old, "Acme"), _redirect(old, uuid.uuid4(), datetime.date(2022, 1, 1))
    )

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates == ()
    assert [item.reason for item in result.rejected] == [REDIRECT_MISSING_TARGET]


def test_two_old_entities_redirecting_to_one_current_entity_merge_into_one_candidate() -> None:
    session = FakeSession()
    first, second, new = _profile("First Acme"), _profile("Second Acme"), _profile("Acme Group")
    _seed(
        session,
        first,
        second,
        new,
        _alias(first, "Acme", alias_type="former_name", source="gleif"),
        _alias(second, "Acme", alias_type="legal_name", source="sec-edgar"),
        _redirect(first, new, datetime.date(2022, 1, 1)),
        _redirect(second, new, datetime.date(2023, 1, 1)),
    )

    result = link_mention(session, _mention("Acme"), _context())

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.entity_id == new.id
    assert candidate.redirected_from == tuple(sorted((first.id, second.id), key=str))
    assert candidate.evidence.alias_type == "legal_name"  # the strongest of the merged evidence
    assert len(candidate.alternate_evidence) == 1
    assert candidate.redirect_paths == tuple(
        sorted(((first.id, new.id), (second.id, new.id)), key=lambda path: tuple(map(str, path)))
    )


def test_the_latest_redirect_in_effect_wins() -> None:
    session = FakeSession()
    old, first, second = _profile("Old"), _profile("First"), _profile("Second")
    _seed(
        session,
        old,
        first,
        second,
        _alias(old, "Acme"),
        _redirect(old, first, datetime.date(2022, 1, 1)),
        _redirect(old, second, datetime.date(2023, 1, 1)),
    )

    candidate = link_mention(session, _mention("Acme"), _context()).candidates[0]

    assert candidate.entity_id == second.id


# --- Redirect forks ------------------------------------------------------------
def _forked(session: FakeSession, name: str = "Old Acme") -> EntityProfile:
    """An entity two competing redirects lead out of on one effective date.

    Both rows are legal under the ``(old, new, effective_date)`` unique key, and nothing in the
    data says which merger happened -- which is why ``resolve_entity_redirect`` refuses the pair
    outright, and why the linker must not quietly pick a side of it either.
    """
    old, left, right = _profile(name), _profile(f"Left {name}"), _profile(f"Right {name}")
    _seed(
        session,
        old,
        left,
        right,
        _redirect(old, left, datetime.date(2024, 1, 1)),
        _redirect(old, right, datetime.date(2024, 1, 1)),
    )
    return old


def test_competing_redirects_on_one_effective_date_are_never_attached() -> None:
    session = FakeSession()
    old = _forked(session)
    _seed(session, _alias(old, "Acme"))

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates == ()
    assert result.band is LinkBand.NIL
    assert result.matched_entity_id is None
    assert not result.should_attach
    assert [item.reason for item in result.rejected] == [REDIRECT_AMBIGUOUS]
    # The originating candidate is what is flagged, and neither branch of the fork is walked into.
    assert result.rejected[0].entity_id == old.id
    assert result.rejected[0].chain == (old.id,)


def test_a_fork_further_down_a_chain_is_flagged_and_never_attached() -> None:
    session = FakeSession()
    start = _profile("Start Acme")
    middle = _forked(session, "Middle Acme")
    _seed(
        session, start, _alias(start, "Acme"), _redirect(start, middle, datetime.date(2022, 1, 1))
    )

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates == ()
    assert result.band is LinkBand.NIL
    assert [item.reason for item in result.rejected] == [REDIRECT_AMBIGUOUS]
    # The chain is the walk up to the fork: the candidate itself, then the node that forks.
    assert result.rejected[0].chain == (start.id, middle.id)


def test_a_later_unique_redirect_settles_an_older_fork() -> None:
    session = FakeSession()
    old, left, right = _profile("Old Acme"), _profile("Left Acme"), _profile("Right Acme")
    _seed(
        session,
        old,
        left,
        right,
        _alias(old, "Acme"),
        _redirect(old, left, datetime.date(2022, 1, 1)),
        _redirect(old, right, datetime.date(2022, 1, 1)),
        _redirect(old, left, datetime.date(2023, 1, 1)),
    )

    result = link_mention(session, _mention("Acme"), _context())

    # Only the latest effective date in force is read, and on that date one target is named; the
    # superseded fork is history, not an ambiguity.
    assert result.rejected == ()
    assert result.candidates[0].entity_id == left.id
    assert result.candidates[0].redirect_paths == ((old.id, left.id),)


@pytest.mark.parametrize(
    ("published_on", "forked"),
    [
        (datetime.date(2023, 12, 31), False),  # neither competing row is in force yet
        (datetime.date(2024, 1, 1), True),  # the effective date is inclusive
        (PUBLISHED, True),
        (None, True),  # an undated article filters no redirect out, so the fork is reachable
    ],
)
def test_a_fork_is_ambiguous_only_once_its_rows_are_in_force(
    published_on: datetime.date | None, forked: bool
) -> None:
    session = FakeSession()
    old = _forked(session)
    _seed(session, _alias(old, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(published_on=published_on))

    if forked:
        assert result.candidates == ()
        assert [item.reason for item in result.rejected] == [REDIRECT_AMBIGUOUS]
    else:
        # Before the fork takes effect the mention resolves to the entity that was live then.
        assert [candidate.entity_id for candidate in result.candidates] == [old.id]
        assert result.rejected == ()


def test_a_forked_candidate_does_not_hold_back_an_independent_valid_one() -> None:
    session = FakeSession()
    valid = _all_signals_profile(session)
    old = _forked(session)
    _seed(session, _alias(valid, "Acme"), _alias(old, "Acme"))

    result = link_mention(session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context())

    # The fork takes down its own originating candidate and nothing else: the valid candidate is
    # scored and accepted exactly as it is when the forked alias row is not in the store at all.
    assert [candidate.entity_id for candidate in result.candidates] == [valid.id]
    assert result.candidates[0].score == 1.0
    assert result.band is LinkBand.ACCEPT
    assert result.matched_entity_id == valid.id
    assert [(item.reason, item.entity_id) for item in result.rejected] == [
        (REDIRECT_AMBIGUOUS, old.id)
    ]


def test_a_fork_alongside_a_convergence_leaves_the_merged_candidate_intact() -> None:
    session = FakeSession()
    first, second, new = _profile("First Acme"), _profile("Second Acme"), _profile("Acme Group")
    old = _forked(session)
    _seed(
        session,
        first,
        second,
        new,
        _alias(old, "Acme"),
        _alias(first, "Acme", alias_type="former_name", source="gleif"),
        _alias(second, "Acme", alias_type="legal_name", source="sec-edgar"),
        _redirect(first, new, datetime.date(2022, 1, 1)),
        _redirect(second, new, datetime.date(2023, 1, 1)),
    )

    result = link_mention(session, _mention("Acme"), _context())

    # Two unambiguous origins still merge onto the one entity they both redirect to.
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.entity_id == new.id
    assert candidate.redirected_from == tuple(sorted((first.id, second.id), key=str))
    assert candidate.redirect_paths == tuple(
        sorted(((first.id, new.id), (second.id, new.id)), key=lambda path: tuple(map(str, path)))
    )
    assert [item.entity_id for item in result.rejected] == [old.id]


def test_a_mention_whose_every_candidate_is_invalid_is_nil_rather_than_an_error() -> None:
    session = FakeSession()
    old = _forked(session)
    looping, target = _profile("Looping Acme"), _profile("Target Acme")
    _seed(
        session,
        looping,
        target,
        _alias(old, "Acme"),
        _alias(looping, "Acme"),
        _redirect(looping, target, datetime.date(2022, 1, 1)),
        _redirect(target, looping, datetime.date(2023, 1, 1)),
    )

    result = link_mention(session, _mention("Acme"), _context())

    # Nothing is raised, so one corrupt candidate never costs the article the rest of its batch.
    assert result.band is LinkBand.NIL
    assert result.reason == REASON_NO_CANDIDATE
    assert result.matched_entity_id is None
    assert result.confidence_score is None
    # Both pathologies stay on the result for audit, in a stable (reason, entity id) order.
    assert [(item.reason, item.entity_id) for item in result.rejected] == sorted(
        [(REDIRECT_AMBIGUOUS, old.id), (REDIRECT_CYCLE, looping.id)],
        key=lambda item: (item[0], str(item[1])),
    )
    assert result.rejected == link_mention(session, _mention("Acme"), _context()).rejected


def test_a_fork_fails_closed_over_the_sqlalchemy_path_with_a_bounded_read_count() -> None:
    """The fork is settled in the rows the bulk read already returned -- never a query per fork."""

    def statements_for(forks: int) -> tuple[Any, list[Any]]:
        rows: list[Any] = []
        for index in range(forks):
            old = _profile(f"Old Acme {index:02d}")
            left, right = _profile(f"Left {index:02d}"), _profile(f"Right {index:02d}")
            rows += [
                old,
                left,
                right,
                _alias(old, "Acme"),
                _redirect(old, left, datetime.date(2024, 1, 1)),
                _redirect(old, right, datetime.date(2024, 1, 1)),
            ]
        session = SqlSession(*rows)
        return link_mention(session, _mention("Acme"), _context()), session.statements

    one, one_statements = statements_for(1)
    many, many_statements = statements_for(6)

    assert one.candidates == () and many.candidates == ()
    assert [item.reason for item in many.rejected] == [REDIRECT_AMBIGUOUS] * 6
    # The redirect read count is a property of the chain depth, never of the candidate count.
    assert len(one_statements) == len(many_statements)


# --- Assertion status ----------------------------------------------------------
@pytest.mark.parametrize(
    "status", [AssertionStatus.ASSERTED, AssertionStatus.DENIED, AssertionStatus.SPECULATIVE]
)
def test_assertion_status_rides_along_without_changing_the_score(status: AssertionStatus) -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme"))

    result = link_mention(
        session,
        _mention("Acme", sentence=CUE_SENTENCE, assertion_status=status),
        _all_signals_context(),
    )

    assert result.assertion_status is status
    assert result.candidates[0].score == 1.0
    assert result.band is LinkBand.ACCEPT


# --- Persistence ---------------------------------------------------------------
def test_a_resolution_run_is_written_once_per_mention_and_updated_in_place() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme"))
    mention = _mention("Acme", sentence=CUE_SENTENCE, start_char=12)

    first = link_mention(session, mention, _context(), persist_run=True)
    second = link_mention(session, mention, _all_signals_context(), persist_run=True)

    runs = session.all_of(EntityResolutionRun)
    assert len(runs) == 1
    assert runs[0].run_key == first.run_key == second.run_key
    assert runs[0].target_type == NEWS_MENTION_TARGET_TYPE
    assert runs[0].target_id == f"{ARTICLE}#12-16"
    assert runs[0].input_names == ["Acme"]
    # The first pass had no supporting context and attached nothing; the second accepted.
    assert first.matched_entity_id is None
    assert runs[0].matched_entity_id == profile.id
    assert float(runs[0].confidence_score) == 1.0


def test_an_unresolved_mention_persists_its_score_and_explanation_but_no_entity() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", entity_type="company")
    _seed(session, profile, _alias(profile, "Acme"))

    link_mention(session, _mention("Acme"), _context(), persist_run=True)

    run = session.all_of(EntityResolutionRun)[0]
    fields = parse_link_explanation(run.explanation)
    assert run.matched_entity_id is None
    assert float(run.confidence_score) == 0.10
    assert fields["band"] == LinkBand.NIL.value
    assert fields["score"] == "0.1000"
    assert fields["alias"] == "legal_name/sec-edgar"
    assert "entity" not in fields


def test_nothing_is_persisted_unless_the_caller_asks() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    _seed(session, profile, _alias(profile, "Acme"))

    link_mention(session, _mention("Acme"), _context())

    assert session.all_of(EntityResolutionRun) == []


def test_linking_a_batch_keeps_document_order_and_one_run_per_mention() -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation")
    other = _profile("Globex Corporation")
    _seed(session, profile, other, _alias(profile, "Acme"), _alias(other, "Globex"))
    mentions = (_mention("Acme", start_char=0), _mention("Globex", start_char=40))

    results = link_mentions(session, mentions, _context(), persist_run=True)

    assert [result.surface for result in results] == ["Acme", "Globex"]
    assert len(session.all_of(EntityResolutionRun)) == 2
    assert results[0].as_dict()["band"] == LinkBand.NIL.value  # JSON-serializable for stage 3


# --- Redirect depth ------------------------------------------------------------
def test_a_redirect_chain_past_the_maximum_depth_is_flagged_and_never_attached() -> None:
    session = FakeSession()
    profiles = [_profile(f"Acme {index}") for index in range(REDIRECT_MAX_DEPTH + 2)]
    _seed(session, *profiles, _alias(profiles[0], "Acme"))
    for index in range(len(profiles) - 1):
        _seed(session, _redirect(profiles[index], profiles[index + 1], datetime.date(2020, 1, 1)))

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates == ()
    assert result.band is LinkBand.NIL
    assert [item.reason for item in result.rejected] == [REDIRECT_TOO_DEEP]


def test_a_chain_at_the_maximum_depth_still_resolves() -> None:
    session = FakeSession()
    profiles = [_profile(f"Acme {index}") for index in range(REDIRECT_MAX_DEPTH + 1)]
    _seed(session, *profiles, _alias(profiles[0], "Acme"))
    for index in range(len(profiles) - 1):
        _seed(session, _redirect(profiles[index], profiles[index + 1], datetime.date(2020, 1, 1)))

    result = link_mention(session, _mention("Acme"), _context())

    assert result.candidates[0].entity_id == profiles[-1].id
    assert result.rejected == ()


# --- Rounding and clamping -----------------------------------------------------
@pytest.mark.parametrize(
    ("value", "rounded"),
    [
        (0.24499999999999997, 0.245),  # what 0.35 * 0.7 actually is in binary floating point
        (0.00005, 0.0001),  # half rounds up, never to even
        (0.00004, 0.0),
        (1.0, 1.0),
    ],
)
def test_scores_round_half_up_to_four_places(value: float, rounded: float) -> None:
    assert round_score(value) == rounded


def test_the_wikidata_discount_of_an_awkward_score_is_rounded_deterministically() -> None:
    """0.35 * 0.7 is 0.24499999999999997 in floating point; the stored score is 0.245."""
    session = FakeSession()
    profile = _profile("Acme Corporation", website="https://acme.example")
    _seed(session, profile, _alias(profile, "Acme", source="wikidata"))

    candidate = link_mention(
        session,
        _mention("Acme", sentence=CUE_SENTENCE),
        _context(url="https://acme.example/news/1", text=CUE_SENTENCE),
    ).candidates[0]

    assert candidate.signal_score == 0.35  # 0.20 mention context + 0.15 URL
    assert candidate.score == 0.245
    assert candidate.band is LinkBand.NIL


def test_a_perfect_signal_set_is_clamped_to_one() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme"))

    candidate = link_mention(
        session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context()
    ).candidates[0]

    assert candidate.signal_score == 1.0
    assert candidate.score == 1.0
    assert all(0.0 <= signal.strength <= 1.0 for signal in candidate.signals)
    assert sum(signal.weight for signal in candidate.signals) == 1.0


# --- The canonical-name fallback's prior ---------------------------------------
def test_a_wikidata_owned_canonical_name_carries_the_prior_through_the_fallback() -> None:
    session = FakeSession()
    profile = _profile(
        "Acme Corporation",
        entity_type="company",
        profile_metadata={"identity_sources": {"canonical_name": "wikidata"}},
    )
    _seed(session, profile)

    candidate = link_mention(session, _mention("Acme Corporation"), _context()).candidates[0]

    assert candidate.evidence.alias_type == "canonical_name"
    assert candidate.prior_multiplier == WIKIDATA_ALIAS_PRIOR
    assert candidate.signal_score == 0.10  # the label half of the mention-context signal
    assert candidate.score == 0.07


# --- URL and domain parsing ----------------------------------------------------
@pytest.mark.parametrize(
    ("website", "url", "fires"),
    [
        ("https://acme.example", "https://acme.example/news/1", True),
        ("https://www.acme.example", "https://acme.example/news/1", True),  # www is not identity
        (
            "https://acme.example",
            "https://IR.Acme.Example:8443/news",
            True,
        ),  # host is case/port free
        ("https://acme.example", "https://ir.acme.example/news", True),  # a subdomain is the entity
        ("https://acme.example", "https://notacme.example/news", False),  # not a suffix match
        ("https://acme.example", "https://acme.example.evil.test/x", False),  # nor a prefix one
        ("https://acme.example", "not a url at all", False),
        (None, "https://acme.example/news/1", False),
    ],
)
def test_the_domain_signal_parses_hostnames_rather_than_matching_strings(
    website: str | None, url: str, fires: bool
) -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", website=website)
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(url=url))

    assert bool(_signal(result.candidates[0], LinkSignal.URL_DOMAIN).strength) is fires


@pytest.mark.parametrize(
    ("cik", "url", "fires"),
    [
        (
            "0000320193",
            "https://www.sec.gov/Archives/edgar/data/320193/00003201932400069.htm",
            True,
        ),
        (
            "0000320193",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193",
            True,
        ),
        # The digits of an *unrelated* company's filing are not this company's CIK: only the
        # /data/<cik>/ segment and the cik query parameter name one.
        ("0000000010", "https://www.sec.gov/cgi-bin/browse-edgar?CIK=0000320193&type=10-K", False),
        (
            "0000320193",
            "https://www.sec.gov/Archives/edgar/data/111111/000032019324000069.htm",
            False,
        ),
        # ... and the CIK of a company is not a licence to match any other host.
        ("0000320193", "https://news.example/Archives/edgar/data/320193/x.htm", False),
    ],
)
def test_an_edgar_url_matches_only_the_cik_it_actually_names(
    cik: str, url: str, fires: bool
) -> None:
    session = FakeSession()
    profile = _profile("Acme Corporation", primary_cik=cik)
    _seed(session, profile, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context(url=url))

    assert bool(_signal(result.candidates[0], LinkSignal.URL_DOMAIN).strength) is fires


# --- The candidate cap ---------------------------------------------------------
def test_the_candidate_list_is_capped_at_the_configured_bound_by_default() -> None:
    session = FakeSession()
    limit = get_settings().entity_link_max_candidates
    profiles = [_profile(f"Acme {index:02d} Corporation") for index in range(limit + 4)]
    _seed(session, *profiles)
    for profile in profiles:
        _seed(session, _alias(profile, "Acme"))

    result = link_mention(session, _mention("Acme"), _context())

    assert len(result.candidates) == limit
    # The cap keeps the head of the deterministic order, so stage 3 always sees the same list.
    assert [candidate.canonical_name for candidate in result.candidates] == [
        profile.canonical_name for profile in profiles[:limit]
    ]


# --- The persisted explanation --------------------------------------------------
def test_a_delimiter_in_an_alias_source_cannot_corrupt_the_persisted_fields() -> None:
    """The explanation round trip has to be total: the queue reads ``reason`` back out of it."""
    session = FakeSession()
    profile = _profile("Acme Corporation", entity_type="company")
    _seed(session, profile, _alias(profile, "Acme", source="we|ird=source"))

    result = link_mention(session, _mention("Acme"), _context(), persist_run=True)
    fields = parse_link_explanation(session.all_of(EntityResolutionRun)[0].explanation)

    assert fields["band"] == LinkBand.NIL.value
    assert fields["score"] == "0.1000"
    assert fields["reason"] == REASON_BELOW_ADJUDICATE == result.reason
    assert fields["alias"] == "legal_name/we ird source"  # sanitized, and still one field


def test_the_explanation_never_ends_in_a_half_written_field() -> None:
    session = FakeSession()
    profile = _all_signals_profile(session)
    _seed(session, _alias(profile, "Acme"))

    link_mention(
        session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context(), persist_run=True
    )
    explanation = session.all_of(EntityResolutionRun)[0].explanation

    assert len(explanation) <= 480
    assert all("=" in part for part in explanation.split(" | "))
    assert parse_link_explanation(explanation)["band"] == LinkBand.ACCEPT.value


# --- Reading a persisted run back -----------------------------------------------
@pytest.mark.parametrize(
    ("matched", "score", "band"),
    [
        (True, 1.0, LinkBand.ACCEPT),
        (False, None, LinkBand.NIL),  # no candidate scored at all
        (False, 0.4999, LinkBand.NIL),
        (False, ADJUDICATE_THRESHOLD, LinkBand.ADJUDICATE),
        # An unmatched run at accept strength is one a gate held back, not an accept.
        (False, ACCEPT_THRESHOLD, LinkBand.ADJUDICATE),
        (False, 1.0, LinkBand.ADJUDICATE),
    ],
)
def test_a_persisted_runs_band_is_derivable_from_its_columns(
    matched: bool, score: float | None, band: LinkBand
) -> None:
    assert band_of_run(uuid.uuid4() if matched else None, score) is band


def test_a_gated_accept_persists_its_score_without_an_entity_and_reads_back_as_adjudicate() -> None:
    session = FakeSession()
    first = _all_signals_profile(session)
    second = _all_signals_profile(session)
    _seed(session, _alias(first, "Acme"), _alias(second, "Acme"))

    result = link_mention(
        session, _mention("Acme", sentence=CUE_SENTENCE), _all_signals_context(), persist_run=True
    )
    run = session.all_of(EntityResolutionRun)[0]

    assert result.band is LinkBand.ADJUDICATE and result.reason == REASON_ACCEPT_TIE
    assert run.matched_entity_id is None  # the tie attached nothing ...
    assert float(run.confidence_score) == 1.0  # ... but the score survives for calibration
    assert band_of_run(run.matched_entity_id, float(run.confidence_score)) is LinkBand.ADJUDICATE


# --- The SQLAlchemy path --------------------------------------------------------
def test_the_linker_resolves_the_same_way_through_the_sqlalchemy_session_path() -> None:
    old = _profile("Old Acme")
    new = _profile("New Acme", entity_type="company", primary_ticker="ACME")
    session = SqlSession(
        old, new, _alias(old, "Acme"), _redirect(old, new, datetime.date(2024, 1, 1))
    )

    result = link_mention(
        session,
        _mention("Acme", sentence=CUE_SENTENCE),
        _context(text="ACME reported revenue."),
        persist_run=True,
    )

    candidate = result.candidates[0]
    assert candidate.entity_id == new.id
    assert candidate.redirect_paths == ((old.id, new.id),)
    assert candidate.score == 0.45  # 0.25 ticker + 0.20 mention context
    # The run was written through the same ``select``-based path, not a fake's find_one hook.
    run = next(row for row in session.rows if isinstance(row, EntityResolutionRun))
    assert run.run_key == result.run_key


def test_every_identity_read_is_one_bounded_in_query_that_does_not_grow_with_the_candidates() -> (
    None
):
    """The linker must never issue a query per candidate; the alias index carries the whole set."""

    def statements_for(candidate_count: int) -> list[Any]:
        profiles = [_profile(f"Acme {index:02d} Corporation") for index in range(candidate_count)]
        session = SqlSession(*profiles, *(_alias(profile, "Acme") for profile in profiles))
        result = link_mention(session, _mention("Acme"), _context())
        assert len(result.candidates) == candidate_count
        return session.statements

    one = statements_for(1)
    many = statements_for(6)

    assert len(one) == len(many)  # the read count is a property of the linker, not of the data
    # Every read is an equality/IN predicate over an indexed column -- _row_matches refuses
    # anything else -- and the linker never selects a whole table.
    assert all(statement.whereclause is not None for statement in many)
