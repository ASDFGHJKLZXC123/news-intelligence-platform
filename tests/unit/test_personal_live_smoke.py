"""Safety tests for the bounded Phase 2 live verification harness.

Every provider and RSS dependency in this module is fake.  These tests never make a network call.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.providers.base import RSSItem
from packages.providers.openai_embeddings import EmbeddingProviderError
from services.ingestion.normalize import url_hash
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
)
from services.personal.live_smoke import (
    CONFIG_SCHEMA,
    FixedCaptureRSSProvider,
    GuardedHTTPClient,
    GuardedLLMProvider,
    LiveSmokeConfig,
    LiveSmokeError,
    LiveSmokeLedger,
    build_guarded_openai_embedding_provider,
)

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "run-personal-phase2-live-smoke.py"
VERIFICATION_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
SOURCE_ID = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "44444444-4444-4444-8444-444444444444"
REPORT_ID = "55555555-5555-4555-8555-555555555555"
ARTICLE_A = "https://news.example.test/a"
ARTICLE_B = "https://news.example.test/b"


def _config_payload(*, authorized: bool = True) -> dict[str, Any]:
    return {
        "schema": CONFIG_SCHEMA,
        "verification_id": VERIFICATION_ID,
        "authorization": {
            "live_execution_authorized": authorized,
            "authorized_allowance_usd": "0.05",
            "authorized_at": "2026-09-09T12:00:00-07:00" if authorized else None,
        },
        "capture": {
            "articles": [
                {
                    "capture_id": "rss-a",
                    "source_id": SOURCE_ID,
                    "canonical_url": ARTICLE_A,
                    "url_hash": url_hash(ARTICLE_A),
                },
                {
                    "capture_id": "rss-b",
                    "source_id": SOURCE_ID,
                    "canonical_url": ARTICLE_B,
                    "url_hash": url_hash(ARTICLE_B),
                },
            ]
        },
        "interest": {
            "include_phrases": ["harbor disruption"],
            "exclude_phrases": ["sports"],
        },
        "limits": {
            "max_dispatches": 8,
            "reasoning_input_tokens": 8192,
            "reasoning_output_tokens": 4096,
            "embedding_input_tokens": 8192,
            "embedding_batch_articles": 2,
        },
        "plan": {
            "generation_route_ids": ["generation-primary"],
            "probe_route_ids": ["identity-probe"],
            "embedding_route_id": "embedding-primary",
        },
        "routes": [
            {
                "route_id": "generation-primary",
                "role": "reasoning",
                "provider": "openai",
                "model": "configured-generation-model",
                "model_version": "2026-09-01",
                "max_dispatches": 5,
                "input_usd_per_million_tokens": "1.25",
                "output_usd_per_million_tokens": "5.00",
                "price_source": {
                    "url": "https://provider.example.test/pricing",
                    "version": "retrieved-2026-09-09",
                    "retrieved_at": "2026-09-09T11:30:00-07:00",
                },
            },
            {
                "route_id": "identity-probe",
                "role": "probe",
                "provider": "anthropic",
                "model": "configured-probe-model",
                "model_version": "2026-08-01",
                "max_dispatches": 1,
                "input_usd_per_million_tokens": "2.00",
                "output_usd_per_million_tokens": "8.00",
                "price_source": {
                    "url": "https://provider.example.test/pricing",
                    "version": "retrieved-2026-09-09",
                    "retrieved_at": "2026-09-09T11:30:00-07:00",
                },
            },
            {
                "route_id": "embedding-primary",
                "role": "embedding",
                "provider": "openai",
                "model": "configured-embedding-model",
                "model_version": "2026-09-01",
                "max_dispatches": 2,
                "input_usd_per_million_tokens": "0.02",
                "output_usd_per_million_tokens": "0",
                "price_source": {
                    "url": "https://provider.example.test/pricing",
                    "version": "retrieved-2026-09-09",
                    "retrieved_at": "2026-09-09T11:30:00-07:00",
                },
            },
        ],
    }


def _config(*, authorized: bool = True) -> LiveSmokeConfig:
    return LiveSmokeConfig.from_mapping(_config_payload(authorized=authorized))


def _ledger(tmp_path: Path, config: LiveSmokeConfig | None = None) -> LiveSmokeLedger:
    selected = config or _config()
    path = LiveSmokeLedger.canonical_path(tmp_path / "private-state", selected.verification_id)
    ledger = LiveSmokeLedger.open(path, selected)
    ledger.bind_run(RUN_ID)
    return ledger


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _Client:
    def __init__(self, response: Any | None = None, error: BaseException | None = None) -> None:
        self.response = response or _Response({"usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False

    def post(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.closed = True


def _request_payload(output_field: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "configured-generation-model",
        "messages": [{"role": "user", "content": "full prompt"}],
        "response_format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    if "." in output_field:
        container, field = output_field.split(".", maxsplit=1)
        payload[container] = {field: 128}
    else:
        payload[output_field] = 128
    return payload


def test_config_is_strict_secret_free_and_uses_decimal_strings(tmp_path: Path) -> None:
    config = _config()
    summary = config.safe_summary()
    rendered = json.dumps(summary, sort_keys=True)

    assert config.effective_allowance_usd.as_tuple().exponent == -2
    assert config.config_hash == LiveSmokeConfig.from_mapping(_config_payload()).config_hash
    assert ARTICLE_A not in rendered
    assert "harbor disruption" not in rendered
    assert summary["capture"][0] == {
        "capture_id": "rss-a",
        "source_id": SOURCE_ID,
        "url_hash": url_hash(ARTICLE_A),
    }

    invalid = _config_payload()
    invalid["authorization"]["authorized_allowance_usd"] = float("nan")
    with pytest.raises(LiveSmokeError, match="decimal string"):
        LiveSmokeConfig.from_mapping(invalid)

    invalid = _config_payload()
    invalid["routes"][0]["input_usd_per_million_tokens"] = "1." + "0" * 30 + "1"
    with pytest.raises(LiveSmokeError, match="bounded plain decimal"):
        LiveSmokeConfig.from_mapping(invalid)

    invalid = _config_payload()
    invalid["provider_api_key"] = "must-never-be-accepted"
    with pytest.raises(LiveSmokeError, match="unexpected fields"):
        LiveSmokeConfig.from_mapping(invalid)

    path = tmp_path / "nan.json"
    path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(LiveSmokeError, match="non-finite"):
        LiveSmokeConfig.from_file(path)

    accepts_all_selected_feeds = _config_payload()
    accepts_all_selected_feeds["interest"]["include_phrases"] = []
    assert LiveSmokeConfig.from_mapping(accepts_all_selected_feeds).include_phrases == ()


def test_config_rejects_wrong_capture_hash_unknown_and_unbounded_routes() -> None:
    invalid = _config_payload()
    invalid["capture"]["articles"][0]["url_hash"] = "0" * 64
    with pytest.raises(LiveSmokeError, match="SHA-256"):
        LiveSmokeConfig.from_mapping(invalid)

    invalid = _config_payload()
    invalid["routes"].append(
        {
            **invalid["routes"][0],
            "route_id": "unplanned-fallback",
            "model": "other-model",
        }
    )
    with pytest.raises(LiveSmokeError, match="exactly once"):
        LiveSmokeConfig.from_mapping(invalid)

    invalid = _config_payload()
    invalid["limits"]["max_dispatches"] = 7
    with pytest.raises(LiveSmokeError, match="allocations exceed"):
        LiveSmokeConfig.from_mapping(invalid)


def test_fixed_capture_filters_unlisted_items_and_requires_every_listed_url() -> None:
    published = None
    unlisted = RSSItem(guid="x", title="x", url="https://news.example.test/x", published_at=published)
    article_b = RSSItem(guid="b", title="b", url=ARTICLE_B, published_at=published)
    article_a = RSSItem(guid="a", title="a", url=ARTICLE_A, published_at=published)

    provider = FixedCaptureRSSProvider(
        config=_config(), source_id=SOURCE_ID, delegate=_RSS([unlisted, article_b, article_a])
    )
    assert [item.guid for item in provider.fetch("https://feed.example.test/rss")] == ["a", "b"]

    missing = FixedCaptureRSSProvider(
        config=_config(), source_id=SOURCE_ID, delegate=_RSS([article_a])
    )
    with pytest.raises(LiveSmokeError, match="every fixed capture"):
        missing.fetch("https://feed.example.test/rss")


class _RSS:
    def __init__(self, items: list[RSSItem]) -> None:
        self.items = items

    def fetch(self, feed_url: str) -> list[RSSItem]:
        del feed_url
        return list(self.items)


@pytest.mark.parametrize(
    "output_field",
    [
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "generation_config.max_output_tokens",
        "generationConfig.maxOutputTokens",
    ],
)
def test_guard_counts_every_physical_post_and_all_supported_output_caps(
    tmp_path: Path, output_field: str
) -> None:
    ledger = _ledger(tmp_path)
    delegate = _Client()
    client = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=delegate,
        input_ids=["rss-a", "rss-b"],
    )

    client.post("/paid", json=_request_payload(output_field))

    snapshot = ledger.snapshot()
    assert len(delegate.calls) == 1
    assert snapshot["totals"]["dispatches"] == 1
    assert snapshot["dispatches"][0]["status"] == "reconciled"
    assert snapshot["dispatches"][0]["actual_input_tokens"] == 12
    assert snapshot["dispatches"][0]["actual_output_tokens"] == 3


def test_oversize_unknown_model_and_monetary_cap_block_before_delegate(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    delegate = _Client()
    client = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=delegate,
        input_ids=["rss-a"],
    )
    oversized = _request_payload("max_completion_tokens")
    oversized["messages"][0]["content"] = "x" * 9000
    with pytest.raises(LiveSmokeError, match="input_token_bound"):
        client.post("/paid", json=oversized)

    wrong_model = _request_payload("max_completion_tokens")
    wrong_model["model"] = "unknown-model"
    with pytest.raises(LiveSmokeError, match="model"):
        client.post("/paid", json=wrong_model)
    assert delegate.calls == []

    multiple = _request_payload("max_completion_tokens")
    multiple["n"] = 8
    with pytest.raises(LiveSmokeError, match="integer from 1 through 1"):
        client.post("/paid", json=multiple)
    assert delegate.calls == []
    assert ledger.snapshot()["dispatches"] == []

    costly = _config_payload()
    costly["authorization"]["authorized_allowance_usd"] = "0.000001"
    costly["routes"][0]["input_usd_per_million_tokens"] = "1000"
    costly_config = LiveSmokeConfig.from_mapping(costly)
    costly_ledger = _ledger(tmp_path / "costly", costly_config)
    costly_client = GuardedHTTPClient(
        config=costly_config,
        ledger=costly_ledger,
        route_id="generation-primary",
        delegate=delegate,
        input_ids=["rss-a"],
    )
    with pytest.raises(LiveSmokeError, match="monetary"):
        costly_client.post("/paid", json=_request_payload("max_completion_tokens"))
    assert delegate.calls == []


def test_guard_rejects_config_and_ledger_identity_mix_before_transport(tmp_path: Path) -> None:
    ledger_config = _config()
    ledger = _ledger(tmp_path, ledger_config)
    different_payload = _config_payload()
    different_payload["authorization"]["authorized_allowance_usd"] = "0.04"
    different_config = LiveSmokeConfig.from_mapping(different_payload)
    delegate = _Client()

    with pytest.raises(LiveSmokeError, match="config identity"):
        GuardedHTTPClient(
            config=different_config,
            ledger=ledger,
            route_id="generation-primary",
            delegate=delegate,
            input_ids=["rss-a"],
        )
    with pytest.raises(LiveSmokeError, match="config identity"):
        build_guarded_openai_embedding_provider(
            config=different_config,
            ledger=ledger,
            route_id="embedding-primary",
            api_key="fake-test-key",
            input_ids=["rss-a"],
            dimension=2,
            client=delegate,
        )
    assert delegate.calls == []


def test_uncertain_reservation_survives_restart_and_retry_costs_again(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=_Client(error=httpx.TransportError("fake transport break")),
        input_ids=["rss-a"],
    )
    with pytest.raises(httpx.TransportError):
        client.post("/paid", json=_request_payload("max_completion_tokens"))

    reopened = LiveSmokeLedger.open(ledger.path, ledger.config, require_existing=True)
    assert reopened.snapshot()["totals"]["dispatches"] == 1
    assert reopened.snapshot()["totals"]["held_uncertain_usd"] != "0"

    retry = GuardedHTTPClient(
        config=ledger.config,
        ledger=reopened,
        route_id="generation-primary",
        delegate=_Client(),
        input_ids=["rss-a"],
    )
    retry.post("/paid", json=_request_payload("max_completion_tokens"))
    snapshot = reopened.snapshot()
    assert snapshot["totals"]["dispatches"] == 2
    assert [item["status"] for item in snapshot["dispatches"]] == ["uncertain", "reconciled"]


def test_missing_usage_stays_held_and_cannot_be_reported_successful(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=_Client(response=_Response({"result": "synthetic"})),
        input_ids=["rss-a"],
    )
    client.post("/paid", json=_request_payload("max_completion_tokens"))
    snapshot = ledger.snapshot()
    assert snapshot["dispatches"][0]["status"] == "uncertain"
    assert snapshot["totals"]["held_uncertain_usd"] == snapshot["totals"]["reserved_usd"]
    with pytest.raises(LiveSmokeError, match="reconciled usage"):
        ledger.record_outcome(
            outcome="succeeded",
            capture_ids=["rss-a", "rss-b"],
            snapshot_id=SNAPSHOT_ID,
            report_id=REPORT_ID,
        )


def test_concurrent_ledger_instances_serialize_unique_reservations(tmp_path: Path) -> None:
    first = _ledger(tmp_path)
    second = LiveSmokeLedger.open(first.path, first.config, require_existing=True)

    def reserve(ledger: LiveSmokeLedger) -> str:
        return ledger.reserve_dispatch(
            route_id="generation-primary",
            input_token_bound=100,
            requested_output_tokens=10,
            input_ids=["rss-a"],
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(reserve, [first, second, first, second]))

    snapshot = first.snapshot()
    assert len(set(identifiers)) == 4
    assert snapshot["totals"]["dispatches"] == 4
    assert snapshot["totals"]["held_uncertain_usd"] == snapshot["totals"]["reserved_usd"]


def test_ledger_is_private_hashed_bound_and_restart_cannot_reinitialize(tmp_path: Path) -> None:
    existing_parent = tmp_path / "shared-existing"
    existing_parent.mkdir(mode=0o755)
    os.chmod(existing_parent, 0o755)
    config = _config()
    path = LiveSmokeLedger.canonical_path(existing_parent, config.verification_id)
    ledger = LiveSmokeLedger.open(path, config)
    assert stat.S_IMODE(existing_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ledger.bind_run(RUN_ID)
    with pytest.raises(LiveSmokeError, match="another run"):
        ledger.bind_run(REPORT_ID)

    missing = LiveSmokeLedger.canonical_path(tmp_path / "missing", config.verification_id)
    with pytest.raises(LiveSmokeError, match="missing on restart"):
        LiveSmokeLedger.open(missing, config, require_existing=True)

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["personal_run_id"] = REPORT_ID
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(LiveSmokeError, match="content hash"):
        LiveSmokeLedger.open(path, config, require_existing=True)

    unauthorized = _config(authorized=False)
    unauthorized_path = LiveSmokeLedger.canonical_path(
        tmp_path / "unauthorized", unauthorized.verification_id
    )
    with pytest.raises(LiveSmokeError, match="unauthorized"):
        LiveSmokeLedger.open(unauthorized_path, unauthorized)
    assert not unauthorized_path.parent.exists()


def test_success_outcome_requires_exact_capture_and_immutable_report_ids(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    dispatch_id = ledger.reserve_dispatch(
        route_id="generation-primary",
        input_token_bound=100,
        requested_output_tokens=10,
        input_ids=["rss-a", "rss-b"],
    )
    ledger.reconcile(dispatch_id, input_tokens=90, output_tokens=8)
    with pytest.raises(LiveSmokeError, match="full fixed capture"):
        ledger.record_outcome(
            outcome="succeeded",
            capture_ids=["rss-a"],
            snapshot_id=SNAPSHOT_ID,
            report_id=REPORT_ID,
        )
    ledger.record_outcome(
        outcome="succeeded",
        capture_ids=["rss-a", "rss-b"],
        snapshot_id=SNAPSHOT_ID,
        report_id=REPORT_ID,
    )
    assert ledger.snapshot()["outcome"]["personal_run_id"] == RUN_ID
    terminal_client = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=_Client(),
        input_ids=["rss-a"],
    )
    with pytest.raises(LiveSmokeError, match="terminal"):
        terminal_client.post("/paid", json=_request_payload("max_completion_tokens"))
    with pytest.raises(LiveSmokeError, match="cannot be rewritten"):
        ledger.record_outcome(outcome="failed", capture_ids=["rss-a"], reason="late-change")


def test_openai_embedding_builder_forces_one_internal_attempt(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    class FailingClient(_Client):
        def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            self.calls.append((args, kwargs))
            return httpx.Response(
                500,
                json={"error": "synthetic"},
                request=httpx.Request("POST", "https://provider.invalid/v1/embeddings"),
            )

    delegate = FailingClient()
    provider = build_guarded_openai_embedding_provider(
        config=ledger.config,
        ledger=ledger,
        route_id="embedding-primary",
        api_key="fake-test-key",
        input_ids=["rss-a", "rss-b"],
        dimension=2,
        client=delegate,
    )
    with pytest.raises(EmbeddingProviderError, match="after 1 attempts"):
        provider.embed(["first", "second"])
    assert len(delegate.calls) == 1
    assert ledger.snapshot()["totals"]["dispatches"] == 1


def test_guarded_llm_reconciles_normalized_usage_and_rejects_batch(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    raw = _Client()
    guarded_http = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=raw,
        input_ids=["rss-a"],
    )
    provider = GuardedLLMProvider(delegate=_LLM(guarded_http), http_client=guarded_http)
    request = LLMInvocationRequest(
        prompt_name="live-smoke",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt="bounded prompt",
        requested_schema="report-composition.v1",
    )
    response = provider.invoke(request)
    assert response.input_tokens == 12
    assert ledger.snapshot()["dispatches"][0]["status"] == "reconciled"
    with pytest.raises(LiveSmokeError, match="does not permit batch"):
        provider.invoke_batch([request])


def test_normalized_defaults_cannot_clear_partial_physical_usage(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    raw = _Client(response=_Response({"usage": {"prompt_tokens": 1}}))
    guarded_http = GuardedHTTPClient(
        config=ledger.config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=raw,
        input_ids=["rss-a"],
    )
    provider = GuardedLLMProvider(
        delegate=_LLM(guarded_http, input_tokens=1, output_tokens=0),
        http_client=guarded_http,
    )
    request = LLMInvocationRequest(
        prompt_name="live-smoke",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt="bounded prompt",
        requested_schema="report-composition.v1",
    )
    with pytest.raises(LiveSmokeError, match="physical provider response"):
        provider.invoke(request)
    dispatch = ledger.snapshot()["dispatches"][0]
    assert dispatch["status"] == "uncertain"
    assert dispatch["known_actual_usd"] is None


def test_gemini_usage_requires_output_and_thought_fields(tmp_path: Path) -> None:
    payload = _config_payload()
    generation = payload["routes"][0]
    generation["provider"] = "gemini"
    generation["model"] = "configured-gemini-model"
    config = LiveSmokeConfig.from_mapping(payload)
    ledger = _ledger(tmp_path, config)
    raw = _Client(
        response=_Response(
            {"usage": {"total_input_tokens": 10, "total_output_tokens": 4}}
        )
    )
    client = GuardedHTTPClient(
        config=config,
        ledger=ledger,
        route_id="generation-primary",
        delegate=raw,
        input_ids=["rss-a"],
    )
    client.post(
        "/paid",
        json={
            "model": "configured-gemini-model",
            "input": "bounded prompt",
            "generation_config": {"max_output_tokens": 128},
        },
    )
    dispatch = ledger.snapshot()["dispatches"][0]
    assert dispatch["status"] == "uncertain"
    assert dispatch["known_actual_usd"] is None


class _LLM:
    provider_name = "openai"
    model_name = "configured-generation-model"
    model_version = "2026-09-01"

    def __init__(
        self, client: GuardedHTTPClient, *, input_tokens: int = 12, output_tokens: int = 3
    ) -> None:
        self.client = client
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return mode == LLMInvocationMode.REALTIME

    def supports_structured_schema(self, schema_name: str) -> bool:
        return bool(schema_name)

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        self.client.post(
            "/paid",
            json={
                "model": self.model_name,
                "max_completion_tokens": 128,
                "messages": [{"role": "user", "content": request.prompt}],
            },
        )
        return LLMInvocationResponse(
            text="{}",
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            structured={},
        )

    def invoke_batch(self, requests: Any) -> Any:
        raise AssertionError(requests)

    def close(self) -> None:
        self.client.close()


def test_cli_defaults_to_safe_dry_validation_and_blocks_unauthorized_execution(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config_payload(authorized=False)), encoding="utf-8")
    dry = subprocess.run(
        [sys.executable, str(CLI), "--config", str(config_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert dry.returncode == 0
    assert '"mode": "validation_only"' in dry.stdout
    assert ARTICLE_A not in dry.stdout
    assert "harbor disruption" not in dry.stdout

    execute = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--config",
            str(config_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--execute",
            "--confirm-verification-id",
            VERIFICATION_ID,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert execute.returncode == 2
    assert "not authorized" in execute.stderr
    assert not (tmp_path / "state").exists()
