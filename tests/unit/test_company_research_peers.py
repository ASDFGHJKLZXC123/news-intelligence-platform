"""Peer group and relative valuation tests."""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

from services.company_research import (
    build_company_research_profile,
    build_peer_group,
    peer_comparison_text,
    validate_company_research_profile,
)
from services.company_research.contract import INFORMATION_NOT_AVAILABLE


def test_peer_group_calculates_medians_for_defensible_peers() -> None:
    company = {"name": "Example Semiconductor", "ticker": "EXM", "sic": "3674", "industry": "Semiconductors"}
    peers = [
        {"name": "Peer A", "ticker": "AAA", "sic": "3674", "metrics": {"pe_ratio": 20, "ps_ratio": 6, "pb_ratio": 5}},
        {"name": "Peer B", "ticker": "BBB", "sic": "3674", "metrics": {"pe_ratio": 30, "ps_ratio": 8, "pb_ratio": 7}},
        {"name": "Not Peer", "ticker": "CCC", "sic": "1311", "metrics": {"pe_ratio": 10}},
    ]

    group = build_peer_group(company, peers, calculated_at=datetime.date(2026, 6, 20))
    text = peer_comparison_text(group, {"pe_ratio": 25, "ps_ratio": 9})

    assert group["defensible"] is True
    assert group["peer_count"] == 2
    assert group["metrics"]["pe_ratio"] == 25
    assert group["metrics"]["ps_ratio"] == 7
    assert "PE 25 is in line with peer median 25" in text
    assert "PS 9 is above peer median 7" in text


def test_peer_group_manual_override_can_create_defensible_group() -> None:
    company = {"name": "Example Defense", "ticker": "EXD", "sector": "Defense"}
    group = build_peer_group(
        company,
        [{"name": "Peer A", "ticker": "AAA", "sector": "Defense", "metrics": {"pe_ratio": 18}}],
        manual_overrides=[{"name": "Manual Peer", "ticker": "MNL", "sector": "Industrials", "metrics": {"pe_ratio": 22}}],
        calculated_at=datetime.date(2026, 6, 20),
    )

    assert group["defensible"] is True
    assert group["peer_count"] == 2
    assert group["metrics"]["pe_ratio"] == 20
    assert any(peer["override"] for peer in group["peers"])


def test_peer_comparison_unavailable_when_peer_group_is_not_defensible() -> None:
    company = {"name": "Example", "ticker": "EXM", "industry": "Specialty"}
    group = build_peer_group(
        company,
        [{"name": "Unrelated", "ticker": "AAA", "industry": "Other", "metrics": {"pe_ratio": 20}}],
        calculated_at=datetime.date(2026, 6, 20),
    )

    assert group["defensible"] is False
    assert peer_comparison_text(group, {"pe_ratio": 25}) is None


def test_profile_uses_peer_group_metadata_for_peer_comparison_only_when_defensible() -> None:
    company = SimpleNamespace(
        id=uuid.uuid4(),
        cik="0000000001",
        name="Example Semiconductor",
        ticker="EXM",
        exchange="NASDAQ",
        sic="3674",
        sic_description="Semiconductors and Related Devices",
        fiscal_year_end="1231",
        company_metadata={
            "market_data": {"pe_ratio": "30", "ps_ratio": "9", "as_of": "2026-06-20"},
            "peer_group": {
                "company": {"name": "Example Semiconductor", "ticker": "EXM", "sic": "3674"},
                "peers": [
                    {"name": "Peer A", "ticker": "AAA", "sic": "3674", "metrics": {"pe_ratio": 20, "ps_ratio": 5}},
                    {"name": "Peer B", "ticker": "BBB", "sic": "3674", "metrics": {"pe_ratio": 24, "ps_ratio": 7}},
                ],
                "calculated_at": "2026-06-20",
            },
        },
    )

    profile = build_company_research_profile(company, as_of=datetime.date(2026, 6, 20))

    assert validate_company_research_profile(profile) == []
    valuation = {field["key"]: field for field in profile["researchChecklist"]["valuation"]}
    assert valuation["peer_comparison"]["available"] is True
    assert "2 peers, 2026-06-20" in valuation["peer_comparison"]["value"]


def test_profile_keeps_peer_comparison_unavailable_without_defensible_peer_group() -> None:
    company = SimpleNamespace(
        id=uuid.uuid4(),
        cik="0000000001",
        name="Example Specialty",
        ticker="EXM",
        exchange="NASDAQ",
        sic="9999",
        sic_description="Specialty",
        fiscal_year_end="1231",
        company_metadata={
            "market_data": {"pe_ratio": "30", "as_of": "2026-06-20"},
            "peer_group": {
                "company": {"name": "Example Specialty", "ticker": "EXM", "sic": "9999"},
                "peers": [{"name": "Unrelated", "ticker": "AAA", "sic": "3674", "metrics": {"pe_ratio": 20}}],
                "calculated_at": "2026-06-20",
            },
        },
    )

    profile = build_company_research_profile(company, as_of=datetime.date(2026, 6, 20))

    valuation = {field["key"]: field for field in profile["researchChecklist"]["valuation"]}
    assert valuation["peer_comparison"]["available"] is False
    assert valuation["peer_comparison"]["value"] == INFORMATION_NOT_AVAILABLE
