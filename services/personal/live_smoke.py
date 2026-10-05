"""Bounded accounting and capture guards for the Phase 2 live smoke run.

This module does not start a run or make a network request on its own.  The personal worker
must inject every billable HTTP client through :class:`GuardedHTTPClient` and every RSS
provider through :class:`FixedCaptureRSSProvider`.  Reservations are persisted before a paid
request leaves the process and are never released, including after transport uncertainty.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from packages.providers.base import RSSItem, RSSProvider
from packages.providers.openai_embeddings import (
    OPENAI_EMBEDDING_DIMENSIONS,
    OpenAIEmbeddingProvider,
)
from services.ingestion.normalize import normalize_url, url_hash
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
    LLMProviderAdapter,
)

CONFIG_SCHEMA: Final = "personal-live-smoke-config.v1"
LEDGER_SCHEMA: Final = "personal-live-smoke-ledger.v1"
MAX_BILLABLE_DISPATCHES: Final = 20
MAX_RESERVED_USD: Final = Decimal("0.25")
MAX_REASONING_INPUT_TOKENS: Final = 8192
MAX_REASONING_OUTPUT_TOKENS: Final = 4096
MAX_EMBEDDING_INPUT_TOKENS: Final = 8192
MAX_CAPTURE_ARTICLES: Final = 3
_PAYLOAD_PROTOCOL_OVERHEAD_TOKENS: Final = 512

_ROLES = frozenset({"reasoning", "probe", "embedding"})
_LLM_PROVIDERS = frozenset({"anthropic", "openai", "gemini", "deepseek"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_BOUNDED_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?$")


class LiveSmokeError(RuntimeError):
    """A configuration, accounting, or capture boundary was violated."""


@dataclass(frozen=True)
class PriceSource:
    """Versioned official source used for one route's configured price."""

    url: str
    version: str
    retrieved_at: str

    def public_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "version": self.version,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True)
class LiveSmokeRoute:
    """One explicitly enumerated paid route and its finite dispatch allocation."""

    route_id: str
    role: str
    provider: str
    model: str
    model_version: str
    max_dispatches: int
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    price_source: PriceSource

    def public_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "max_dispatches": self.max_dispatches,
            "input_usd_per_million_tokens": _decimal_text(
                self.input_usd_per_million_tokens
            ),
            "output_usd_per_million_tokens": _decimal_text(
                self.output_usd_per_million_tokens
            ),
            "price_source": self.price_source.public_dict(),
        }


@dataclass(frozen=True)
class FixedCaptureArticle:
    """An authorized RSS article identity; the URL itself is omitted from the ledger."""

    capture_id: str
    source_id: str
    canonical_url: str
    url_hash: str

    def ledger_dict(self) -> dict[str, str]:
        return {
            "capture_id": self.capture_id,
            "source_id": self.source_id,
            "url_hash": self.url_hash,
        }


@dataclass(frozen=True)
class LiveSmokeLimits:
    """All hard bounds enforced for one verification ID."""

    max_dispatches: int
    reasoning_input_tokens: int
    reasoning_output_tokens: int
    embedding_input_tokens: int
    embedding_batch_articles: int

    def public_dict(self) -> dict[str, int]:
        return {
            "max_dispatches": self.max_dispatches,
            "reasoning_input_tokens": self.reasoning_input_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "embedding_input_tokens": self.embedding_input_tokens,
            "embedding_batch_articles": self.embedding_batch_articles,
        }


