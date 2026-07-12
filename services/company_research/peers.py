"""Deterministic peer-group and peer-metric helpers."""

from __future__ import annotations

import datetime
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

MIN_PEERS_FOR_COMPARISON = 2


def build_peer_group(
    company: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    manual_overrides: Sequence[Mapping[str, Any]] | None = None,
    calculated_at: datetime.date | None = None,
) -> dict[str, Any]:
    """Build a defensible peer group from explicit candidates and overrides."""

    peer_candidates = [dict(item) for item in candidates]
    for override in manual_overrides or []:
        item = dict(override)
        item["override"] = True
        peer_candidates.append(item)

    peers = [_peer_summary(peer) for peer in peer_candidates if _is_peer(company, peer)]
    peers = _dedupe_peers(peers)
    metrics = _peer_medians(peers)
    calculated = calculated_at or datetime.date.today()
    return {
        "company": _peer_summary(company),
        "peers": peers,
        "peer_count": len(peers),
        "metrics": metrics,
        "calculated_at": calculated.isoformat(),
        "defensible": len(peers) >= MIN_PEERS_FOR_COMPARISON,
        "reason": None if len(peers) >= MIN_PEERS_FOR_COMPARISON else "Fewer than two defensible peers with comparable identity.",
    }


def peer_comparison_text(peer_group: Mapping[str, Any], company_metrics: Mapping[str, Any]) -> str | None:
    """Return a relative valuation summary when a peer group is defensible."""

    if not peer_group.get("defensible"):
        return None
    metrics = peer_group.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    parts: list[str] = []
    for label, key in (
        ("PE", "pe_ratio"),
        ("PS", "ps_ratio"),
        ("PB", "pb_ratio"),
        ("EV/EBITDA", "ev_ebitda"),
    ):
        company_value = _number(company_metrics.get(key))
        median_value = _number(metrics.get(key))
        if company_value is None or median_value is None:
            continue
        relation = "above" if company_value > median_value else "below" if company_value < median_value else "in line with"
        parts.append(f"{label} {company_value:g} is {relation} peer median {median_value:g}")
    if not parts:
        return None
    return "; ".join(parts) + f" ({peer_group.get('peer_count')} peers, {peer_group.get('calculated_at')})."


def _is_peer(company: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    if candidate.get("override"):
        return True
    for key in ("sic", "naics", "industry", "sector"):
        company_value = _clean(company.get(key))
        candidate_value = _clean(candidate.get(key))
        if company_value and candidate_value and company_value == candidate_value:
            return True
    return False


def _peer_summary(peer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company_id": peer.get("company_id") or peer.get("companyId") or peer.get("id"),
        "name": peer.get("name") or peer.get("display_name") or peer.get("canonical_name"),
        "ticker": _upper(peer.get("ticker") or peer.get("primary_ticker")),
        "exchange": _upper(peer.get("exchange")),
        "country": _upper(peer.get("country")),
        "sic": _clean(peer.get("sic")),
        "naics": _clean(peer.get("naics")),
        "industry": peer.get("industry"),
        "sector": peer.get("sector"),
        "metrics": dict(peer.get("metrics") or peer.get("valuation") or {}),
        "override": bool(peer.get("override")),
    }


def _dedupe_peers(peers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for peer in peers:
        marker = (peer.get("company_id"), peer.get("ticker"), peer.get("exchange"))
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(dict(peer))
    return deduped


def _peer_medians(peers: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    values: dict[str, list[float]] = {
        "pe_ratio": [],
        "ps_ratio": [],
        "pb_ratio": [],
        "ev_ebitda": [],
        "net_margin": [],
        "revenue_growth": [],
    }
    for peer in peers:
        metrics = peer.get("metrics") if isinstance(peer.get("metrics"), Mapping) else {}
        for key in values:
            value = _number(metrics.get(key))
            if value is not None:
                values[key].append(value)
    return {key: statistics.median(items) for key, items in values.items() if items}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip().removeprefix("$").replace(",", "").removesuffix("x").removesuffix("%")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().casefold()


def _upper(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().upper()
