"""Production OpenAI embeddings adapter (ADR 0004).

``text-embedding-3-small`` at 1536 dimensions is the single vector space articles, events, and
historical episodes share, so this client is deliberately strict about identity: a response that
names a different model, returns a different vector width, or fails to cover every input index is
a *corrupted* vector space, not a degraded one. It fails the call rather than write something
plausible into a table other rows will later be compared against.

Exactly two failure classes are retried here (ADR 0004: "exponential backoff on 429/5xx"): 429 and
5xx. Everything else is permanent -- another 4xx, a non-JSON body, a vector of the wrong width, a
short or misindexed ``data`` array -- and retrying it would only re-ask a question that has already
been answered wrongly. Transport faults are raised to the caller, whose own backoff (the Celery
task's) owns that blast radius.

The API key is held only for the bearer header: it is never logged, never echoed into an exception
message, and never persisted.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from packages.providers.base import EmbeddingProvider, EmbeddingResult, ensure_finite_vector

#: The endpoint takes an input array, and ADR 0004 caps one request at 96 texts.
MAX_EMBEDDING_BATCH_SIZE = 96

#: Native width of ``text-embedding-3-small``. Callers pin the embedding column's ``EMBEDDING_DIM``
#: explicitly; ``db.models`` owns that number and this layer must not import it (packages sits
#: below db, and importing db.base would build an engine just to read a constant).
OPENAI_EMBEDDING_DIMENSIONS = 1536

EMBEDDINGS_PATH = "/v1/embeddings"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 60.0

SleepCallable = Callable[[float], None]


class EmbeddingProviderError(RuntimeError):
    """An embeddings call that failed, tagged with whether retrying it could ever help."""

    def __init__(
        self, message: str, *, retryable: bool = False, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def is_retryable_status(status: int) -> bool:
    """429 and a real 5xx are transient; every other status is the server's final answer.

    The upper bound is not pedantry. ADR 0004 retries "429/5xx", which is the server telling us it
    could not answer *this time*; a status outside 100-599 is not a 5xx at all but a proxy or a
    malformed response inventing a code, and re-asking cannot make it a different question.
    """
    return status == 429 or 500 <= status < 600


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embeddings client: chunked requests, validated responses, bounded retries."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        model_version: str,
        base_url: str = "https://api.openai.com",
        dimension: int = OPENAI_EMBEDDING_DIMENSIONS,
        batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        sleep: SleepCallable | None = None,
    ) -> None:
        if not api_key:
            raise EmbeddingProviderError("OpenAI API key is not configured")
        if not 1 <= batch_size <= MAX_EMBEDDING_BATCH_SIZE:
            raise EmbeddingProviderError(
                f"an embeddings request carries 1..{MAX_EMBEDDING_BATCH_SIZE} texts"
            )
        if max_attempts < 1:
            raise EmbeddingProviderError("max_attempts must allow at least one call")
        if dimension < 1:
            raise EmbeddingProviderError("dimension must be positive")

        self.model_name = model_name
        self.model_version = model_version
        self.dimension = dimension
        self._api_key = api_key
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep or time.sleep
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed any number of texts, one request per ``batch_size`` chunk, in caller order."""
        results: list[EmbeddingResult] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = list(texts[start : start + self._batch_size])
            results.extend(self._embed_chunk(chunk))
        return results

    def close(self) -> None:
        self._client.close()

    # --- transport -------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    def _embed_chunk(self, texts: list[str]) -> list[EmbeddingResult]:
        body = self._post({"model": self.model_name, "input": texts})
        return self._parse(body, texts)

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        last_status: int | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post(
                    EMBEDDINGS_PATH, json=dict(payload), headers=self._headers()
                )
            except httpx.HTTPError as exc:
                # Not one of the two classes ADR 0004 retries here; the task's backoff owns it.
                raise EmbeddingProviderError(f"OpenAI embeddings request failed: {exc}") from exc

            status = response.status_code
            if is_retryable_status(status):
                last_status = status
                if attempt < self._max_attempts:
                    self._sleep(min(self._backoff_base * 2 ** (attempt - 1), self._backoff_max))
                    continue
                break
            if status >= 400:
                raise EmbeddingProviderError(
                    f"OpenAI embeddings request failed with status {status}", status=status
                )
            return self._json_object(response)

        raise EmbeddingProviderError(
            f"OpenAI embeddings request failed with status {last_status} after "
            f"{self._max_attempts} attempts",
            retryable=True,
            status=last_status,
        )

    def _json_object(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError("OpenAI embeddings response is not valid JSON") from exc
        if not isinstance(body, Mapping):
            raise EmbeddingProviderError("OpenAI embeddings response must be a JSON object")
        return body

    # --- response validation ---------------------------------------------------------
    def _parse(self, body: Mapping[str, Any], texts: list[str]) -> list[EmbeddingResult]:
        served_model = body.get("model")
        if isinstance(served_model, str) and served_model and served_model != self.model_name:
            raise EmbeddingProviderError(
                f"OpenAI served model {served_model!r}, not the configured {self.model_name!r}"
            )

        data = body.get("data")
        if not _is_array(data):
            raise EmbeddingProviderError("OpenAI embeddings response 'data' must be an array")
        if len(data) != len(texts):
            raise EmbeddingProviderError(
                f"OpenAI returned {len(data)} vectors for {len(texts)} inputs"
            )

        # Index coverage, not arrival order, is what pairs a vector with its text: the API is
        # explicitly allowed to return `data` out of order.
        vectors: dict[int, tuple[float, ...]] = {}
        for item in data:
            if not isinstance(item, Mapping):
                raise EmbeddingProviderError("OpenAI embeddings 'data' entries must be objects")
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise EmbeddingProviderError("OpenAI embeddings entry carried no integer index")
            if not 0 <= index < len(texts):
                raise EmbeddingProviderError(
                    f"OpenAI embeddings index {index} is outside the {len(texts)} inputs sent"
                )
            if index in vectors:
                raise EmbeddingProviderError(f"OpenAI embeddings index {index} was returned twice")
            vectors[index] = self._vector(item.get("embedding"), index)

        return [self._result(vectors[index], texts[index]) for index in range(len(texts))]

    def _vector(self, embedding: Any, index: int) -> tuple[float, ...]:
        if not _is_array(embedding):
            raise EmbeddingProviderError(f"OpenAI embedding {index} is not an array")
        if len(embedding) != self.dimension:
            raise EmbeddingProviderError(
                f"OpenAI embedding {index} has {len(embedding)} values, not {self.dimension}"
            )
        try:
            # Rejects NaN/Inf as well as a non-numeric value: a non-finite component would be
            # written into the shared vector space and silently never match anything again.
            return ensure_finite_vector(embedding)
        except (TypeError, ValueError) as exc:
            raise EmbeddingProviderError(f"OpenAI embedding {index}: {exc}") from exc

    def _result(self, vector: tuple[float, ...], text: str) -> EmbeddingResult:
        return EmbeddingResult(
            vector=vector,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            dimension=self.dimension,
            model_run_id=self._model_run_id(text),
        )

    def _model_run_id(self, text: str) -> str:
        """A stable id for the (identity, text) pair. Derived from no secret."""
        seed = f"{self.provider_name}|{self.model_name}|{self.model_version}|{text}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _is_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


__all__ = [
    "MAX_EMBEDDING_BATCH_SIZE",
    "OPENAI_EMBEDDING_DIMENSIONS",
    "EmbeddingProviderError",
    "OpenAIEmbeddingProvider",
    "is_retryable_status",
]