@dataclass(frozen=True)
class LiveSmokeConfig:
    """Strict, secret-free contract for one disposable live verification run."""

    verification_id: str
    live_execution_authorized: bool
    authorized_allowance_usd: Decimal
    authorized_at: str | None
    capture_articles: tuple[FixedCaptureArticle, ...]
    include_phrases: tuple[str, ...]
    exclude_phrases: tuple[str, ...]
    limits: LiveSmokeLimits
    generation_route_ids: tuple[str, ...]
    probe_route_ids: tuple[str, ...]
    embedding_route_id: str
    routes: tuple[LiveSmokeRoute, ...]
    config_hash: str

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> LiveSmokeConfig:
        """Load a JSON config without permitting JSON NaN/Infinity extensions."""

        config_path = Path(path)
        try:
            payload = json.loads(
                config_path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_raise_json_constant(value)),
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LiveSmokeError("live smoke configuration is not readable JSON") from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, value: Any) -> LiveSmokeConfig:
        root = _object(value, "configuration")
        _exact_keys(
            root,
            {
                "schema",
                "verification_id",
                "authorization",
                "capture",
                "interest",
                "limits",
                "plan",
                "routes",
            },
            "configuration",
        )
        if root["schema"] != CONFIG_SCHEMA:
            raise LiveSmokeError(f"configuration schema must be {CONFIG_SCHEMA}")
        verification_id = _uuid_text(root["verification_id"], "verification_id")

        authorization = _object(root["authorization"], "authorization")
        _exact_keys(
            authorization,
            {"live_execution_authorized", "authorized_allowance_usd", "authorized_at"},
            "authorization",
        )
        authorized = authorization["live_execution_authorized"]
        if not isinstance(authorized, bool):
            raise LiveSmokeError("live_execution_authorized must be true or false")
        allowance = _decimal_from_string(
            authorization["authorized_allowance_usd"],
            "authorized_allowance_usd",
            allow_zero=False,
        )
        authorized_at_value = authorization["authorized_at"]
        if authorized:
            authorized_at = _timestamp(authorized_at_value, "authorized_at")
        elif authorized_at_value is None:
            authorized_at = None
        else:
            raise LiveSmokeError("authorized_at must be null while live execution is unauthorized")

        capture = _object(root["capture"], "capture")
        _exact_keys(capture, {"articles"}, "capture")
        article_values = _array(capture["articles"], "capture.articles")
        if not 1 <= len(article_values) <= MAX_CAPTURE_ARTICLES:
            raise LiveSmokeError("capture must list one to three articles")
        articles = tuple(_capture_article(item, index) for index, item in enumerate(article_values))
        _unique((item.capture_id for item in articles), "capture_id")
        _unique((item.url_hash for item in articles), "capture url_hash")

        interest = _object(root["interest"], "interest")
        _exact_keys(interest, {"include_phrases", "exclude_phrases"}, "interest")
        include_phrases = _phrases(interest["include_phrases"], "include_phrases")
        exclude_phrases = _phrases(interest["exclude_phrases"], "exclude_phrases")

        limits_value = _object(root["limits"], "limits")
        _exact_keys(
            limits_value,
            {
                "max_dispatches",
                "reasoning_input_tokens",
                "reasoning_output_tokens",
                "embedding_input_tokens",
                "embedding_batch_articles",
            },
            "limits",
        )
        limits = LiveSmokeLimits(
            max_dispatches=_bounded_int(
                limits_value["max_dispatches"], 1, MAX_BILLABLE_DISPATCHES, "max_dispatches"
            ),
            reasoning_input_tokens=_bounded_int(
                limits_value["reasoning_input_tokens"],
                1,
                MAX_REASONING_INPUT_TOKENS,
                "reasoning_input_tokens",
            ),
            reasoning_output_tokens=_bounded_int(
                limits_value["reasoning_output_tokens"],
                1,
                MAX_REASONING_OUTPUT_TOKENS,
                "reasoning_output_tokens",
            ),
            embedding_input_tokens=_bounded_int(
                limits_value["embedding_input_tokens"],
                1,
                MAX_EMBEDDING_INPUT_TOKENS,
                "embedding_input_tokens",
            ),
            embedding_batch_articles=_bounded_int(
                limits_value["embedding_batch_articles"],
                1,
                min(MAX_CAPTURE_ARTICLES, len(articles)),
                "embedding_batch_articles",
            ),
        )

        routes_value = _array(root["routes"], "routes")
        if not routes_value:
            raise LiveSmokeError("routes must enumerate every paid route")
        routes = tuple(_route(item, index) for index, item in enumerate(routes_value))
        _unique((route.route_id for route in routes), "route_id")
        route_by_id = {route.route_id: route for route in routes}

        plan = _object(root["plan"], "plan")
        _exact_keys(
            plan,
            {"generation_route_ids", "probe_route_ids", "embedding_route_id"},
            "plan",
        )
        generation_route_ids = _id_array(plan["generation_route_ids"], "generation_route_ids")
        if not generation_route_ids:
            raise LiveSmokeError("generation_route_ids must name a primary generation route")
        probe_route_ids = _id_array(plan["probe_route_ids"], "probe_route_ids")
        embedding_route_id = _safe_id(plan["embedding_route_id"], "embedding_route_id")
        planned = (*generation_route_ids, *probe_route_ids, embedding_route_id)
        _unique(planned, "planned route")
        if set(planned) != set(route_by_id):
            raise LiveSmokeError("every paid route must appear exactly once in the finite plan")
        for route_id in generation_route_ids:
            if route_by_id[route_id].role != "reasoning":
                raise LiveSmokeError("generation routes must have the reasoning role")
        for route_id in probe_route_ids:
            if route_by_id[route_id].role != "probe":
                raise LiveSmokeError("probe routes must have the probe role")
        embedding_route = route_by_id[embedding_route_id]
        if embedding_route.role != "embedding" or embedding_route.provider != "openai":
            raise LiveSmokeError("the embedding route must be the explicit OpenAI embedding route")
        if sum(route.max_dispatches for route in routes) > limits.max_dispatches:
            raise LiveSmokeError("route dispatch allocations exceed the one-run dispatch cap")

        canonical = _canonical_json(root)
        return cls(
            verification_id=verification_id,
            live_execution_authorized=authorized,
            authorized_allowance_usd=allowance,
            authorized_at=authorized_at,
            capture_articles=articles,
            include_phrases=include_phrases,
            exclude_phrases=exclude_phrases,
            limits=limits,
            generation_route_ids=generation_route_ids,
            probe_route_ids=probe_route_ids,
            embedding_route_id=embedding_route_id,
            routes=routes,
            config_hash=hashlib.sha256(canonical).hexdigest(),
        )

    @property
    def effective_allowance_usd(self) -> Decimal:
        return min(self.authorized_allowance_usd, MAX_RESERVED_USD)

    @property
    def route_by_id(self) -> dict[str, LiveSmokeRoute]:
        return {route.route_id: route for route in self.routes}

    def require_execution_authorized(self, confirmation: str) -> None:
        if not self.live_execution_authorized:
            raise LiveSmokeError("live execution is not authorized by this configuration")
        if confirmation != self.verification_id:
            raise LiveSmokeError("the confirmation verification ID does not match the configuration")

    def safe_summary(self) -> dict[str, Any]:
        """Return an allowlisted summary with no article URLs, phrases, or credentials."""

        return {
            "schema": CONFIG_SCHEMA,
            "verification_id": self.verification_id,
            "config_hash": self.config_hash,
            "live_execution_authorized": self.live_execution_authorized,
            "authorized_at": self.authorized_at,
            "authorized_allowance_usd": _decimal_text(self.authorized_allowance_usd),
            "effective_allowance_usd": _decimal_text(self.effective_allowance_usd),
            "limits": self.limits.public_dict(),
            "capture": [article.ledger_dict() for article in self.capture_articles],
            "plan": {
                "generation_route_ids": list(self.generation_route_ids),
                "probe_route_ids": list(self.probe_route_ids),
                "embedding_route_id": self.embedding_route_id,
            },
            "routes": [route.public_dict() for route in self.routes],
        }

    def filter_source_items(self, source_id: str, items: Sequence[RSSItem]) -> list[RSSItem]:
        """Keep only explicitly listed URLs for one source and require each listed item once."""

        source_text = _uuid_text(source_id, "source_id")
        expected = {
            article.url_hash: article
            for article in self.capture_articles
            if article.source_id == source_text
        }
        if not expected:
            raise LiveSmokeError("the RSS source is absent from the fixed capture")
        selected: dict[str, RSSItem] = {}
        for item in items:
            item_hash = url_hash(item.url)
            if item_hash not in expected:
                continue
            if normalize_url(item.url) != expected[item_hash].canonical_url:
                raise LiveSmokeError("an RSS item did not match its canonical capture URL")
            if item_hash in selected:
                raise LiveSmokeError("a fixed capture article appeared more than once")
            selected[item_hash] = item
        missing = set(expected).difference(selected)
        if missing:
            raise LiveSmokeError("the RSS feed did not contain every fixed capture article")
        return [
            selected[article.url_hash]
            for article in self.capture_articles
            if article.source_id == source_text
        ]


