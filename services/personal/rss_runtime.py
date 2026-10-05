"""Personal RSS transport with a complete connect/header/body elapsed deadline."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import httpx

from services.ingestion.http_provider import HttpRSSProvider
from services.personal.deadlines import check_budget, require_call_budget


class _MemoryResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: httpx.Headers):
        super().__init__(body)
        self.headers = headers


class DeadlineRSSProvider(HttpRSSProvider):
    def __init__(self, *, transport: Any = None, **kwargs):
        super().__init__(**kwargs)
        self._transport = transport

    def _open_response(self, request):
        bound = require_call_budget(self._timeout)

        async def receive():
            try:
                async with asyncio.timeout(bound):
                    async with httpx.AsyncClient(
                        transport=self._transport,
                        timeout=bound,
                        follow_redirects=True,
                        trust_env=False,
                    ) as client:
                        async with client.stream(
                            "GET", request.full_url, headers=dict(request.header_items())
                        ) as response:
                            response.raise_for_status()
                            headers = httpx.Headers(response.headers)
                            if headers.get("Content-Encoding", "identity").lower() != "identity":
                                headers.pop("Content-Length", None)
                            try:
                                length = int(headers.get("Content-Length", ""))
                            except ValueError:
                                length = None
                            if length is not None and length > self._max_response_bytes:
                                return _MemoryResponse(b"", headers)
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                check_budget()
                                room = self._max_response_bytes - len(body)
                                body.extend(chunk[:room])
                                if len(body) == self._max_response_bytes:
                                    break
                            check_budget()
                            return _MemoryResponse(bytes(body), headers)
            except TimeoutError as exc:
                raise TimeoutError("RSS total deadline elapsed") from exc

        return asyncio.run(receive())
