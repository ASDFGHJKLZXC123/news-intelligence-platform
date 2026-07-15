"""Section-budget enforcement: the deterministic word counter, the ranges, and the one retry.

DB-free. Budget behaviour is driven through the narrow :class:`FakeOrchestrator`, which returns
*real* validated :class:`ReportComposition` contracts, so the word counts under test are counted
off genuine contract output rather than a mock of it.
"""

from __future__ import annotations

from services.reports.composition import (
    EXECUTIVE_SUMMARY_BUDGET,
    HISTORICAL_PARALLELS_BUDGET,
    RISK_COMMENTARY_BUDGET,
    TOP_EVENT_BUDGET,
    DraftDegradationCode,
    compose_brief,
    count_words,
)
from services.reports.contracts import RiskRadar
from services.reports.material import SectionKind
from tests.unit._report_composition_fixtures import (
    CLAIM_ID,
    FakeOrchestrator,
    abstain,
    claim,
    multi_block,
    one_block,
    radar_entry,
    single_event_brief,
    words,
)

EXEC_OK = one_block(108)  # in the executive summary's 96..120 range, so exec takes one call


def _compose(fake: FakeOrchestrator, brief: tuple) -> object:
    inputs, context, material = brief
    return compose_brief(fake, inputs=inputs, context=context, material=material)


def _section(draft: object, kind: SectionKind) -> object:
    return draft.of_kind(kind)[0]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# The deterministic word counter
# --------------------------------------------------------------------------------------


def test_word_counter_splits_on_any_whitespace_and_empty_is_zero() -> None:
    assert count_words("") == 0
    assert count_words("   \t\n ") == 0
    assert count_words("one two three") == 3
    assert count_words("  a\tb\nc  ") == 3
    assert count_words(words(120)) == 120


def test_budget_ranges_are_the_documented_bands() -> None:
    # Executive summary: the spec hard cap of 120 overrides the +20% ceiling (would be 144).
    assert EXECUTIVE_SUMMARY_BUDGET.maximum == 120
    assert EXECUTIVE_SUMMARY_BUDGET.contains(96) and EXECUTIVE_SUMMARY_BUDGET.contains(120)
    assert not EXECUTIVE_SUMMARY_BUDGET.contains(95)
    assert not EXECUTIVE_SUMMARY_BUDGET.contains(121)

    assert TOP_EVENT_BUDGET.contains(120) and TOP_EVENT_BUDGET.contains(180)
    assert not TOP_EVENT_BUDGET.contains(119) and not TOP_EVENT_BUDGET.contains(181)

    assert RISK_COMMENTARY_BUDGET.contains(48) and RISK_COMMENTARY_BUDGET.contains(72)
    assert not RISK_COMMENTARY_BUDGET.contains(47) and not RISK_COMMENTARY_BUDGET.contains(73)

    assert HISTORICAL_PARALLELS_BUDGET.contains(80) and HISTORICAL_PARALLELS_BUDGET.contains(120)
    assert not HISTORICAL_PARALLELS_BUDGET.contains(79)
    assert not HISTORICAL_PARALLELS_BUDGET.contains(121)


# --------------------------------------------------------------------------------------
# Executive summary boundaries (hard cap 120)
# --------------------------------------------------------------------------------------


def test_executive_summary_accepts_the_lower_and_upper_bounds() -> None:
    for word_count in (96, 120):
        fake = FakeOrchestrator([one_block(word_count)])
        draft = _compose(fake, single_event_brief(claims=(claim(),)))
        section = _section(draft, SectionKind.EXECUTIVE_SUMMARY)
        assert section.is_generated
        assert section.budget.word_count == word_count
        assert section.budget.within_budget is True
        assert len(fake.requests_for("executive_summary")) == 1


def test_executive_summary_over_120_is_rejected_then_degrades() -> None:
    fake = FakeOrchestrator([one_block(121), one_block(121)])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    section = _section(draft, SectionKind.EXECUTIVE_SUMMARY)
    assert not section.is_generated
    assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
    # Exactly one retry: two exec invocations, never a third.
    assert len(fake.requests_for("executive_summary")) == 2
    assert len(section.attempts) == 2


def test_executive_summary_over_120_can_be_fixed_on_the_retry() -> None:
    fake = FakeOrchestrator([one_block(140), one_block(110)])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    section = _section(draft, SectionKind.EXECUTIVE_SUMMARY)
    assert section.is_generated
    assert section.budget.word_count == 110
    assert len(section.attempts) == 2
    assert section.attempts[0].within_budget is False
    assert section.attempts[1].within_budget is True