class FixedCaptureRSSProvider(RSSProvider):
    """Filter a real RSS provider down to the config's exact article identities."""

    def __init__(self, *, config: LiveSmokeConfig, source_id: str, delegate: RSSProvider) -> None:
        self._config = config
        self._source_id = _uuid_text(source_id, "source_id")
        if not any(item.source_id == self._source_id for item in config.capture_articles):
            raise LiveSmokeError("the RSS source is absent from the fixed capture")
        self._delegate = delegate

    def fetch(self, feed_url: str) -> list[RSSItem]:
        return self._config.filter_source_items(self._source_id, self._delegate.fetch(feed_url))


class LiveSmokeLedger:
    """Cross-process, append-only reservation ledger for one verification ID."""

    def __init__(self, path: Path, config: LiveSmokeConfig) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self.config = config

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        config: LiveSmokeConfig,
        *,
        require_existing: bool = False,
    ) -> LiveSmokeLedger:
        ledger = cls(Path(path).expanduser().resolve(), config)
        ledger._initialize_or_validate(require_existing=require_existing)
        return ledger

    @staticmethod
    def canonical_path(
        state_directory: str | os.PathLike[str], verification_id: str
    ) -> Path:
        canonical_id = _uuid_text(verification_id, "verification_id")
        return Path(state_directory).expanduser().resolve() / f"live-smoke-{canonical_id}.json"

    def bind_run(self, personal_run_id: str) -> None:
        """Bind the one ledger to one persisted run before any paid dispatch."""

        run_id = _uuid_text(personal_run_id, "personal_run_id")

        def mutate(payload: dict[str, Any]) -> None:
            existing = payload["personal_run_id"]
            if existing is not None and existing != run_id:
                raise LiveSmokeError("the live smoke ledger is already bound to another run")
            if payload["dispatches"] and existing is None:
                raise LiveSmokeError("an unbound ledger already contains dispatches")
            payload["personal_run_id"] = run_id

        self._mutate(mutate)

    def reserve_dispatch(
        self,
        *,
        route_id: str,
        input_token_bound: int,
        requested_output_tokens: int,
        input_ids: Sequence[str],
    ) -> str:
        route = self._route(route_id)
        input_bound, output_cap = self._validate_request_bounds(
            route, input_token_bound, requested_output_tokens
        )
        clean_ids = tuple(_safe_id(item, "input_id") for item in input_ids)
        if not clean_ids:
            raise LiveSmokeError("a paid dispatch must identify its sanitized inputs")
        if len(clean_ids) > MAX_CAPTURE_ARTICLES:
            raise LiveSmokeError("a paid dispatch carries too many input identities")
        _unique(clean_ids, "input_id")
        reservation = _reservation(route, input_bound, output_cap)
        dispatch_id = str(uuid.uuid4())

        def mutate(payload: dict[str, Any]) -> None:
            if payload["personal_run_id"] is None:
                raise LiveSmokeError("bind the persisted personal run before any paid dispatch")
            if payload["outcome"] is not None:
                raise LiveSmokeError("a terminal live smoke run cannot make another paid dispatch")
            dispatches = payload["dispatches"]
            route_count = sum(1 for item in dispatches if item["route_id"] == route.route_id)
            if route_count >= route.max_dispatches:
                raise LiveSmokeError(f"route {route.route_id} exhausted its dispatch allocation")
            if len(dispatches) >= self.config.limits.max_dispatches:
                raise LiveSmokeError("the live smoke dispatch ceiling is exhausted")
            reserved = _sum_decimals(
                _ledger_decimal(item["reserved_usd"], "reserved_usd")
                for item in dispatches
            )
            if reserved + reservation > self.config.effective_allowance_usd:
                raise LiveSmokeError("the live smoke monetary reservation ceiling would be exceeded")
            now = _utc_now()
            dispatches.append(
                {
                    "dispatch_id": dispatch_id,
                    "sequence": len(dispatches) + 1,
                    "route_id": route.route_id,
                    "role": route.role,
                    "provider": route.provider,
                    "model": route.model,
                    "model_version": route.model_version,
                    "price_source": route.price_source.public_dict(),
                    "input_ids": list(clean_ids),
                    "input_token_bound": input_bound,
                    "requested_output_tokens": output_cap,
                    "reserved_usd": _decimal_text(reservation),
                    "status": "reserved",
                    "reserved_at": now,
                    "response_received_at": None,
                    "reconciled_at": None,
                    "actual_input_tokens": None,
                    "actual_output_tokens": None,
                    "known_actual_usd": None,
                    "uncertain_reason": None,
                }
            )

        self._mutate(mutate)
        return dispatch_id

    def mark_response_received(self, dispatch_id: str) -> None:
        def mutate(payload: dict[str, Any]) -> None:
            item = _dispatch(payload, dispatch_id)
            if item["status"] == "reserved":
                item["status"] = "response_received"
                item["response_received_at"] = _utc_now()

        self._mutate(mutate)

    def reconcile(self, dispatch_id: str, *, input_tokens: int, output_tokens: int) -> None:
        actual_input = _nonnegative_int(input_tokens, "input_tokens")
        actual_output = _nonnegative_int(output_tokens, "output_tokens")
        violation: list[str] = []

        def mutate(payload: dict[str, Any]) -> None:
            item = _dispatch(payload, dispatch_id)
            if item["status"] == "reconciled":
                if (
                    item["actual_input_tokens"] != actual_input
                    or item["actual_output_tokens"] != actual_output
                ):
                    raise LiveSmokeError("a reconciled dispatch cannot be rewritten")
                return
            if actual_input > item["input_token_bound"] or actual_output > item["requested_output_tokens"]:
                item["status"] = "uncertain"
                item["uncertain_reason"] = "provider_usage_exceeded_reserved_bounds"
                violation.append("provider usage exceeded its reserved token bounds")
                return
            route = self._route(item["route_id"])
            actual_cost = _reservation(route, actual_input, actual_output)
            item.update(
                {
                    "status": "reconciled",
                    "response_received_at": item["response_received_at"] or _utc_now(),
                    "reconciled_at": _utc_now(),
                    "actual_input_tokens": actual_input,
                    "actual_output_tokens": actual_output,
                    "known_actual_usd": _decimal_text(actual_cost),
                    "uncertain_reason": None,
                }
            )

        self._mutate(mutate)
        if violation:
            raise LiveSmokeError(violation[0])

    def mark_uncertain(self, dispatch_id: str, reason: str = "transport_outcome_unknown") -> None:
        safe_reason = _safe_id(reason, "uncertain_reason")

        def mutate(payload: dict[str, Any]) -> None:
            item = _dispatch(payload, dispatch_id)
            if item["status"] == "reconciled":
                return
            item["status"] = "uncertain"
            item["uncertain_reason"] = safe_reason

        self._mutate(mutate)

    def record_outcome(
        self,
        *,
        outcome: str,
        capture_ids: Sequence[str],
        personal_run_id: str | None = None,
        snapshot_id: str | None = None,
        report_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        if outcome not in {"succeeded", "failed", "blocked"}:
            raise LiveSmokeError("outcome must be succeeded, failed, or blocked")
        clean_captures = tuple(_safe_id(item, "capture_id") for item in capture_ids)
        configured = {article.capture_id for article in self.config.capture_articles}
        if not set(clean_captures).issubset(configured):
            raise LiveSmokeError("outcome refers to a capture outside the fixed configuration")
        _unique(clean_captures, "capture_id")
        ids = {
            "personal_run_id": _optional_uuid(personal_run_id, "personal_run_id"),
            "snapshot_id": _optional_uuid(snapshot_id, "snapshot_id"),
            "report_id": _optional_uuid(report_id, "report_id"),
        }
        clean_reason = None if reason is None else _safe_id(reason, "outcome reason")

        def mutate(payload: dict[str, Any]) -> None:
            bound_run_id = payload["personal_run_id"]
            if bound_run_id is None:
                raise LiveSmokeError("the live smoke ledger has no bound personal run")
            if ids["personal_run_id"] is None:
                ids["personal_run_id"] = bound_run_id
            elif ids["personal_run_id"] != bound_run_id:
                raise LiveSmokeError("outcome run ID does not match the ledger binding")
            if outcome == "succeeded":
                if set(clean_captures) != configured:
                    raise LiveSmokeError("a successful outcome must record the full fixed capture")
                if ids["snapshot_id"] is None or ids["report_id"] is None:
                    raise LiveSmokeError("a successful outcome requires snapshot and report IDs")
                if not payload["dispatches"] or any(
                    item["status"] != "reconciled" for item in payload["dispatches"]
                ):
                    raise LiveSmokeError(
                        "a successful outcome requires reconciled usage for every dispatch"
                    )
            proposed = {
                "status": outcome,
                "reason": clean_reason,
                "capture_ids": list(clean_captures),
                **ids,
            }
            existing = payload["outcome"]
            if existing is not None and {key: existing[key] for key in proposed} != proposed:
                raise LiveSmokeError("the terminal live smoke outcome cannot be rewritten")
            if existing is None:
                payload["outcome"] = {**proposed, "recorded_at": _utc_now()}

        self._mutate(mutate)

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            return self._read_unlocked()

    def _route(self, route_id: str) -> LiveSmokeRoute:
        try:
            return self.config.route_by_id[route_id]
        except KeyError as exc:
            raise LiveSmokeError("paid route is not enumerated in the live smoke config") from exc

    def _validate_request_bounds(
        self, route: LiveSmokeRoute, input_token_bound: int, requested_output_tokens: int
    ) -> tuple[int, int]:
        if route.role == "embedding":
            input_bound = _bounded_int(
                input_token_bound, 1, self.config.limits.embedding_input_tokens, "input_token_bound"
            )
            output_cap = _bounded_int(requested_output_tokens, 0, 0, "requested_output_tokens")
        else:
            input_bound = _bounded_int(
                input_token_bound, 1, self.config.limits.reasoning_input_tokens, "input_token_bound"
            )
            output_cap = _bounded_int(
                requested_output_tokens,
                1,
                self.config.limits.reasoning_output_tokens,
                "requested_output_tokens",
            )
        return input_bound, output_cap

    def _initialize_or_validate(self, *, require_existing: bool) -> None:
        if not self.config.live_execution_authorized:
            raise LiveSmokeError("an unauthorized configuration cannot open an execution ledger")
        expected_name = f"live-smoke-{self.config.verification_id}.json"
        if self.path.name != expected_name:
            raise LiveSmokeError(f"ledger filename must be {expected_name}")
        if self.path.is_symlink():
            raise LiveSmokeError("live smoke ledger cannot be a symbolic link")
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)
        with self._locked():
            if self.path.exists():
                self._read_unlocked()
                return
            if require_existing:
                raise LiveSmokeError("the bound live smoke ledger is missing on restart")
            now = _utc_now()
            payload = {
                "schema": LEDGER_SCHEMA,
                "verification_id": self.config.verification_id,
                "config_hash": self.config.config_hash,
                "created_at": now,
                "updated_at": now,
                "personal_run_id": None,
                "configuration": self.config.safe_summary(),
                "dispatches": [],
                "totals": {
                    "dispatches": 0,
                    "reserved_usd": "0",
                    "known_actual_usd": "0",
                    "held_uncertain_usd": "0",
                },
                "outcome": None,
            }
            self._write_unlocked(payload)

    def _mutate(self, operation: Callable[[dict[str, Any]], None]) -> None:
        with self._locked():
            payload = self._read_unlocked()
            operation(payload)
            payload["updated_at"] = _utc_now()
            payload["totals"] = _totals(payload["dispatches"])
            self._write_unlocked(payload)

    @contextlib.contextmanager
    def _locked(self):
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
            payload = json.loads(raw, parse_constant=lambda value: _raise_json_constant(value))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LiveSmokeError("live smoke ledger is unreadable") from exc
        if not isinstance(payload, dict):
            raise LiveSmokeError("live smoke ledger must be a JSON object")
        if payload.get("schema") != LEDGER_SCHEMA:
            raise LiveSmokeError("live smoke ledger schema does not match")
        if payload.get("verification_id") != self.config.verification_id:
            raise LiveSmokeError("live smoke ledger verification ID does not match")
        if payload.get("config_hash") != self.config.config_hash:
            raise LiveSmokeError("live smoke ledger configuration hash does not match")
        expected_hash = payload.get("ledger_hash")
        unhashed = dict(payload)
        unhashed.pop("ledger_hash", None)
        if (
            not isinstance(expected_hash, str)
            or not _HEX_64.fullmatch(expected_hash)
            or hashlib.sha256(_canonical_json(unhashed)).hexdigest() != expected_hash
        ):
            raise LiveSmokeError("live smoke ledger content hash does not match")
        if payload.get("personal_run_id") is not None:
            _uuid_text(payload["personal_run_id"], "personal_run_id")
        if not isinstance(payload.get("dispatches"), list):
            raise LiveSmokeError("live smoke ledger dispatches are malformed")
        return payload

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        materialized = dict(payload)
        materialized.pop("ledger_hash", None)
        materialized["ledger_hash"] = hashlib.sha256(_canonical_json(materialized)).hexdigest()
        data = _canonical_json(materialized) + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise


