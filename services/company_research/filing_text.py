"""Deterministic SEC filing text extraction helpers."""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping
from typing import Any


SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "item_1_business": re.compile(r"\bitem\s+1[\.\s:-]+business\b", re.IGNORECASE),
    "item_1a_risk_factors": re.compile(r"\bitem\s+1a[\.\s:-]+risk\s+factors\b", re.IGNORECASE),
    "item_7_mda": re.compile(r"\bitem\s+7[\.\s:-]+management'?s\s+discussion\s+and\s+analysis\b", re.IGNORECASE),
}

SECTION_ORDER = ("item_1_business", "item_1a_risk_factors", "item_7_mda")


def extract_filing_sections(
    text_or_html: str,
    *,
    accession_number: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Extract deterministic section snippets and hashes from filing text/HTML."""

    text = normalize_filing_text(text_or_html)
    located = _locate_sections(text)
    sections: dict[str, dict[str, Any]] = {}
    for section_name, start in located:
        end = _next_section_start(located, section_name, default=len(text))
        section_text = text[start:end].strip()
        if not section_text:
            continue
        sections[section_name] = {
            "text": section_text,
            "excerpt": _excerpt(section_text),
            "hash": section_hash(section_name, section_text),
            "accession_number": accession_number,
            "source_url": source_url,
        }

    return {
        "accession_number": accession_number,
        "source_url": source_url,
        "sections": sections,
        "section_hashes": {name: section["hash"] for name, section in sections.items()},
    }


def filing_business_metadata(extraction: Mapping[str, Any]) -> dict[str, Any]:
    """Build profile metadata from deterministic filing section extraction."""

    sections = extraction.get("sections") if isinstance(extraction, Mapping) else None
    if not isinstance(sections, Mapping):
        return {}

    metadata: dict[str, Any] = {"filing_sections": extraction}
    business = sections.get("item_1_business")
    risks = sections.get("item_1a_risk_factors")
    mda = sections.get("item_7_mda")
    if isinstance(business, Mapping):
        metadata["business_description"] = business.get("excerpt")
        metadata["business_description_source"] = _section_source("item_1_business", business)
    if isinstance(risks, Mapping):
        metadata["risk_factors"] = [risks.get("excerpt")]
        metadata["risk_factors_source"] = _section_source("item_1a_risk_factors", risks)
    if isinstance(mda, Mapping):
        metadata["management_strategy"] = mda.get("excerpt")
        metadata["management_strategy_source"] = _section_source("item_7_mda", mda)
    return metadata


def normalize_filing_text(text_or_html: str) -> str:
    """Convert filing HTML/text to normalized plain text."""

    text = str(text_or_html or "")
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def section_hash(section_name: str, text: str) -> str:
    """Return a stable cache key for a filing section."""

    normalized = normalize_filing_text(text)
    return hashlib.sha256(f"{section_name}\n{normalized}".encode("utf-8")).hexdigest()


def _locate_sections(text: str) -> list[tuple[str, int]]:
    located: list[tuple[str, int]] = []
    for section_name in SECTION_ORDER:
        match = SECTION_PATTERNS[section_name].search(text)
        if match:
            located.append((section_name, match.start()))
    return sorted(located, key=lambda item: item[1])


def _next_section_start(located: list[tuple[str, int]], section_name: str, *, default: int) -> int:
    for index, (candidate_name, _start) in enumerate(located):
        if candidate_name == section_name and index + 1 < len(located):
            return located[index + 1][1]
    return default


def _excerpt(text: str, *, limit: int = 700) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0]
    return truncated.rstrip(" .") + "."


def _section_source(section_name: str, section: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "section": section_name,
        "section_hash": section.get("hash"),
        "accession_number": section.get("accession_number"),
        "source_url": section.get("source_url"),
    }
