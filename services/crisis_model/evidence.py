"""Evidence helpers shared by standalone crisis-model components."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.crisis_model.contract import validate_evidence_refs
from services.crisis_model.types import ComponentForecast


def evidence_ref(kind: str, id_value: object) -> dict[str, str]:
    """Create a Phase 0 evidence ref object."""
    return {"kind": kind, "id": str(id_value)}


def dedupe_evidence_refs(
    refs: Sequence[Mapping[str, str]],
    *,
    validate: bool = True,
) -> tuple[Mapping[str, str], ...]:
    """Dedupe evidence refs by kind/id while preserving order."""
    if not refs:
        return ()
    if validate:
        validate_evidence_refs(refs)
    seen: set[tuple[str, str]] = set()
    deduped: list[Mapping[str, str]] = []
    for ref in refs:
        key = (str(ref["kind"]), str(ref["id"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"kind": key[0], "id": key[1]})
    return tuple(deduped)


def validate_component_evidence(forecast: ComponentForecast) -> None:
    """Validate evidence carried by a component forecast."""
    if forecast.evidence_refs:
        validate_evidence_refs(forecast.evidence_refs)
    for index, driver in enumerate(forecast.top_drivers):
        refs = driver.get("evidence_refs") if isinstance(driver, Mapping) else None
        if refs:
            try:
                validate_evidence_refs(refs)
            except Exception as exc:
                msg = f"top_drivers[{index}] carries invalid evidence refs"
                raise ValueError(msg) from exc


def event_feature_evidence_refs(event: Mapping[str, Any] | object) -> tuple[Mapping[str, str], ...]:
    """Build evidence refs from an event-risk-feature-like object or mapping."""
    event_id = _get(event, "event_id")
    article_ids = _get(event, "evidence_article_ids") or ()
    refs: list[Mapping[str, str]] = []
    if event_id:
        refs.append(evidence_ref("event", event_id))
    refs.extend(evidence_ref("article", article_id) for article_id in article_ids)
    return dedupe_evidence_refs(refs)


def _get(source: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)