class GuardedHTTPClient:
    """Reserve and count one physical paid HTTP POST before delegating it."""

    def __init__(
        self,
        *,
        config: LiveSmokeConfig,
        ledger: LiveSmokeLedger,
        route_id: str,
        delegate: Any,
        input_ids: Sequence[str],
    ) -> None:
        _require_matching_ledger_config(config, ledger)
        self.config = config
        self.ledger = ledger
        self.route = ledger._route(route_id)
        self.delegate = delegate
        self.input_ids = tuple(_safe_id(item, "input_id") for item in input_ids)
        if not self.input_ids:
            raise LiveSmokeError("guarded paid clients require sanitized input identities")
        self._local = threading.local()

    @property
    def last_dispatch_id(self) -> str | None:
        return getattr(self._local, "last_dispatch_id", None)

    @property
    def last_usage_complete(self) -> bool:
        return bool(getattr(self._local, "last_usage_complete", False))

    def post(self, *args: Any, **kwargs: Any) -> Any:
        payload = _object(kwargs.get("json"), "paid request JSON")
        if payload.get("model") != self.route.model:
            raise LiveSmokeError("paid request model does not match the enumerated route")
        output_cap = _requested_output_cap(payload, role=self.route.role)
        _assert_single_generation(payload, role=self.route.role)
        input_bound = conservative_payload_token_bound(payload)
        if self.route.role == "embedding":
            inputs = _array(payload.get("input"), "embedding input")
            if not 1 <= len(inputs) <= self.config.limits.embedding_batch_articles:
                raise LiveSmokeError("embedding request exceeds the configured article batch")
            if not all(isinstance(item, str) and item for item in inputs):
                raise LiveSmokeError("embedding inputs must be non-empty strings")
        dispatch_id = self.ledger.reserve_dispatch(
            route_id=self.route.route_id,
            input_token_bound=input_bound,
            requested_output_tokens=output_cap,
            input_ids=self.input_ids,
        )
        self._local.last_dispatch_id = dispatch_id
        self._local.last_usage_complete = False
        try:
            response = self.delegate.post(*args, **kwargs)
        except BaseException:
            self.ledger.mark_uncertain(dispatch_id)
            raise
        self.ledger.mark_response_received(dispatch_id)
        usage = _response_usage(response, self.route.provider, self.route.role)
        if usage is not None:
            self.ledger.reconcile(
                dispatch_id, input_tokens=usage[0], output_tokens=usage[1]
            )
            self._local.last_usage_complete = True
        else:
            self.ledger.mark_uncertain(dispatch_id, "provider_response_usage_unknown")
        return response

    def close(self) -> None:
        close = getattr(self.delegate, "close", None)
        if callable(close):
            close()


