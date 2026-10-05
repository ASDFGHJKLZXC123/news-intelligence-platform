"""Explicit file-backed providers for the disposable personal workflow harness only."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from db.models import EMBEDDING_DIM, Source
from packages.config.settings import Settings
from packages.providers.base import RSSItem, RSSProvider
from packages.providers.fakes import FakeEmbeddingProvider, FakeRSSProvider
from services.llm.cache import BoundedLRULLMPromptCache
from services.llm.fake_providers import CallableLLMProvider
from services.llm.orchestrator import LLMOrchestrator
from services.llm.repository import SQLAlchemyLLMRuntimeRepository
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_SCHEMA,
    GROUNDING_SCHEMA_VERSION,
)
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    COMPOSITION_SCHEMA_VERSION,
)

MAX_OFFLINE_FIXTURE_BYTES = 1_048_576
OFFLINE_GENERATION_PROVIDER = "offline-personal-fixture"
OFFLINE_EMBEDDING_PROVIDER = "offline-personal-fixture"


def offline_fixture_route(fixture_id: str) -> dict[str, Any]:
    """Return the complete immutable provider identity for one labeled fixture."""

    return {
        "mode": "offline_fixture",
        "fixture_id": fixture_id,
        "generation": {
            "provider": OFFLINE_GENERATION_PROVIDER,
            "model": f"offline-personal-fixture:{fixture_id}",
        },
        "embedding": {
            "provider": OFFLINE_EMBEDDING_PROVIDER,
            "model": f"offline-fixture-embedding:{fixture_id}",
            "model_version": "fixture-v1",
        },
    }


@dataclass(frozen=True)
class OfflinePersonalFixture:
    fixture_id: str
    label: str
    items_by_feed: dict[str, tuple[RSSItem, ...]]
    executive_text: str
    event_text: str
    _prompt_cache: BoundedLRULLMPromptCache = field(
        default_factory=BoundedLRULLMPromptCache, init=False, repr=False, compare=False
    )

    @property
    def route(self) -> dict[str, Any]:
        return offline_fixture_route(self.fixture_id)

    def validate_route(self, route: dict[str, Any]) -> None:
        if route != self.route:
            raise ValueError("frozen route does not match the configured offline fixture")

    def rss_provider(self, source: Source) -> RSSProvider:
        items = self.items_by_feed.get(source.feed_url)
        if items is None:
            raise ValueError("selected feed is absent from the labeled offline fixture")
        return FakeRSSProvider(list(items))

    def embedding_provider(self) -> FakeEmbeddingProvider:
        provider = FakeEmbeddingProvider(
            dimension=EMBEDDING_DIM,
            model=f"offline-fixture-embedding:{self.fixture_id}",
        )
        provider.provider_name = OFFLINE_EMBEDDING_PROVIDER
        provider.model_version = "fixture-v1"
        return provider

    def orchestrator(self, session: Session, route: dict[str, Any]) -> LLMOrchestrator:
        self.validate_route(route)

        def respond(request):  # noqa: ANN001, ANN202
            allowed = list(request.context.get("allowed_claim_ids", []))
            if request.requested_schema == COMPOSITION_SCHEMA:
                if not allowed:
                    raise ValueError("offline composition fixture requires a supported claim")
                text = (
                    self.executive_text
                    if request.context["section_kind"] == "executive_summary"
                    else self.event_text
                )
                return {
                    "schema_name": COMPOSITION_SCHEMA,
                    "schema_version": COMPOSITION_SCHEMA_VERSION,
                    "prompt_template_version": COMPOSITION_PROMPT_TEMPLATE_VERSION,
                    "blocks": [{"text": text, "claim_ids": [allowed[0]]}],
                }
            if request.requested_schema == GROUNDING_SCHEMA:
                return {
                    "schema_name": GROUNDING_SCHEMA,
                    "schema_version": GROUNDING_SCHEMA_VERSION,
                    "prompt_template_version": GROUNDING_PROMPT_TEMPLATE_VERSION,
                    "verdicts": [
                        {"claim_id": claim_id, "verdict": "supported"} for claim_id in allowed
                    ],
                }
            raise ValueError("offline fixture received an unsupported prompt schema")

        provider = CallableLLMProvider(
            respond,
            provider_name=OFFLINE_GENERATION_PROVIDER,
            model_name=f"offline-personal-fixture:{self.fixture_id}",
        )
        return LLMOrchestrator(
            settings=Settings(app_env="test", llm_budget_enforced=False),
            repository=SQLAlchemyLLMRuntimeRepository(session, commit_on_write=False),
            providers_by_tier={"T1": (provider,), "T2": (provider,), "T3": (provider,)},
            cache=self._prompt_cache,
        )


def _instant(value: object) -> datetime.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("offline fixture publication timestamp must be an ISO string or null")
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("offline fixture publication timestamp must be timezone-aware")
    return parsed.astimezone(datetime.UTC)


def load_offline_personal_fixture(path_value: str) -> OfflinePersonalFixture:
    """Load a small labeled fixture; this function never resolves a default path."""

    if not path_value:
        raise ValueError("PERSONAL_OFFLINE_FIXTURE_PATH is required for offline fixture mode")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ValueError("configured personal offline fixture file is unavailable")
    if path.stat().st_size > MAX_OFFLINE_FIXTURE_BYTES:
        raise ValueError("personal offline fixture exceeds its one-MiB bound")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "personal-offline-fixture.v1":
        raise ValueError("personal offline fixture schema is unsupported")
    fixture_id = payload.get("fixture_id")
    label = payload.get("label")
    sources = payload.get("sources")
    composition = payload.get("composition")
    if (
        not isinstance(fixture_id, str)
        or not fixture_id
        or not isinstance(label, str)
        or not label
        or not isinstance(sources, dict)
        or not isinstance(composition, dict)
    ):
        raise ValueError("personal offline fixture identity or content is invalid")
    items_by_feed: dict[str, tuple[RSSItem, ...]] = {}
    for feed_url, rows in sources.items():
        if not isinstance(feed_url, str) or not isinstance(rows, list) or len(rows) > 500:
            raise ValueError("personal offline fixture feed is invalid or exceeds 500 items")
        items: list[RSSItem] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("personal offline fixture item is invalid")
            try:
                items.append(
                    RSSItem(
                        guid=str(row["guid"]),
                        title=str(row["title"]),
                        url=str(row["url"]),
                        published_at=_instant(row.get("published_at")),
                        summary=str(row.get("summary") or ""),
                        source=feed_url,
                        provider_name="offline-personal-fixture",
                        source_refs=(f"fixture:{fixture_id}:{feed_url}",),
                        evidence_refs=(str(row["url"]),),
                    )
                )
            except KeyError as exc:
                raise ValueError(
                    "personal offline fixture item is missing a required field"
                ) from exc
        items_by_feed[feed_url] = tuple(items)
    executive_text = composition.get("executive_text")
    event_text = composition.get("event_text")
    if not isinstance(executive_text, str) or not isinstance(event_text, str):
        raise ValueError("personal offline fixture composition text is invalid")
    return OfflinePersonalFixture(
        fixture_id=fixture_id,
        label=label,
        items_by_feed=items_by_feed,
        executive_text=executive_text,
        event_text=event_text,
    )


__all__ = [
    "OfflinePersonalFixture",
    "load_offline_personal_fixture",
    "offline_fixture_route",
]
