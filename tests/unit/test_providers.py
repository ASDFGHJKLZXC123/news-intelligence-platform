"""Fake provider tests: contract conformance and deterministic output."""

from __future__ import annotations

import datetime
from types import MappingProxyType

import pytest

from packages.providers.base import (
    Clock,
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    RSSProvider,
    SystemClock,
)
from packages.providers.fakes import (
    FakeEmbeddingProvider,
    FakeLLMProvider,
    FakeRSSProvider,
    FixedClock,
)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeRSSProvider(), RSSProvider)
    assert isinstance(FakeEmbeddingProvider(), EmbeddingProvider)
    assert isinstance(FakeLLMProvider(), LLMProvider)
    assert isinstance(FixedClock(), Clock)
    assert isinstance(SystemClock(), Clock)


def test_fake_rss_is_deterministic_and_carries_provenance() -> None:
    provider = FakeRSSProvider()
    first = provider.fetch("https://any.example/feed")
    second = provider.fetch("https://other.example/feed")
    assert first == second
    assert all(item.url.startswith("https://") for item in first)
    assert first[0].provider_name == "fake-provider"
    assert first[0].output_schema_version == "rss-item.v1"
    assert first[0].schema_version == first[0].output_schema_version
    assert first[0].source_refs == ("fake-feed",)
    assert first[0].evidence_refs == ("https://example.com/news/1",)
    assert first[0].published_at.tzinfo is datetime.UTC


def test_fake_embedding_dimension_determinism_and_provenance() -> None:
    provider = FakeEmbeddingProvider(dimension=8)
    a = provider.embed(["hello"])
    b = provider.embed(["hello"])
    assert a[0].vector == b[0].vector
    assert a[0].dimension == 8
    assert len(a[0].vector) == 8
    assert isinstance(a[0].vector, tuple)
    assert a[0].provider_name == "fake-provider"
    assert a[0].model == a[0].model_name == "fake-embed"
    assert a[0].model_version == "v1"
    assert a[0].model_run_id == b[0].model_run_id
    assert a[0].schema_version == a[0].output_schema_version
    assert provider.embed(["world"])[0].vector != a[0].vector
    assert (
        FakeEmbeddingProvider(dimension=8, model="other").embed(["hello"])[0].model_run_id
        != a[0].model_run_id
    )


def test_embedding_vector_is_immutable() -> None:
    result = FakeEmbeddingProvider(dimension=2).embed(["hello"])[0]
    with pytest.raises(AttributeError):
        result.vector.append(1.0)  # type: ignore[attr-defined]


def test_fake_llm_carries_provenance() -> None:
    response = FakeLLMProvider().complete("summary", "v1", "some prompt")
    assert response.prompt_name == "summary"
    assert response.prompt_version == "v1"
    assert response.model == response.model_name == "fake-llm"
    assert response.model_version == "v1"
    assert response.provider_name == "fake-provider"
    assert response.output_schema_version == "llm-response.v1"
    assert response.model_run_id
    assert response.source_refs
    assert response.evidence_refs
    assert "some prompt" in response.text
    assert (
        FakeLLMProvider(model="other").complete("summary", "v1", "some prompt").model_run_id
        != response.model_run_id
    )


def test_llm_metadata_is_immutable_and_copy_safe() -> None:
    metadata = {"claim_count": 2, "nested": {"ids": ["a"]}}
    response = LLMResponse(
        text="ok",
        provider_name="fake-provider",
        model_name="fake-llm",
        model_version="v1",
        model_run_id="run-1",
        prompt_name="summary",
        prompt_version="v1",
        metadata=metadata,
    )
    metadata["claim_count"] = 99
    metadata["nested"]["ids"].append("b")
    assert isinstance(response.metadata, MappingProxyType)
    assert response.metadata["claim_count"] == 2
    assert response.metadata["nested"]["ids"] == ("a",)
    with pytest.raises(TypeError):
        response.metadata["claim_count"] = 3  # type: ignore[index]
    with pytest.raises(TypeError):
        response.metadata["nested"]["ids"] += ("b",)  # type: ignore[index,operator]


def test_fixed_clock_is_constant_and_utc() -> None:
    clock = FixedClock(datetime.datetime(2030, 5, 1, tzinfo=datetime.UTC))
    assert clock.now() == clock.now()
    assert clock.now().year == 2030
    assert clock.now().tzinfo is datetime.UTC


def test_fixed_clock_rejects_naive_and_non_utc() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime.datetime(2030, 5, 1))
    with pytest.raises(ValueError):
        FixedClock(
            datetime.datetime(2030, 5, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=1)))
        )


def test_system_clock_returns_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is datetime.UTC
    assert now.utcoffset() == datetime.timedelta(0)