class GuardedLLMProvider(LLMProviderAdapter):
    """Reconcile normalized LLM usage while preserving the provider interface."""

    def __init__(
        self,
        *,
        delegate: LLMProviderAdapter,
        http_client: GuardedHTTPClient,
    ) -> None:
        route = http_client.route
        if route.role not in {"reasoning", "probe"}:
            raise LiveSmokeError("an LLM provider requires a reasoning or probe route")
        if (
            delegate.provider_name != route.provider
            or delegate.model_name != route.model
            or delegate.model_version != route.model_version
        ):
            raise LiveSmokeError("LLM provider identity does not match its paid route")
        self._delegate = delegate
        self._http_client = http_client
        self.provider_name = delegate.provider_name
        self.model_name = delegate.model_name
        self.model_version = delegate.model_version

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return self._delegate.supports_mode(mode)

    def supports_structured_schema(self, schema_name: str) -> bool:
        return self._delegate.supports_structured_schema(schema_name)

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        before = self._http_client.last_dispatch_id
        try:
            response = self._delegate.invoke(request)
        except BaseException as exc:
            dispatch_id = self._http_client.last_dispatch_id
            billed_response = getattr(exc, "response", None)
            if dispatch_id is not None and dispatch_id != before:
                if self._http_client.last_usage_complete and isinstance(
                    billed_response, LLMInvocationResponse
                ):
                    self._http_client.ledger.reconcile(
                        dispatch_id,
                        input_tokens=billed_response.input_tokens,
                        output_tokens=billed_response.output_tokens,
                    )
                elif not self._http_client.last_usage_complete:
                    self._http_client.ledger.mark_uncertain(
                        dispatch_id, "provider_response_usage_unknown"
                    )
            raise
        dispatch_id = self._http_client.last_dispatch_id
        if dispatch_id is None or dispatch_id == before:
            raise LiveSmokeError("LLM provider returned without a counted physical dispatch")
        if not self._http_client.last_usage_complete:
            self._http_client.ledger.mark_uncertain(
                dispatch_id, "provider_response_usage_unknown"
            )
            raise LiveSmokeError("physical provider response did not supply complete token accounting")
        self._http_client.ledger.reconcile(
            dispatch_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        return response

    def invoke_batch(
        self, requests: Sequence[LLMInvocationRequest]
    ) -> Sequence[LLMInvocationResponse]:
        del requests
        raise LiveSmokeError("the bounded live smoke harness does not permit batch LLM jobs")

    def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def build_guarded_openai_embedding_provider(
    *,
    config: LiveSmokeConfig,
    ledger: LiveSmokeLedger,
    route_id: str,
    api_key: str,
    input_ids: Sequence[str],
    dimension: int = OPENAI_EMBEDDING_DIMENSIONS,
    base_url: str = "https://api.openai.com",
    timeout: float = 60.0,
    client: Any | None = None,
) -> OpenAIEmbeddingProvider:
    """Build the production adapter with its internal retry loop forced to one attempt."""

    _require_matching_ledger_config(config, ledger)
    route = ledger._route(route_id)
    if route.route_id != config.embedding_route_id or route.role != "embedding":
        raise LiveSmokeError("route is not the configured embedding route")
    raw_client = client or httpx.Client(base_url=base_url, timeout=timeout)
    guarded_client = GuardedHTTPClient(
        config=config,
        ledger=ledger,
        route_id=route_id,
        delegate=raw_client,
        input_ids=input_ids,
    )
    return OpenAIEmbeddingProvider(
        api_key=api_key,
        model_name=route.model,
        model_version=route.model_version,
        dimension=dimension,
        batch_size=config.limits.embedding_batch_articles,
        max_attempts=1,
        client=guarded_client,
    )


def conservative_payload_token_bound(payload: Mapping[str, Any]) -> int:
    """Bound tokens from the full actual JSON body, including system/schema/context content.

    Supported provider tokenizers encode text to byte-backed pieces, so the UTF-8 byte count is
    already conservative.  The fixed addition covers request framing outside the JSON value.
    """

    return len(_canonical_json(payload)) + _PAYLOAD_PROTOCOL_OVERHEAD_TOKENS


def _require_matching_ledger_config(
    config: LiveSmokeConfig, ledger: LiveSmokeLedger
) -> None:
    if (
        config.verification_id != ledger.config.verification_id
        or config.config_hash != ledger.config.config_hash
    ):
        raise LiveSmokeError("live smoke config identity does not match the durable ledger")


def _requested_output_cap(payload: Mapping[str, Any], *, role: str) -> int:
    values: list[tuple[str, Any]] = []
    for name in (
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "max_completion_token_count",
        "maxOutputTokens",
    ):
        if name in payload:
            values.append((name, payload[name]))
    for container_name in ("generation_config", "generationConfig"):
        container = payload.get(container_name)
        if isinstance(container, Mapping):
            for name in ("max_output_tokens", "maxOutputTokens"):
                if name in container:
                    values.append((f"{container_name}.{name}", container[name]))
    if role == "embedding":
        if values:
            raise LiveSmokeError("an embedding request cannot reserve generated output")
        return 0
    if not values:
        raise LiveSmokeError("paid reasoning request has no explicit output token cap")
    caps = {_bounded_int(value, 1, MAX_REASONING_OUTPUT_TOKENS, name) for name, value in values}
    if len(caps) != 1:
        raise LiveSmokeError("paid request carries conflicting output token caps")
    return caps.pop()


def _assert_single_generation(payload: Mapping[str, Any], *, role: str) -> None:
    if role == "embedding":
        return
    multiplicities: list[tuple[str, Any]] = []
    for name in ("n", "candidate_count", "candidateCount", "num_return_sequences", "best_of", "bestOf"):
        if name in payload:
            multiplicities.append((name, payload[name]))
    for container_name in ("generation_config", "generationConfig"):
        container = payload.get(container_name)
        if isinstance(container, Mapping):
            for name in ("candidate_count", "candidateCount"):
                if name in container:
                    multiplicities.append((f"{container_name}.{name}", container[name]))
    for name, value in multiplicities:
        if _bounded_int(value, 1, 1, name) != 1:
            raise LiveSmokeError("paid reasoning requests must generate exactly one result")


def _response_usage(response: Any, provider: str, role: str) -> tuple[int, int] | None:
    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(body, Mapping) or not isinstance(body.get("usage"), Mapping):
        return None
    usage = body["usage"]
    if role == "embedding":
        pair = (usage.get("prompt_tokens"), 0)
    elif provider == "anthropic":
        pair = (usage.get("input_tokens"), usage.get("output_tokens"))
    elif provider == "gemini":
        output = _usage_int(usage.get("total_output_tokens"))
        thought = _usage_int(usage.get("total_thought_tokens"))
        if output is None or thought is None:
            return None
        pair = (usage.get("total_input_tokens"), output + thought)
    else:
        pair = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
    input_tokens = _usage_int(pair[0])
    output_tokens = _usage_int(pair[1])
    if input_tokens is None or output_tokens is None:
        return None
    if input_tokens == 0:
        return None
    return input_tokens, output_tokens


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _route(value: Any, index: int) -> LiveSmokeRoute:
    item = _object(value, f"routes[{index}]")
    _exact_keys(
        item,
        {
            "route_id",
            "role",
            "provider",
            "model",
            "model_version",
            "max_dispatches",
            "input_usd_per_million_tokens",
            "output_usd_per_million_tokens",
            "price_source",
        },
        f"routes[{index}]",
    )
    role = _text(item["role"], "route role", limit=20)
    if role not in _ROLES:
        raise LiveSmokeError("route role must be reasoning, probe, or embedding")
    provider = _text(item["provider"], "route provider", limit=40).lower()
    if role == "embedding":
        if provider != "openai":
            raise LiveSmokeError("only the production OpenAI embedding adapter is supported")
    elif provider not in _LLM_PROVIDERS:
        raise LiveSmokeError("reasoning/probe provider is unsupported by the live harness")
    input_price = _decimal_from_string(
        item["input_usd_per_million_tokens"],
        "input_usd_per_million_tokens",
        allow_zero=False,
    )
    output_price = _decimal_from_string(
        item["output_usd_per_million_tokens"],
        "output_usd_per_million_tokens",
        allow_zero=role == "embedding",
    )
    if role == "embedding" and output_price != 0:
        raise LiveSmokeError("embedding output price must be exactly zero")
    price_source_value = _object(item["price_source"], "price_source")
    _exact_keys(price_source_value, {"url", "version", "retrieved_at"}, "price_source")
    source_url = _https_url(price_source_value["url"], "price_source.url")
    return LiveSmokeRoute(
        route_id=_safe_id(item["route_id"], "route_id"),
        role=role,
        provider=provider,
        model=_text(item["model"], "model", limit=120),
        model_version=_text(item["model_version"], "model_version", limit=120),
        max_dispatches=_bounded_int(
            item["max_dispatches"], 1, MAX_BILLABLE_DISPATCHES, "route max_dispatches"
        ),
        input_usd_per_million_tokens=input_price,
        output_usd_per_million_tokens=output_price,
        price_source=PriceSource(
            url=source_url,
            version=_text(price_source_value["version"], "price_source.version", limit=120),
            retrieved_at=_timestamp(price_source_value["retrieved_at"], "price_source.retrieved_at"),
        ),
    )


def _capture_article(value: Any, index: int) -> FixedCaptureArticle:
    item = _object(value, f"capture.articles[{index}]")
    _exact_keys(
        item,
        {"capture_id", "source_id", "canonical_url", "url_hash"},
        f"capture.articles[{index}]",
    )
    canonical = _http_url(item["canonical_url"], "canonical_url")
    if normalize_url(canonical) != canonical:
        raise LiveSmokeError("canonical_url must already be normalized")
    expected_hash = url_hash(canonical)
    supplied_hash = _text(item["url_hash"], "url_hash", limit=64)
    if not _HEX_64.fullmatch(supplied_hash) or supplied_hash != expected_hash:
        raise LiveSmokeError("url_hash must be the SHA-256 of canonical_url")
    return FixedCaptureArticle(
        capture_id=_safe_id(item["capture_id"], "capture_id"),
        source_id=_uuid_text(item["source_id"], "source_id"),
        canonical_url=canonical,
        url_hash=supplied_hash,
    )


def _reservation(route: LiveSmokeRoute, input_tokens: int, output_tokens: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return (
            Decimal(input_tokens) * route.input_usd_per_million_tokens
            + Decimal(output_tokens) * route.output_usd_per_million_tokens
        ) / Decimal("1000000")


def _totals(dispatches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reserved = _sum_decimals(
        _ledger_decimal(item["reserved_usd"], "reserved_usd") for item in dispatches
    )
    actual = _sum_decimals(
        _ledger_decimal(item["known_actual_usd"], "known_actual_usd")
        for item in dispatches
        if item.get("known_actual_usd") is not None
    )
    held = _sum_decimals(
        _ledger_decimal(item["reserved_usd"], "reserved_usd")
        for item in dispatches
        if item.get("status") != "reconciled"
    )
    return {
        "dispatches": len(dispatches),
        "reserved_usd": _decimal_text(reserved),
        "known_actual_usd": _decimal_text(actual),
        "held_uncertain_usd": _decimal_text(held),
    }


def _dispatch(payload: Mapping[str, Any], dispatch_id: str) -> dict[str, Any]:
    dispatch_text = _uuid_text(dispatch_id, "dispatch_id")
    for item in payload["dispatches"]:
        if item.get("dispatch_id") == dispatch_text:
            return item
    raise LiveSmokeError("dispatch ID is absent from the live smoke ledger")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LiveSmokeError("value is not finite canonical JSON") from exc


def _decimal_from_string(value: Any, label: str, *, allow_zero: bool) -> Decimal:
    if not isinstance(value, str) or not _BOUNDED_DECIMAL.fullmatch(value):
        raise LiveSmokeError(f"{label} must be a bounded plain decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise LiveSmokeError(f"{label} must be a finite decimal string") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result <= 0):
        raise LiveSmokeError(f"{label} must be a finite {'non-negative' if allow_zero else 'positive'} decimal")
    return result


def _ledger_decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise LiveSmokeError(f"{label} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise LiveSmokeError(f"{label} must be a finite decimal string") from exc
    if not result.is_finite() or result < 0:
        raise LiveSmokeError(f"{label} must be a finite non-negative decimal")
    return result


def _sum_decimals(values: Any) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return sum(values, Decimal("0"))


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiveSmokeError(f"{label} must be an object")
    return dict(value)


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise LiveSmokeError(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        detail = f"missing={missing}, extra={extra}"
        raise LiveSmokeError(f"{label} has unexpected fields ({detail})")


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LiveSmokeError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LiveSmokeError(f"{label} must be a non-negative integer")
    return value


def _text(value: Any, label: str, *, limit: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > limit:
        raise LiveSmokeError(f"{label} must be a non-empty trimmed string up to {limit} characters")
    if any(ord(character) < 32 for character in value):
        raise LiveSmokeError(f"{label} contains a control character")
    return value


def _safe_id(value: Any, label: str) -> str:
    text = _text(value, label, limit=80)
    if not _SAFE_ID.fullmatch(text):
        raise LiveSmokeError(f"{label} must contain only safe identifier characters")
    return text


def _uuid_text(value: Any, label: str) -> str:
    text = _text(value, label, limit=36)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise LiveSmokeError(f"{label} must be a UUID") from exc
    if str(parsed) != text.lower():
        raise LiveSmokeError(f"{label} must use canonical UUID text")
    return str(parsed)


def _optional_uuid(value: Any, label: str) -> str | None:
    return None if value is None else _uuid_text(value, label)


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label, limit=40)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveSmokeError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveSmokeError(f"{label} must include a timezone")
    return text


def _http_url(value: Any, label: str) -> str:
    text = _text(value, label, limit=2048)
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise LiveSmokeError(f"{label} must be an http(s) URL without credentials")
    return text


def _https_url(value: Any, label: str) -> str:
    text = _http_url(value, label)
    if urlsplit(text).scheme != "https":
        raise LiveSmokeError(f"{label} must use https")
    return text


def _phrases(value: Any, label: str) -> tuple[str, ...]:
    items = _array(value, label)
    if len(items) > 20:
        raise LiveSmokeError(f"{label} cannot contain more than 20 phrases")
    phrases = tuple(_text(item, label, limit=160) for item in items)
    _unique((item.casefold() for item in phrases), label)
    return phrases


def _id_array(value: Any, label: str) -> tuple[str, ...]:
    items = _array(value, label)
    result = tuple(_safe_id(item, label) for item in items)
    _unique(result, label)
    return result


def _unique(values: Sequence[str] | Any, label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise LiveSmokeError(f"{label} values must be unique")


def _raise_json_constant(value: str) -> Any:
    raise LiveSmokeError(f"non-finite JSON constant {value} is forbidden")


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONFIG_SCHEMA",
    "LEDGER_SCHEMA",
    "FixedCaptureArticle",
    "FixedCaptureRSSProvider",
    "GuardedHTTPClient",
    "GuardedLLMProvider",
    "LiveSmokeConfig",
    "LiveSmokeError",
    "LiveSmokeLedger",
    "LiveSmokeLimits",
    "LiveSmokeRoute",
    "PriceSource",
    "build_guarded_openai_embedding_provider",
    "conservative_payload_token_bound",
]
