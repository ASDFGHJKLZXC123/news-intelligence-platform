"""Offline checks for verifiable local embedding snapshot identities."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import services.nlp.embeddings as embedding_lifecycle
from packages.config.settings import Settings
from packages.providers.base import EmbeddingResult
from services.nlp.embeddings import (
    snapshot_manifest_sha256_for_identity,
    validate_production_embedding_identity,
)
from services.nlp.snapshot_registry import (
    DEFAULT_MAX_ABSOLUTE_DRIFT,
    DEFAULT_MIN_COSINE_SIMILARITY,
    EmbeddingSnapshotError,
    activate_snapshot,
    active_snapshot,
    capture_snapshot,
    require_registered_snapshot,
    sha256_file,
    verify_snapshot,
)

MODEL = "text-embedding-3-small"
DIMENSION = 3
CAPTURED_AT = datetime.datetime(2026, 7, 29, 12, tzinfo=datetime.UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "snapshots"
    registry = root / "registry.json"
    probes = root / "probe-inputs.json"
    _write(
        registry,
        {
            "schema": "embedding-snapshot-registry.v1",
            "active_snapshot_id": None,
            "legacy_spaces": [{"model_version": "current", "status": "legacy_unverifiable"}],
            "snapshots": [],
        },
    )
    _write(
        probes,
        {
            "schema": "embedding-snapshot-probe-inputs.v1",
            "inputs": ["bank funding stress", "供应链中断"],
        },
    )
    return registry, probes


def _rehash_manifest(registry: Path, manifest_path: Path) -> None:
    saved_registry = json.loads(registry.read_text(encoding="utf-8"))
    relative = os.fspath(manifest_path.relative_to(registry.parent))
    for entry in saved_registry["snapshots"]:
        if entry["manifest_path"] == relative:
            entry["manifest_sha256"] = sha256_file(manifest_path)
            break
    else:  # pragma: no cover - a broken test fixture, not a product branch
        raise AssertionError("manifest is not registered")
    _write(registry, saved_registry)


class FakeProvider:
    provider_name = "openai"
    model_name = MODEL
    model_version = "snapshot-capture-pending"
    dimension = DIMENSION

    def __init__(
        self,
        *,
        drift: float = 0.0,
        vectors: tuple[tuple[float, ...], ...] | None = None,
    ) -> None:
        self.drift = drift
        self.vectors = vectors or ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        self.dimension = len(self.vectors[0])
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        self.calls.append(list(texts))
        return [
            EmbeddingResult(
                vector=(vector[0] + self.drift, *vector[1:]),
                provider_name=self.provider_name,
                model_name=self.model_name,
                model_version=self.model_version,
                dimension=self.dimension,
                model_run_id=f"probe-{index}",
            )
            for index, vector in enumerate(self.vectors[: len(texts)])
        ]


def test_capture_derives_inactive_id_and_registers_exact_artifacts(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    provider = FakeProvider()

    record = capture_snapshot(
        provider,
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert record.snapshot_id.startswith("nip-es2-20260729-")
    assert len(record.snapshot_id.rsplit("-", 1)[1]) == 20
    assert record.response_sha256.startswith(record.snapshot_id.rsplit("-", 1)[1])
    assert record.status == "retained"
    assert record.manifest_path.exists()
    saved_registry = json.loads(registry.read_text())
    assert saved_registry["active_snapshot_id"] is None
    assert saved_registry["legacy_spaces"] == [
        {"model_version": "current", "status": "legacy_unverifiable"}
    ]
    assert provider.calls == [["bank funding stress", "供应链中断"]]

    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    assert manifest["provider_adapter"] == "openai-embeddings-adapter.v2"
    assert manifest["drift_tolerances"] == {
        "max_absolute_drift": DEFAULT_MAX_ABSOLUTE_DRIFT,
        "min_cosine_similarity": DEFAULT_MIN_COSINE_SIMILARITY,
    }


def test_activation_replays_candidate_before_making_it_active(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    provider = FakeProvider()
    provider.model_version = record.snapshot_id

    verification = activate_snapshot(
        provider,
        record.snapshot_id,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert verification.max_absolute_drift == 0.0
    saved = json.loads(registry.read_text(encoding="utf-8"))
    assert saved["active_snapshot_id"] == record.snapshot_id
    assert saved["snapshots"][0]["status"] == "active"
    assert active_snapshot(
        model=MODEL,
        dimension=DIMENSION,
        registry_path=registry,
    ).snapshot_id == record.snapshot_id


def test_failed_activation_leaves_candidate_inactive(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    provider = FakeProvider(drift=0.01)
    provider.model_version = record.snapshot_id

    with pytest.raises(EmbeddingSnapshotError, match="drifted"):
        activate_snapshot(
            provider,
            record.snapshot_id,
            registry_path=registry,
            probe_inputs_path=probes,
        )

    saved = json.loads(registry.read_text(encoding="utf-8"))
    assert saved["active_snapshot_id"] is None
    assert saved["snapshots"][0]["status"] == "retained"


def test_current_is_never_accepted_as_a_registered_snapshot(tmp_path: Path) -> None:
    registry, _probes = _artifacts(tmp_path)

    with pytest.raises(EmbeddingSnapshotError, match="legacy_unverifiable"):
        require_registered_snapshot(
            model=MODEL,
            model_version="current",
            dimension=DIMENSION,
            registry_path=registry,
        )


def test_live_embedding_identity_enforcement_is_fail_closed_by_default() -> None:
    settings = Settings(
        embedding_model=MODEL,
        embedding_model_version="current",
        embedding_require_registered_snapshot=True,
    )

    with pytest.raises(
        EmbeddingSnapshotError,
        match="active embedding snapshot|not the active snapshot",
    ):
        validate_production_embedding_identity(settings)


def test_explicit_isolated_tooling_can_disable_registry_enforcement() -> None:
    settings = Settings(
        embedding_model=MODEL,
        embedding_model_version="current",
        embedding_require_registered_snapshot=False,
    )

    assert validate_production_embedding_identity(settings) is None


def test_production_identity_requires_the_configured_snapshot_to_be_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    settings = Settings(
        embedding_model=MODEL,
        embedding_model_version=record.snapshot_id,
        embedding_require_registered_snapshot=True,
    )
    monkeypatch.setattr(embedding_lifecycle, "DEFAULT_REGISTRY_PATH", registry)
    monkeypatch.setattr(embedding_lifecycle, "EMBEDDING_DIM", DIMENSION)

    with pytest.raises(EmbeddingSnapshotError, match="no active embedding snapshot"):
        validate_production_embedding_identity(settings)

    provider = FakeProvider()
    provider.model_version = record.snapshot_id
    activate_snapshot(
        provider,
        record.snapshot_id,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert validate_production_embedding_identity(settings).snapshot_id == record.snapshot_id


def test_manifest_tampering_is_detected_before_provider_use(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    record.manifest_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(EmbeddingSnapshotError, match="manifest hash mismatch"):
        require_registered_snapshot(
            model=MODEL,
            model_version=record.snapshot_id,
            dimension=DIMENSION,
            registry_path=registry,
        )


def test_missing_or_tampered_probe_evidence_is_rejected_before_provider_use(
    tmp_path: Path,
) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    response_path = registry.parent / manifest["probe_response_path"]
    response_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(EmbeddingSnapshotError, match="probe-response hash mismatch"):
        require_registered_snapshot(
            model=MODEL,
            model_version=record.snapshot_id,
            dimension=DIMENSION,
            registry_path=registry,
        )

    response_path.unlink()
    with pytest.raises(EmbeddingSnapshotError, match="cannot hash snapshot artifact"):
        require_registered_snapshot(
            model=MODEL,
            model_version=record.snapshot_id,
            dimension=DIMENSION,
            registry_path=registry,
        )


def test_probe_input_bytes_are_part_of_registered_snapshot_evidence(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    _write(
        probes,
        {
            "schema": "embedding-snapshot-probe-inputs.v1",
            "inputs": ["changed after capture"],
        },
    )

    with pytest.raises(EmbeddingSnapshotError, match="probe-input hash mismatch"):
        require_registered_snapshot(
            model=MODEL,
            model_version=record.snapshot_id,
            dimension=DIMENSION,
            registry_path=registry,
        )


def test_probe_replay_accepts_the_same_vectors(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    provider = FakeProvider()
    provider.model_version = record.snapshot_id

    result = verify_snapshot(
        provider,
        record.snapshot_id,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert result.probe_count == 2
    assert result.max_absolute_drift == 0.0
    assert result.minimum_cosine_similarity == pytest.approx(1.0)


@pytest.mark.parametrize("drift", [2**-13, DEFAULT_MAX_ABSOLUTE_DRIFT])
def test_probe_replay_accepts_quantized_noise_and_exact_boundary(
    tmp_path: Path,
    drift: float,
) -> None:
    registry, probes = _artifacts(tmp_path)
    baseline = ((0.0, 1.0, 0.0), (0.0, 1.0, 0.0))
    record = capture_snapshot(
        FakeProvider(vectors=baseline),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    provider = FakeProvider(drift=drift, vectors=baseline)
    provider.model_version = record.snapshot_id

    result = verify_snapshot(
        provider,
        record.snapshot_id,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert result.max_absolute_drift == pytest.approx(drift)
    assert result.minimum_cosine_similarity >= DEFAULT_MIN_COSINE_SIMILARITY


def test_probe_replay_fails_absolute_gate_while_cosine_still_passes(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    provider = FakeProvider(drift=0.001)
    provider.model_version = record.snapshot_id

    with pytest.raises(EmbeddingSnapshotError, match="max absolute drift"):
        verify_snapshot(
            provider,
            record.snapshot_id,
            registry_path=registry,
            probe_inputs_path=probes,
        )


def test_probe_replay_fails_cosine_gate_below_absolute_limit(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    width = 512
    component = width**-0.5
    baseline_vector = tuple(component for _ in range(width))
    baseline = (baseline_vector, baseline_vector)
    record = capture_snapshot(
        FakeProvider(vectors=baseline),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    delta = 0.00024
    changed_vector = tuple(
        value + (delta if index % 2 == 0 else -delta)
        for index, value in enumerate(baseline_vector)
    )
    provider = FakeProvider(vectors=(changed_vector, changed_vector))
    provider.model_version = record.snapshot_id

    with pytest.raises(EmbeddingSnapshotError, match="minimum cosine"):
        verify_snapshot(
            provider,
            record.snapshot_id,
            registry_path=registry,
            probe_inputs_path=probes,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_absolute_drift", True, "real number"),
        ("max_absolute_drift", "0.00025", "real number"),
        ("max_absolute_drift", -0.1, "non-negative"),
        ("min_cosine_similarity", False, "real number"),
        ("min_cosine_similarity", None, "real number"),
        ("min_cosine_similarity", -0.1, "between zero and one"),
        ("min_cosine_similarity", 1.1, "between zero and one"),
    ],
)
def test_invalid_manifest_tolerances_fail_closed(
    tmp_path: Path,
    field: str,
    value: Any,
    message: str,
) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    manifest["drift_tolerances"][field] = value
    _write(record.manifest_path, manifest)
    _rehash_manifest(registry, record.manifest_path)

    with pytest.raises(EmbeddingSnapshotError, match=message):
        require_registered_snapshot(
            model=MODEL,
            model_version=record.snapshot_id,
            dimension=DIMENSION,
            registry_path=registry,
        )


def test_snapshot_artifacts_are_write_once(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    first = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )
    second = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert second == first
    saved = json.loads(registry.read_text())
    assert len(saved["snapshots"]) == 1


def test_snapshot_identity_resolves_to_the_authoritative_manifest_hash(tmp_path: Path) -> None:
    registry, probes = _artifacts(tmp_path)
    record = capture_snapshot(
        FakeProvider(),
        captured_at=CAPTURED_AT,
        registry_path=registry,
        probe_inputs_path=probes,
    )

    assert (
        snapshot_manifest_sha256_for_identity(
            model=MODEL,
            model_version=record.snapshot_id,
            dimension=DIMENSION,
            registry_path=registry,
        )
        == record.manifest_sha256
    )


def test_non_snapshot_identity_has_no_manifest_claim(tmp_path: Path) -> None:
    registry, _probes = _artifacts(tmp_path)

    assert (
        snapshot_manifest_sha256_for_identity(
            model=MODEL,
            model_version="current",
            dimension=DIMENSION,
            registry_path=registry,
        )
        is None
    )


def test_capture_cli_runs_directly_and_fails_actionably_without_a_key(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment.pop("OPENAI_API_KEY", None)

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "capture-embedding-snapshot.py"),
            "capture",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "OPENAI_API_KEY is not configured" in completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr
