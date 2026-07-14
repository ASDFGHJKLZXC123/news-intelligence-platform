"""Production OpenAI embeddings adapter (ADR 0004).

Transport is mocked end to end (``httpx.MockTransport``) and the sleep is injected, so the retry
paths are exercised at full speed and no test touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from db.models import EMBEDDING_DIM
from packages.providers.openai_embeddings import (
    MAX_EMBEDDING_BATCH_SIZE,
    OPENAI_EMBEDDING_DIMENSIONS,
    EmbeddingProviderError,
    OpenAIEmbeddingProvider,
    is_retryable_status,
)

API_KEY = "sk-test-do-not-log-me"
MODEL = "text-embedding-3-small"
VERSION = "current"
DIM = 4

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(handler: Handler, *, sleeps: list[float] | None = None, **kwargs: Any):
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.openai.invalid"
    )
    return OpenAIEmbeddingProvider(
        api_key=API_KEY,
        model_name=MODEL,
        model_version=VERSION,
        dimension=kwargs.pop("dimension", DIM),
        client=client,
        sleep=(sleeps.append if sleeps is not None else lambda _seconds: None),
        backoff_base_seconds=kwargs.pop("backoff_base_seconds", 1.0),
        **kwargs,
    )


def _index_of(text: str) -> int:
    return int(text.rsplit("-", 1)[1])


def _echo_handler(seen: list[list[str]]) -> Handler:
    """Answer with one vector per input, carrying the input's own sequence number."""

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        seen.append(list(inputs))
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "data": [
                    {"index": position, "embedding": [float(_index_of(text))] * DIM}
                    for position, text in enumerate(inputs)
                ],
            },
        )

    return handler


def _static_handler(payload: Any, status: int = 200) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    return handler


def _one_vector(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"index": 0, "embedding": [0.1] * DIM}
    entry.update(overrides)
    return {"model": MODEL, "data": [entry]}


# --- configuration --------------------------------------------------------------------
def test_the_default_dimension_is_the_embedding_column_width() -> None:
    # Drift here would let a provider produce vectors the table cannot index.
    assert OPENAI_EMBEDDING_DIMENSIONS == EMBEDDING_DIM == 1536


def test_a_missing_api_key_fails_before_any_request() -> None:
    with pytest.raises(EmbeddingProviderError, match="API key"):
        OpenAIEmbeddingProvider(api_key="", model_name=MODEL, model_version=VERSION)


def test_a_batch_over_the_cap_is_refused_at_construction() -> None:
    with pytest.raises(EmbeddingProviderError, match="1..96"):
        _provider(_static_handler(_one_vector()), batch_size=MAX_EMBEDDING_BATCH_SIZE + 1)


# --- batching -------------------------------------------------------------------------
def test_requests_are_chunked_at_96_and_results_keep_caller_order() -> None:
    seen: list[list[str]] = []
    provider = _provider(_echo_handler(seen))

    results = provider.embed([f"text-{index}" for index in range(200)])

    assert [len(chunk) for chunk in seen] == [96, 96, 8]
    assert max(len(chunk) for chunk in seen) <= MAX_EMBEDDING_BATCH_SIZE
    # Every result sits at its caller's position, across chunk boundaries.
    assert [result.vector[0] for result in results] == [float(i) for i in range(200)]


def test_an_empty_batch_makes_no_request() -> None:
    seen: list[list[str]] = []
    provider = _provider(_echo_handler(seen))

    assert provider.embed([]) == []
    assert seen == []


def test_the_request_carries_the_configured_model_and_a_bearer_key() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers["authorization"]
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_one_vector())

    _provider(handler).embed(["one"])

    assert captured["body"] == {"model": MODEL, "input": ["one"]}
    assert captured["auth"] == f"Bearer {API_KEY}"
    assert captured["url"].endswith("/v1/embeddings")


def test_results_carry_the_configured_identity_and_no_secret() -> None:
    result = _provider(_static_handler(_one_vector())).embed(["one"])[0]

    assert (result.model_name, result.model_version) == (MODEL, VERSION)
    assert result.provider_name == "openai"
    assert result.dimension == DIM
    assert API_KEY not in result.model_run_id


# --- retry classification -------------------------------------------------------------
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_are_retried_with_exponential_backoff(status: int) -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(status)
        return httpx.Response(200, json=_one_vector())

    results = _provider(handler, sleeps=sleeps).embed(["one"])

    assert len(results) == 1
    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]  # base, doubled


def test_retries_stop_at_the_attempt_limit_and_the_failure_is_tagged_retryable() -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503)

    provider = _provider(handler, sleeps=sleeps, max_attempts=3)

    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed(["one"])

    assert len(attempts) == 3  # finite: never an unbounded loop
    assert sleeps == [1.0, 2.0]  # no sleep after the final attempt
    assert caught.value.retryable is True
    assert caught.value.status == 503


def test_backoff_is_capped() -> None:
    sleeps: list[float] = []
    provider = _provider(
        _static_handler(None, status=500), sleeps=sleeps, max_attempts=5, backoff_max_seconds=2.0
    )

    with pytest.raises(EmbeddingProviderError):
        provider.embed(["one"])

    assert sleeps == [1.0, 2.0, 2.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_fails_immediately_and_is_not_retryable(status: int) -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(status)

    with pytest.raises(EmbeddingProviderError) as caught:
        _provider(handler, sleeps=sleeps).embed(["one"])

    assert len(attempts) == 1  # asking the same question again cannot change the answer
    assert sleeps == []
    assert caught.value.retryable is False
    assert caught.value.status == status
    assert API_KEY not in str(caught.value)  # a 401 must not leak the key it rejected


def test_a_transport_fault_is_raised_rather_than_retried_here() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(EmbeddingProviderError) as caught:
        _provider(handler, sleeps=sleeps).embed(["one"])

    assert sleeps == []  # ADR 0004 retries 429/5xx here; the task's backoff owns the rest
    assert caught.value.retryable is False


# --- response validation --------------------------------------------------------------
def test_a_non_json_body_is_a_permanent_failure() -> None:
    sleeps: list[float] = []

    with pytest.raises(EmbeddingProviderError, match="not valid JSON") as caught:
        _provider(_static_handler(b"<html>gateway</html>"), sleeps=sleeps).embed(["one"])

    assert sleeps == []
    assert caught.value.retryable is False


def test_a_body_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(EmbeddingProviderError, match="JSON object"):
        _provider(_static_handler([1, 2, 3])).embed(["one"])


def test_a_short_response_is_never_zipped_against_the_inputs() -> None:
    payload = {"model": MODEL, "data": [{"index": 0, "embedding": [0.1] * DIM}]}

    with pytest.raises(EmbeddingProviderError, match="1 vectors for 2 inputs"):
        _provider(_static_handler(payload)).embed(["one", "two"])


def test_vectors_are_paired_by_index_not_arrival_order() -> None:
    payload = {
        "model": MODEL,
        "data": [
            {"index": 1, "embedding": [1.0] * DIM},
            {"index": 0, "embedding": [0.0] * DIM},
        ],
    }

    results = _provider(_static_handler(payload)).embed(["zero", "one"])

    assert [result.vector[0] for result in results] == [0.0, 1.0]


def test_a_duplicated_index_leaves_an_input_uncovered_and_fails() -> None:
    payload = {
        "model": MODEL,
        "data": [
            {"index": 0, "embedding": [0.0] * DIM},
            {"index": 0, "embedding": [1.0] * DIM},
        ],
    }

    with pytest.raises(EmbeddingProviderError, match="returned twice"):
        _provider(_static_handler(payload)).embed(["zero", "one"])


def test_an_out_of_range_index_is_rejected() -> None:
    with pytest.raises(EmbeddingProviderError, match="outside the 1 inputs"):
        _provider(_static_handler(_one_vector(index=7))).embed(["one"])


def test_a_missing_or_non_integer_index_is_rejected() -> None:
    with pytest.raises(EmbeddingProviderError, match="integer index"):
        _provider(_static_handler({"model": MODEL, "data": [{"embedding": [0.1] * DIM}]})).embed(
            ["one"]
        )
    with pytest.raises(EmbeddingProviderError, match="integer index"):
        _provider(_static_handler(_one_vector(index="0"))).embed(["one"])


def test_a_wrong_width_vector_is_rejected() -> None:
    with pytest.raises(EmbeddingProviderError, match=f"has 3 values, not {DIM}"):
        _provider(_static_handler(_one_vector(embedding=[0.1, 0.2, 0.3]))).embed(["one"])


def test_a_non_numeric_vector_value_is_rejected() -> None:
    with pytest.raises(EmbeddingProviderError, match="is not a number"):
        _provider(_static_handler(_one_vector(embedding=["a", "b", "c", "d"]))).embed(["one"])


def test_a_data_field_that_is_not_an_array_is_rejected() -> None:
    with pytest.raises(EmbeddingProviderError, match="'data' must be an array"):
        _provider(_static_handler({"model": MODEL, "data": {}})).embed(["one"])


def test_a_response_served_by_another_model_is_rejected() -> None:
    payload = _one_vector()
    payload["model"] = "text-embedding-3-large"

    with pytest.raises(EmbeddingProviderError, match="not the configured"):
        _provider(_static_handler(payload)).embed(["one"])


def test_a_response_that_omits_the_model_is_accepted() -> None:
    # Identity is only checked where the API exposes it; a proxy that drops the field is not
    # evidence of a wrong model.
    payload = {"data": [{"index": 0, "embedding": [0.1] * DIM}]}

    results = _provider(_static_handler(payload)).embed(["one"])

    assert results[0].model_name == MODEL


def test_the_full_width_vector_round_trips() -> None:
    payload = {"model": MODEL, "data": [{"index": 0, "embedding": [0.5] * EMBEDDING_DIM}]}

    results = _provider(_static_handler(payload), dimension=EMBEDDING_DIM).embed(["one"])

    assert len(results[0].vector) == EMBEDDING_DIM
    assert results[0].dimension == EMBEDDING_DIM


# --- non-finite vectors and the retry class ------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_vector_value_is_rejected(bad: float) -> None:
    """NaN and Inf are corruption, not degradation, and the damage would be silent.

    pgvector's cosine distance against a NaN component is NaN, `NaN >= threshold` is False, and the
    row would seed, index, and then quietly never match anything again. A width check does not
    catch it -- the vector is exactly 1536 long.
    """
    payload = {"model": MODEL, "data": [{"index": 0, "embedding": [bad] + [0.1] * (DIM - 1)}]}
    # Serialized by hand: httpx's `json=` refuses to encode a NaN, but a real service can and does
    # emit the `NaN`/`Infinity` literals, and `json.loads` accepts them without complaint. Sending
    # it as raw bytes is what makes this the response the adapter would actually have to survive.
    handler = _static_handler(json.dumps(payload).encode())

    with pytest.raises(EmbeddingProviderError, match="is not finite"):
        _provider(handler).embed(["one"])


def test_a_boolean_vector_value_is_rejected_rather_than_coerced_to_one() -> None:
    """A JSON `true` would otherwise float() into a perfectly plausible 1.0."""
    payload = {"model": MODEL, "data": [{"index": 0, "embedding": [True] + [0.1] * (DIM - 1)}]}

    with pytest.raises(EmbeddingProviderError, match="is not a number"):
        _provider(_static_handler(payload)).embed(["one"])


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_the_retry_class_is_429_and_a_real_5xx(status: int) -> None:
    assert is_retryable_status(status) is True


@pytest.mark.parametrize("status", [400, 401, 404, 418, 600, 999])
def test_a_status_outside_429_and_5xx_is_final(status: int) -> None:
    """ADR 0004 retries "429/5xx". A code above 599 is not a 5xx -- it is a proxy or a malformed
    response inventing one, and re-asking cannot make it a different question."""
    assert is_retryable_status(status) is False
