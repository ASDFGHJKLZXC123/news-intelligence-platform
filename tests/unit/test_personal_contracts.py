from __future__ import annotations

import datetime
import math
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from apps.api.personal import WorkspaceSetupRequest
from services.personal.contracts import (
    RankedEvent,
    article_matches_profile,
    normalize_phrase_tokens,
    personal_local_date,
    rank_events,
)
from services.personal.workspace import configure_profile


def test_interest_matching_is_nfkc_casefolded_complete_token_and_exclude_wins() -> None:
    assert normalize_phrase_tokens("ＩＮＴＥＲＥＳＴ_rate") == ("interest", "rate")
    assert article_matches_profile(
        "INTEREST-RATE decision",
        "Officials released the statement.",
        include_phrases=["interest rate"],
        exclude_phrases=[],
    )
    assert not article_matches_profile(
        "INTEREST-RATE decision",
        "Opinion from the editorial board.",
        include_phrases=["interest rate"],
        exclude_phrases=["opinion"],
    )
    assert not article_matches_profile(
        "Interested rates move",
        None,
        include_phrases=["interest rate"],
        exclude_phrases=[],
    )
    assert not article_matches_profile(
        "AI policy",
        "said interest rates changed",
        include_phrases=["AI said"],
        exclude_phrases=[],
    )


def test_empty_include_accepts_and_tokenless_configuration_is_rejected() -> None:
    assert article_matches_profile(
        "Any selected-feed article", None, include_phrases=[], exclude_phrases=[]
    )
    with pytest.raises(ValueError, match="no letter or digit"):
        article_matches_profile("story", None, include_phrases=["___"], exclude_phrases=[])


def test_personal_date_uses_workspace_timezone_and_accepts_weekends() -> None:
    now = datetime.datetime(2026, 9, 7, 6, 55, tzinfo=datetime.UTC)
    assert personal_local_date(now, "America/Los_Angeles") == datetime.date(2026, 9, 6)


def test_personal_ranking_uses_source_hotness_publication_then_uuid() -> None:
    a_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    b_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    c_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    now = datetime.datetime(2026, 9, 6, tzinfo=datetime.UTC)
    ranked = rank_events(
        [
            RankedEvent(a_id, 3, 10, now),
            RankedEvent(b_id, 2, 99, now),
            RankedEvent(c_id, 3, None, now),
        ]
    )
    assert [item.event_id for item in ranked] == [a_id, c_id, b_id]


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, "Infinity", "NaN"])
def test_non_finite_spending_is_rejected_before_workspace_writes(value: object) -> None:
    source_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="finite"):
        WorkspaceSetupRequest.model_validate(
            {
                "selected_source_ids": [str(source_id)],
                "execution_profile": "assisted",
                "model_route": {"mode": "live"},
                "authorized_spend_usd": value,
            }
        )

    class NoDatabaseCalls:
        def execute(self, *_args, **_kwargs):  # noqa: ANN202
            raise AssertionError("configuration validation reached the database")

    with pytest.raises(ValueError, match="finite"):
        configure_profile(
            NoDatabaseCalls(),  # type: ignore[arg-type]
            SimpleNamespace(id=uuid.uuid4()),
            selected_source_ids=[source_id],
            execution_profile="assisted",
            settings={
                "model_route": {"mode": "live"},
                "authorized_spend_usd": value,
            },
        )
