"""Event hotness: bounded, deterministic, and not severity under another name.

DB-free. The blocker Stage 6 opened with was that `events` had no hotness at all and the
brief's ranking would have had to borrow `severity_score`; these tests exist to prove the
borrow did not quietly happen anyway.
"""

from __future__ import annotations

import datetime
import itertools

import pytest

from services.nlp.features import (
    MIN_SPAN_HOURS,
    NEUTRAL_COMPONENT,
    ArticleRecord,
    compute_event_features,
    coverage_span_hours,
    event_hotness_score,
    event_severity_score,
)
from services.reports.selection import HOTNESS_FLOOR

UTC = datetime.UTC


def _record(index: int, source: str, hour: float, authority: float | None = None) -> ArticleRecord:
    return ArticleRecord(
        key=f"a{index}",
        source_id=source,
        published_at=datetime.datetime(2026, 7, 13, 12, tzinfo=UTC)
        + datetime.timedelta(hours=hour),
        title=f"headline {index}",
        source_authority=authority,
    )


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("articles", "sources", "span", "authority", "novelty"),
    list(
        itertools.product(
            [0, 1, 5, 50, 10_000],
            [0, 1, 3, 40],
            [0.0, 0.001, 1.0, 72.0],
            [None, 0.0, 0.5, 1.0],
            [None, 0.0, 1.0],
        )
    ),
)
def test_hotness_is_bounded_0_100(
    articles: int, sources: int, span: float, authority: float | None, novelty: float | None
) -> None:
    score = event_hotness_score(articles, sources, span, authority, novelty)
    assert 0.0 <= score <= 100.0


def test_hotness_stays_bounded_for_absurd_inputs() -> None:
    # Negative counts and a zero span are the two ways a caller could try to divide by nothing
    # or drive a component past 1.0; both are absorbed rather than propagated.
    assert 0.0 <= event_hotness_score(-5, -5, 0.0, None, None) <= 100.0
    assert 0.0 <= event_hotness_score(10**9, 10**9, 1e-12, 5.0, 5.0) <= 100.0


def test_out_of_range_authority_and_novelty_are_clamped_not_trusted() -> None:
    assert event_hotness_score(5, 3, 5.0, 9.0, 9.0) == event_hotness_score(5, 3, 5.0, 1.0, 1.0)
    assert event_hotness_score(5, 3, 5.0, -9.0, -9.0) == event_hotness_score(5, 3, 5.0, 0.0, 0.0)


def test_hotness_is_deterministic() -> None:
    assert event_hotness_score(7, 4, 3.0, 0.8) == event_hotness_score(7, 4, 3.0, 0.8)


# --------------------------------------------------------------------------------------
# Hotness is not severity
# --------------------------------------------------------------------------------------


def test_hotness_differs_from_severity_on_the_same_cluster() -> None:
    # Same volume and diversity -- the only two things severity can see -- and the scores
    # still diverge, because hotness also reads velocity and authority.
    assert event_hotness_score(20, 8, 4.0, 0.9) != event_severity_score(20, 8)
    assert event_hotness_score(5, 3, 5.0, None) != event_severity_score(5, 3)


def test_velocity_separates_clusters_severity_cannot_tell_apart() -> None:
    # Identical volume and source diversity: severity is blind here by construction. The
    # story that broke in an hour is hotter than the one that dribbled out over two days.
    breaking = event_hotness_score(12, 5, 1.0, 0.7)
    slow_burn = event_hotness_score(12, 5, 48.0, 0.7)
    assert event_severity_score(12, 5) == event_severity_score(12, 5)
    assert breaking > slow_burn


def test_authority_separates_clusters_severity_cannot_tell_apart() -> None:
    wire_services = event_hotness_score(12, 5, 6.0, 0.95)
    unknown_blogs = event_hotness_score(12, 5, 6.0, 0.05)
    assert wire_services > unknown_blogs


# --------------------------------------------------------------------------------------
# Monotonicity in each real input
# --------------------------------------------------------------------------------------