# --------------------------------------------------------------------------------------
# Event / risk / parallels boundaries
# --------------------------------------------------------------------------------------


def test_top_event_accepts_its_bounds() -> None:
    for word_count in (120, 180):
        fake = FakeOrchestrator([EXEC_OK, one_block(word_count)])
        draft = _compose(fake, single_event_brief(claims=(claim(),)))
        section = _section(draft, SectionKind.TOP_EVENT)
        assert section.is_generated
        assert section.budget.word_count == word_count
        assert len(fake.requests_for("top_event")) == 1


def test_risk_commentary_accepts_its_bounds() -> None:
    radar = RiskRadar(current=(radar_entry(60.0, "high"),), previous=(), moves=())
    for word_count in (48, 72):
        fake = FakeOrchestrator([EXEC_OK, one_block(150), one_block(word_count)])
        draft = _compose(fake, single_event_brief(claims=(claim(),), radar=radar))
        section = _section(draft, SectionKind.RISK_RADAR)
        assert section.is_generated
        assert section.budget.word_count == word_count


def test_historical_parallels_accepts_its_bounds() -> None:
    for word_count in (80, 120):
        fake = FakeOrchestrator([EXEC_OK, one_block(150), one_block(word_count)])
        draft = _compose(fake, single_event_brief(claims=(claim(),), analogies_present=True))
        section = _section(draft, SectionKind.HISTORICAL_PARALLELS)
        assert section.is_generated
        assert section.budget.word_count == word_count


# --------------------------------------------------------------------------------------
# The one retry, and what happens after it
# --------------------------------------------------------------------------------------


def test_a_first_violation_is_fixed_on_the_second_and_both_attempts_are_retained() -> None:
    fake = FakeOrchestrator([EXEC_OK, one_block(220), one_block(150)])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    section = _section(draft, SectionKind.TOP_EVENT)
    assert section.is_generated
    assert section.budget.word_count == 150
    assert [a.word_count for a in section.attempts] == [220, 150]
    assert len(fake.requests_for("top_event")) == 2


def test_a_second_violation_degrades_and_never_makes_a_third_call() -> None:
    fake = FakeOrchestrator([EXEC_OK, one_block(220), one_block(210)])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    section = _section(draft, SectionKind.TOP_EVENT)
    assert not section.is_generated
    assert section.blocks == ()  # generated prose is omitted, never trimmed to fit
    assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
    assert len(section.attempts) == 2  # both records kept
    assert section.budget.within_budget is False
    assert len(fake.requests_for("top_event")) == 2  # no third call


def test_an_abstention_counts_as_zero_words_and_is_retried_once_then_degrades() -> None:
    fake = FakeOrchestrator([EXEC_OK, abstain(), abstain()])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    section = _section(draft, SectionKind.TOP_EVENT)
    assert not section.is_generated
    assert [a.word_count for a in section.attempts] == [0, 0]
    assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
    assert len(fake.requests_for("top_event")) == 2


# --------------------------------------------------------------------------------------
# Combined multi-block counting and retention
# --------------------------------------------------------------------------------------


def test_the_budget_counts_the_combined_text_of_all_blocks_and_keeps_them() -> None:
    fake = FakeOrchestrator([EXEC_OK, multi_block([60, 70])])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    section = _section(draft, SectionKind.TOP_EVENT)
    assert section.is_generated
    assert len(section.blocks) == 2  # block boundaries retained, not concatenated away
    assert section.budget.word_count == 130  # 60 + 70, combined
    assert all(block.claim_ids == (CLAIM_ID,) for block in section.blocks)


# --------------------------------------------------------------------------------------
# Missing claims: no call at all
# --------------------------------------------------------------------------------------


def test_a_section_with_no_citable_claim_makes_no_call_and_degrades() -> None:
    fake = FakeOrchestrator([one_block(150)])
    draft = _compose(fake, single_event_brief(claims=()))
    section = _section(draft, SectionKind.TOP_EVENT)
    assert not section.is_generated
    assert section.degradations[0].code is DraftDegradationCode.NO_ELIGIBLE_CLAIMS
    assert section.attempts == ()
    assert fake.requests == []  # deterministic sections and empty-claim sections never call
