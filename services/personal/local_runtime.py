"""One runner's ephemeral prompt cache and serialized provider throughput control."""

from __future__ import annotations

import contextlib
import datetime as dt
import math
import threading
import time
from collections.abc import Callable, Iterator
from email.utils import parsedate_to_datetime
from typing import Any

from services.llm.cache import BoundedLRULLMPromptCache
from services.llm.limiter import LocalProviderRateLimiter
from services.personal.deadlines import (
    GRACEFUL_SECONDS,
    RuntimeCancelled,
    check_budget,
    require_call_budget,
)
from services.personal.spending import PaidRoute, PaidWorkBlocked


class PersonalPaidRuntime:
    """Share pacing across embeddings, generation, and every physical retry."""

    def __init__(
        self,
        limiter: LocalProviderRateLimiter,
        *,
        cache: BoundedLRULLMPromptCache | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.limiter = limiter
        self.cache = cache if cache is not None else BoundedLRULLMPromptCache()
        self.monotonic, self.sleep, self.wall_clock = monotonic, sleep, wall_clock
        self._calls = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Any, providers: set[str]) -> PersonalPaidRuntime:
        return cls(
            LocalProviderRateLimiter(
                rpm_limits={
                    provider: settings.llm_provider_rpm_limits.get(provider, 0)
                    for provider in providers
                },
                tpm_limits={
                    provider: settings.llm_provider_tpm_limits.get(provider, 0)
                    for provider in providers
                },
            )
        )

    def _check_wait(self, started: float, wait: float, request_seconds: float) -> None:
        check_budget()
        if self.monotonic() - started + wait + request_seconds > GRACEFUL_SECONDS:
            raise RuntimeCancelled("provider pacing exceeds finite personal execution budget")
        # Reserve the full configured HTTP bound after every wait. Never extend the
        # child's persisted graceful deadline to accommodate Retry-After.
        require_call_budget(wait + request_seconds)

    @contextlib.contextmanager
    def call(self, route: PaidRoute, tokens: int) -> Iterator[None]:
        started = self.monotonic()
        acquired = False
        try:
            # A free lock requires no waiting allowance. Contended acquisition also
            # obeys cancellation and the same finite horizon.
            self._check_wait(started, 0, route.deadline_seconds)
            acquired = self._calls.acquire(blocking=False)
            while not acquired:
                self._check_wait(started, 0.1, route.deadline_seconds)
                acquired = self._calls.acquire(timeout=0.1)
            key = f"{route.provider}:{route.model}"
            while True:
                self._check_wait(started, 0, route.deadline_seconds)
                delay = self.limiter.delay_until_available(key, tokens)
                if not math.isfinite(delay):
                    raise PaidWorkBlocked(
                        "request_bound_exceeded",
                        "Paid request cannot fit the configured provider token ceiling",
                    )
                self._check_wait(started, delay, route.deadline_seconds)
                if self.limiter.consume(key, tokens):
                    break
                # Short waits permit Ctrl-C/deadline cancellation while cooling down.
                self.sleep(min(0.1, max(delay, 0.000001)))
            yield
        finally:
            if acquired:
                self._calls.release()

    def record_retry_after(self, provider: str, response: Any) -> None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return
        try:
            seconds = float(raw)
        except (ValueError, TypeError):
            try:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=dt.UTC)
                seconds = (target - self.wall_clock()).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return
        if math.isnan(seconds) or seconds < 0:
            return
        # Unbounded provider guidance cannot cause an unbounded wait. Preserve a
        # cooldown larger than the finite runner horizon so the next call cancels.
        if math.isinf(seconds):
            seconds = GRACEFUL_SECONDS + 1
        self.limiter.defer(provider, seconds)