def test_hotness_rises_with_volume_diversity_and_authority() -> None:
    base = event_hotness_score(8, 4, 6.0, 0.5)
    assert event_hotness_score(16, 4, 6.0, 0.5) > base
    assert event_hotness_score(8, 8, 6.0, 0.5) > base
    assert event_hotness_score(8, 4, 6.0, 0.9) > base


def test_coverage_span_is_floored_so_a_burst_cannot_divide_by_zero() -> None:
    instant = datetime.datetime(2026, 7, 13, 12, tzinfo=UTC)
    assert coverage_span_hours(instant, instant) == MIN_SPAN_HOURS
    assert coverage_span_hours(instant, instant + datetime.timedelta(minutes=6)) == MIN_SPAN_HOURS
    assert coverage_span_hours(instant, instant + datetime.timedelta(hours=9)) == 9.0


# --------------------------------------------------------------------------------------
# Unmeasured components contribute neutrally, not favourably
# --------------------------------------------------------------------------------------


def test_unmeasured_authority_scores_neutral_not_maximal_or_zero() -> None:
    unmeasured = event_hotness_score(10, 4, 4.0, mean_authority=None)
    assert unmeasured == event_hotness_score(10, 4, 4.0, mean_authority=NEUTRAL_COMPONENT)
    assert event_hotness_score(10, 4, 4.0, 0.0) < unmeasured < event_hotness_score(10, 4, 4.0, 1.0)


def test_unmeasured_novelty_scores_neutral_not_maximal() -> None:
    unmeasured = event_hotness_score(10, 4, 4.0, 0.6, novelty=None)
    assert unmeasured == event_hotness_score(10, 4, 4.0, 0.6, novelty=NEUTRAL_COMPONENT)
    assert unmeasured < event_hotness_score(10, 4, 4.0, 0.6, novelty=1.0)


# --------------------------------------------------------------------------------------
# The floor separates real events from noise
# --------------------------------------------------------------------------------------


def test_a_lone_article_does_not_clear_the_qualifying_floor() -> None:
    lone = compute_event_features([_record(0, "s1", 0.0)])
    assert lone["hotness_score"] < HOTNESS_FLOOR


def test_a_fast_well_sourced_cluster_clears_the_floor() -> None:
    records = [_record(i, f"s{i % 6}", i * 0.2, authority=0.8) for i in range(18)]
    assert compute_event_features(records)["hotness_score"] >= HOTNESS_FLOOR


# --------------------------------------------------------------------------------------
# The clustering pipeline emits it
# --------------------------------------------------------------------------------------


def test_compute_event_features_emits_a_bounded_hotness_beside_severity() -> None:
    records = [_record(i, f"s{i % 4}", i * 0.5, authority=0.9) for i in range(10)]
    features = compute_event_features(records)
    assert 0.0 <= features["hotness_score"] <= 100.0
    assert features["hotness_score"] != features["severity_score"]


def test_hotness_does_not_inherit_the_placeholder_novelty_score() -> None:
    # `novelty_score` is a hardcoded 1.0 the pipeline does not actually measure. Feeding it to
    # hotness would add a flat 0.10 to every event while dressing an unmeasured input up as a
    # maximal one, so the feature builder passes novelty=None and hotness scores it neutrally.
    records = [_record(i, f"s{i % 3}", i * 0.4, authority=0.6) for i in range(9)]
    features = compute_event_features(records)
    assert features["novelty_score"] == 1.0
    assert features["hotness_score"] == event_hotness_score(
        article_count=features["article_count"],
        source_count=features["source_count"],
        span_hours=coverage_span_hours(features["first_seen_at"], features["last_seen_at"]),
        mean_authority=features["source_authority_score"],
        novelty=None,
    )
    assert features["hotness_score"] != event_hotness_score(
        article_count=features["article_count"],
        source_count=features["source_count"],
        span_hours=coverage_span_hours(features["first_seen_at"], features["last_seen_at"]),
        mean_authority=features["source_authority_score"],
        novelty=1.0,
    )
