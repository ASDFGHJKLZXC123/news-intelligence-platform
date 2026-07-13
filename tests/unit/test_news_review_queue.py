"""The unresolved news-mention review queue (ADR 0005: "weekly manual pass for MVP").

The queue is a read-only aggregation over the ``entity_resolution_runs`` rows the linker writes,
so most of these tests build those rows directly: that is the queue's real input, and constructing
it by hand is what lets a test pin a ``created_at`` (the column is a server default, so a run only
gets one from the database). The end-to-end test at the bottom goes through the linker instead, to
show the two halves agree on the format they hand each other.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from db.models import EntityResolutionRun
from services.entities.news_linking import (
    NEWS_MENTION_TARGET_TYPE,
    REASON_BELOW_ADJUDICATE,
    LinkBand,
    format_link_explanation,
    link_mention,
    mention_run_key,
)
from services.entities.review_queue import (
    REPRESENTATIVE_SURFACE_LIMIT,
    REVIEW_WINDOW,
    unresolved_mention_queue,
    weekly_unresolved_mention_queue,
)
from services.provider_data.common import normalize_alias
from tests.unit.test_news_entity_linking import (
    FakeSession,
    SqlSession,
    _alias,
    _context,
    _mention,
    _profile,
    _seed,
)

NOW = datetime.datetime(2024, 6, 10, 12, 0, tzinfo=datetime.UTC)


def _run(
    surface: str,
    *,
    article_key: str = "article-1",
    start_char: int = 0,
    created_at: datetime.datetime | None = NOW,
    score: float | None = 0.10,
    reason: str = REASON_BELOW_ADJUDICATE,
    matched_entity_id: uuid.UUID | None = None,
    target_type: str = NEWS_MENTION_TARGET_TYPE,
    explanation: str | None = None,
) -> EntityResolutionRun:
    """One persisted run, shaped exactly as ``NewsEntityLinker._persist`` writes it."""
    end_char = start_char + len(surface)
    if explanation is None:
        explanation = format_link_explanation(
            {
                "band": LinkBand.NIL.value,
                "score": "" if score is None else f"{score:.4f}",
                "reason": reason,
            }
        )
    return EntityResolutionRun(
        id=uuid.uuid4(),
        run_key=f"{target_type}:link:{article_key}:{start_char}-{end_char}:{uuid.uuid4()}",
        target_type=target_type,
        target_id=f"{article_key}#{start_char}-{end_char}",
        input_names=[surface],
        matched_entity_id=matched_entity_id,
        confidence_score=score,
        explanation=explanation,
        created_at=created_at,
    )


# --- Grouping ------------------------------------------------------------------
def test_occurrences_of_one_surface_group_under_its_normalized_form() -> None:
    session = FakeSession()
    _seed(
        session,
        _run("Acme Corp.", article_key="a", start_char=0),
        _run("ACME", article_key="b", start_char=10),
        _run("Acme", article_key="b", start_char=40),
    )

    entry = unresolved_mention_queue(session)[0]

    assert entry.normalized_surface == normalize_alias("Acme Corp.") == "acme"
    assert entry.occurrence_count == 3
    assert entry.article_keys == ("a", "b")


def test_representative_surfaces_are_the_most_frequent_ones_and_are_capped() -> None:
    session = FakeSession()
    _seed(
        session,
        *(_run("Acme", start_char=index) for index in range(3)),
        *(_run("ACME", start_char=10 + index) for index in range(2)),
        _run("Acme Corp.", start_char=30),
        _run("acme", start_char=40),
    )

    entry = unresolved_mention_queue(session)[0]

    assert REPRESENTATIVE_SURFACE_LIMIT == 3
    # Most frequent first, then alphabetically: "ACME" (2) sorts before "Acme Corp." (1).
    assert entry.representative_surfaces == ("Acme", "ACME", "Acme Corp.")


def test_the_queue_is_ordered_by_frequency_then_normalized_surface() -> None:
    session = FakeSession()
    _seed(
        session,
        _run("Zeta", start_char=0),
        _run("Zeta", start_char=10),
        _run("Alpha", start_char=20),
        _run("Beta", start_char=30),
    )

    queue = unresolved_mention_queue(session)

    assert [(entry.normalized_surface, entry.occurrence_count) for entry in queue] == [
        ("zeta", 2),  # the frequent unknown leads
        ("alpha", 1),  # then the ties, alphabetically
        ("beta", 1),
    ]


def test_a_surface_with_no_normalized_form_is_not_a_review_item() -> None:
    session = FakeSession()
    _seed(session, _run("!!!"), _run("Acme"))

    assert [entry.normalized_surface for entry in unresolved_mention_queue(session)] == ["acme"]


# --- What belongs in the queue --------------------------------------------------
def test_a_mention_that_attached_an_entity_is_not_in_the_queue() -> None:
    session = FakeSession()
    _seed(
        session,
        _run("Acme", start_char=0, matched_entity_id=uuid.uuid4(), score=1.0),
        _run("Globex", start_char=10),
    )

    assert [entry.normalized_surface for entry in unresolved_mention_queue(session)] == ["globex"]


def test_a_run_from_the_provider_resolver_is_not_a_news_mention() -> None:
    session = FakeSession()
    _seed(session, _run("Acme", target_type="sec_company"), _run("Globex"))

    assert [entry.normalized_surface for entry in unresolved_mention_queue(session)] == ["globex"]


# --- Band, score, and reason ----------------------------------------------------
@pytest.mark.parametrize(
    ("score", "band"),
    [
        (None, LinkBand.NIL),  # nothing matched the surface at all
        (0.0, LinkBand.NIL),
        (0.4999, LinkBand.NIL),
        (0.50, LinkBand.ADJUDICATE),
        (0.84, LinkBand.ADJUDICATE),
        # An unmatched run at or above the accept threshold is one the tie or brand gate held
        # back: it is an adjudicate, and the queue must not read it as an accept.
        (0.85, LinkBand.ADJUDICATE),
        (1.0, LinkBand.ADJUDICATE),
    ],
)
def test_the_band_is_derived_from_the_runs_own_columns(score: float | None, band: LinkBand) -> None:
    session = FakeSession()
    _seed(session, _run("Acme", score=score))

    entry = unresolved_mention_queue(session)[0]

    assert entry.band is band
    assert entry.confidence_score == score


def test_a_group_is_represented_by_its_best_scoring_occurrence() -> None:
    session = FakeSession()
    _seed(
        session,
        _run("Acme", start_char=0, score=0.10, reason=REASON_BELOW_ADJUDICATE),
        _run("Acme", start_char=10, score=0.62, reason="the near miss"),
    )

    entry = unresolved_mention_queue(session)[0]

    assert entry.confidence_score == 0.62
    assert entry.band is LinkBand.ADJUDICATE
    assert entry.reason == "the near miss"


def test_an_explanation_the_linker_did_not_write_yields_no_reason_but_still_bands() -> None:
    """The band never depends on the text: only the human-facing reason is read out of it."""
    session = FakeSession()
    _seed(session, _run("Acme", score=0.60, explanation="free-form prose from somewhere else"))

    entry = unresolved_mention_queue(session)[0]

    assert entry.reason is None
    assert entry.band is LinkBand.ADJUDICATE
    assert entry.confidence_score == 0.60


# --- Filters --------------------------------------------------------------------
def test_min_occurrences_keeps_only_the_frequent_unknowns() -> None:
    session = FakeSession()
    _seed(
        session,
        _run("Acme", start_char=0),
        _run("Acme", start_char=10),
        _run("Globex", start_char=20),
    )

    queue = unresolved_mention_queue(session, min_occurrences=2)

    assert [entry.normalized_surface for entry in queue] == ["acme"]


@pytest.mark.parametrize(
    ("created_at", "kept"),
    [
        (NOW - datetime.timedelta(days=8), False),  # before the window
        (NOW - REVIEW_WINDOW, True),  # the window's lower bound is inclusive
        (NOW - datetime.timedelta(days=3), True),
        (NOW, True),  # and so is its upper bound
        (NOW + datetime.timedelta(seconds=1), False),  # after it
    ],
)
def test_the_time_window_is_inclusive_at_both_bounds(
    created_at: datetime.datetime, kept: bool
) -> None:
    session = FakeSession()
    _seed(session, _run("Acme", created_at=created_at))

    assert bool(weekly_unresolved_mention_queue(session, as_of=NOW)) is kept


def test_an_undatable_run_is_excluded_from_a_window_but_kept_without_one() -> None:
    session = FakeSession()
    _seed(session, _run("Acme", created_at=None))

    assert unresolved_mention_queue(session)[0].occurrence_count == 1
    assert weekly_unresolved_mention_queue(session, as_of=NOW) == ()


def test_first_and_last_seen_span_the_groups_occurrences() -> None:
    session = FakeSession()
    earliest = NOW - datetime.timedelta(days=5)
    latest = NOW - datetime.timedelta(days=1)
    _seed(
        session,
        _run("Acme", start_char=0, created_at=latest),
        _run("Acme", start_char=10, created_at=earliest),
        _run("Acme", start_char=20, created_at=NOW - datetime.timedelta(days=3)),
    )

    entry = weekly_unresolved_mention_queue(session, as_of=NOW)[0]

    assert entry.first_seen_at == earliest
    assert entry.last_seen_at == latest


def test_the_limit_truncates_the_ordered_queue() -> None:
    session = FakeSession()
    _seed(
        session,
        _run("Acme", start_char=0),
        _run("Acme", start_char=10),
        _run("Globex", start_char=20),
    )

    queue = unresolved_mention_queue(session, limit=1)

    assert [entry.normalized_surface for entry in queue] == ["acme"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_occurrences": 0}, "min_occurrences"),
        ({"limit": 0}, "limit"),
        ({"since": datetime.datetime(2024, 6, 1)}, "timezone-aware"),
        ({"until": datetime.datetime(2024, 6, 1)}, "timezone-aware"),
    ],
)
def test_the_queue_refuses_arguments_it_cannot_honour(kwargs: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        unresolved_mention_queue(FakeSession(), **kwargs)


# --- Against the linker and the real session path -------------------------------
def test_the_linkers_unresolved_runs_are_exactly_what_the_queue_reads_back() -> None:
    """End to end: what ``_persist`` writes is what the queue groups, bands, and explains."""
    session = FakeSession()
    profile = _profile("Globex Corporation")
    _seed(session, profile, _alias(profile, "Globex"))

    # "Acme" matches nothing in the identity store; "Globex" matches, but scores nothing.
    unknown = link_mention(session, _mention("Acme"), _context(), persist_run=True)
    known = link_mention(session, _mention("Globex", start_char=40), _context(), persist_run=True)

    queue = unresolved_mention_queue(session)

    assert unknown.band is LinkBand.NIL and known.band is LinkBand.NIL
    assert [entry.normalized_surface for entry in queue] == ["acme", "globex"]
    assert {entry.reason for entry in queue} == {unknown.reason, known.reason}
    assert [entry.band for entry in queue] == [LinkBand.NIL, LinkBand.NIL]
    # The run the queue grouped is the one the linker keyed.
    assert session.find_one(EntityResolutionRun, run_key=mention_run_key(_mention("Acme")))


def test_the_queue_reads_the_same_rows_through_the_sqlalchemy_session_path() -> None:
    session = SqlSession(
        _run("Acme", start_char=0, created_at=NOW - datetime.timedelta(days=1)),
        _run("Acme", start_char=10, created_at=NOW - datetime.timedelta(days=2)),
        _run("Stale", created_at=NOW - datetime.timedelta(days=30)),
        _run("Matched", matched_entity_id=uuid.uuid4(), score=1.0),
    )

    queue = weekly_unresolved_mention_queue(session, as_of=NOW)

    assert [(entry.normalized_surface, entry.occurrence_count) for entry in queue] == [("acme", 2)]
    # The window and the unresolved filter are pushed into SQL rather than applied after the read.
    where = str(session.statements[0].whereclause)
    assert "entity_resolution_runs.target_type = " in where
    assert "entity_resolution_runs.matched_entity_id IS NULL" in where
    assert where.count("entity_resolution_runs.created_at") == 2  # both window bounds


def test_a_profile_that_later_learns_the_alias_drops_out_of_the_queue() -> None:
    """The queue is the ADR's feedback loop: seeding the missing alias resolves the surface."""
    session = FakeSession()
    mention = _mention("Globex")
    link_mention(session, mention, _context(), persist_run=True)
    assert [entry.normalized_surface for entry in unresolved_mention_queue(session)] == ["globex"]

    profile = _profile("Globex Corporation", entity_type="company")
    _seed(session, profile, _alias(profile, "Globex"))
    linked = link_mention(session, mention, _context(), persist_run=True)

    # The same mention, re-linked in place: the run is updated, not duplicated.
    assert len(session.all_of(EntityResolutionRun)) == 1
    assert linked.candidates[0].entity_id == profile.id
    assert unresolved_mention_queue(session)[0].confidence_score == 0.10


def test_the_review_window_is_the_adrs_week() -> None:
    assert REVIEW_WINDOW == datetime.timedelta(days=7)
