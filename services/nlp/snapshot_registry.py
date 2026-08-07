"""Source-controlled embedding snapshot capture and verification.

OpenAI exposes ``text-embedding-3-small`` as a model alias, not as an immutable provider build.
Calling that alias ``current`` forever can therefore mix vectors produced at different times under
one database key.  This module gives each observed vector space a local, verifiable identity:

``nip-es2-YYYYMMDD-<first 20 hex chars of the canonical probe-response SHA-256>``.

The identifier is deliberately derived from returned vectors rather than from a date or a claimed
vendor version.  The full response hash remains authoritative in the manifest.  A later process
can call :func:`verify_snapshot` before and after a backfill; if the hosted alias drifted beyond
the recorded tolerances, the write is refused instead of silently contaminating the space.

The registry is a source artifact, not a database foreign-key table.  That keeps migrations
deployable before the first live capture while still making production identities reviewable.
The legacy ``current`` space is recorded as unverifiable and is never an alias for a snapshot.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.providers.base import (
    EmbeddingProvider,
    EmbeddingResult,
    ensure_finite_vector,
    ensure_utc,
)

REGISTRY_SCHEMA = "embedding-snapshot-registry.v1"
MANIFEST_SCHEMA = "embedding-snapshot-manifest.v1"
PROBE_INPUT_SCHEMA = "embedding-snapshot-probe-inputs.v1"
PROBE_RESPONSE_SCHEMA = "embedding-snapshot-probe-responses.v1"
SNAPSHOT_ID_PATTERN = re.compile(
    r"^nip-(?P<format>es[12])-(?P<date>[0-9]{8})-(?P<digest>[0-9a-f]{20})$"
)
CURRENT_SNAPSHOT_FORMAT = "es2"
LEGACY_MODEL_VERSION = "current"

# OpenAI currently serves vectors on a binary16 lattice. These bounds admit small serving-side
# quantization variation while remaining far tighter than downstream similarity thresholds.
DEFAULT_MAX_ABSOLUTE_DRIFT = 2.5e-4
DEFAULT_MIN_COSINE_SIMILARITY = 0.99999

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_ROOT = _REPO_ROOT / "evaluation" / "embedding-snapshots"
DEFAULT_REGISTRY_PATH = DEFAULT_REGISTRY_ROOT / "registry.json"
DEFAULT_PROBE_INPUTS_PATH = DEFAULT_REGISTRY_ROOT / "probe-inputs.json"


class EmbeddingSnapshotError(RuntimeError):
    """A snapshot artifact is missing, malformed, unregistered, or no longer reproducible."""


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """The validated registry entry for one captured vector space."""

    snapshot_id: str
    provider: str
    model: str
    dimension: int
    manifest_path: Path
    manifest_sha256: str
    response_sha256: str
    status: str


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    """Drift diagnostics from replaying a registered snapshot's fixed probes."""

    snapshot_id: str
    probe_count: int
    max_absolute_drift: float
    minimum_cosine_similarity: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "probe_count": self.probe_count,
            "max_absolute_drift": self.max_absolute_drift,
            "minimum_cosine_similarity": self.minimum_cosine_similarity,
        }


def canonical_json_bytes(value: Any) -> bytes:
    """The one byte encoding used by every snapshot hash and stored JSON artifact."""

    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EmbeddingSnapshotError(f"snapshot artifact is not canonical JSON: {exc}") from exc
    return f"{payload}\n".encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EmbeddingSnapshotError(f"cannot hash snapshot artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EmbeddingSnapshotError(f"snapshot JSON repeats key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EmbeddingSnapshotError(f"cannot read snapshot artifact {path}: {exc}") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EmbeddingSnapshotError(f"snapshot artifact {path} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EmbeddingSnapshotError(f"snapshot artifact {path} must contain a JSON object")
    if raw != canonical_json_bytes(value):
        raise EmbeddingSnapshotError(f"snapshot artifact {path} is not canonical JSON")
    return value


def _require(value: bool, message: str) -> None:
    if not value:
        raise EmbeddingSnapshotError(message)


def _hex_digest(value: Any, field: str) -> str:
    _require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{field} must be a lowercase SHA-256 hex digest",
    )
    return value


def _drift_tolerances(manifest: Mapping[str, Any]) -> tuple[float, float]:
    tolerances = manifest.get("drift_tolerances")
    _require(isinstance(tolerances, Mapping), "manifest drift_tolerances must be an object")

    maximum_value = tolerances.get("max_absolute_drift")
    minimum_value = tolerances.get("min_cosine_similarity")
    _require(
        isinstance(maximum_value, int | float) and not isinstance(maximum_value, bool),
        "max_absolute_drift must be a real number",
    )
    _require(
        isinstance(minimum_value, int | float) and not isinstance(minimum_value, bool),
        "min_cosine_similarity must be a real number",
    )
    maximum = float(maximum_value)
    minimum = float(minimum_value)
    _require(math.isfinite(maximum), "max_absolute_drift must be finite")
    _require(math.isfinite(minimum), "min_cosine_similarity must be finite")
    _require(maximum >= 0.0, "max_absolute_drift must be non-negative")
    _require(
        0.0 <= minimum <= 1.0,
        "min_cosine_similarity must be between zero and one",
    )
    return maximum, minimum


def load_probe_inputs(path: Path = DEFAULT_PROBE_INPUTS_PATH) -> tuple[str, ...]:
    """Load the fixed, canonical probe text list without accepting an empty probe set."""

    artifact = _load_json(path)
    _require(artifact.get("schema") == PROBE_INPUT_SCHEMA, "unexpected probe-input schema")
    inputs = artifact.get("inputs")
    _require(isinstance(inputs, list) and bool(inputs), "probe inputs must be a non-empty list")
    _require(
        all(isinstance(value, str) and value for value in inputs),
        "every probe input must be a non-empty string",
    )
    _require(len(inputs) == len(set(inputs)), "probe inputs must not repeat")
    return tuple(inputs)


def _registry(path: Path) -> dict[str, Any]:
    registry = _load_json(path)
    _require(registry.get("schema") == REGISTRY_SCHEMA, "unexpected snapshot-registry schema")
    _require(
        registry.get("legacy_spaces")
        == [
            {
                "model_version": LEGACY_MODEL_VERSION,
                "status": "legacy_unverifiable",
            }
        ],
        "registry must preserve current as the one legacy_unverifiable space",
    )
    _require(isinstance(registry.get("snapshots"), list), "registry snapshots must be a list")
    active = registry.get("active_snapshot_id")
    _require(active is None or isinstance(active, str), "active_snapshot_id must be null or string")
    return registry


def _entry_for(registry: Mapping[str, Any], snapshot_id: str) -> Mapping[str, Any]:
    entries = [
        entry
        for entry in registry["snapshots"]
        if isinstance(entry, Mapping) and entry.get("snapshot_id") == snapshot_id
    ]
    _require(len(entries) == 1, f"snapshot {snapshot_id!r} is not registered exactly once")
    return entries[0]


def _artifact_path(root: Path, value: Any, field: str) -> Path:
    """Resolve one manifest-relative artifact without permitting an absolute/path-traversal read."""

    _require(isinstance(value, str) and bool(value), f"{field} must be a non-empty string")
    relative = Path(value)
    _require(not relative.is_absolute(), f"{field} must be relative to the snapshot registry")
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise EmbeddingSnapshotError(f"{field} escapes the snapshot registry") from exc
    return resolved


def _validate_probe_artifacts(
    manifest: Mapping[str, Any],
    *,
    root: Path,
    provider: Any,
    model: str,
    dimension: int,
) -> str:
    """Verify the exact fixed inputs and captured vectors a manifest claims as its evidence."""

    input_path = _artifact_path(root, manifest.get("probe_input_path"), "probe_input_path")
    input_hash = _hex_digest(manifest.get("probe_input_sha256"), "probe_input_sha256")
    _require(
        sha256_file(input_path) == input_hash,
        "snapshot probe-input hash mismatch",
    )
    inputs = load_probe_inputs(input_path)

    response_path = _artifact_path(
        root,
        manifest.get("probe_response_path"),
        "probe_response_path",
    )
    response_hash = _hex_digest(
        manifest.get("probe_response_sha256"),
        "probe_response_sha256",
    )
    _require(
        sha256_file(response_path) == response_hash,
        "snapshot probe-response hash mismatch",
    )
    response = _load_json(response_path)
    _require(response.get("schema") == PROBE_RESPONSE_SCHEMA, "unexpected probe-response schema")
    _require(response.get("provider") == provider, "probe-response provider mismatch")
    _require(response.get("model") == model, "probe-response model mismatch")
    _require(response.get("dimension") == dimension, "probe-response dimension mismatch")
    vectors = response.get("vectors")
    _require(
        isinstance(vectors, list) and len(vectors) == len(inputs),
        "probe-response vector count does not match fixed inputs",
    )
    for index, vector in enumerate(vectors):
        _require(isinstance(vector, list), f"stored probe {index} is not a vector")
        try:
            finite = ensure_finite_vector(vector)
        except (TypeError, ValueError) as exc:
            raise EmbeddingSnapshotError(f"stored probe {index} is invalid: {exc}") from exc
        _require(
            len(finite) == dimension,
            f"stored probe {index} width does not match snapshot dimension",
        )
    return response_hash


def require_registered_snapshot(
    *,
    model: str,
    model_version: str,
    dimension: int,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> SnapshotRecord:
    """Validate a production identity against its registry entry and exact manifest bytes."""

    _require(
        model_version != LEGACY_MODEL_VERSION,
        "embedding model_version 'current' is legacy_unverifiable and cannot identify a "
        "new production vector; capture and configure a registered snapshot",
    )
    match = SNAPSHOT_ID_PATTERN.fullmatch(model_version)
    _require(match is not None, f"invalid embedding snapshot id {model_version!r}")

    registry = _registry(registry_path)
    entry = _entry_for(registry, model_version)
    root = registry_path.parent
    manifest_path = _artifact_path(root, entry.get("manifest_path"), "manifest_path")
    manifest_hash = _hex_digest(entry.get("manifest_sha256"), "manifest_sha256")
    _require(
        sha256_file(manifest_path) == manifest_hash,
        f"snapshot manifest hash mismatch for {model_version}",
    )
    manifest = _load_json(manifest_path)
    _require(manifest.get("schema") == MANIFEST_SCHEMA, "unexpected snapshot-manifest schema")
    _require(manifest.get("snapshot_id") == model_version, "manifest snapshot id mismatch")
    _require(manifest.get("model") == model, "manifest model does not match configured model")
    _require(manifest.get("dimension") == dimension, "manifest dimension does not match column")
    _require(manifest.get("provider") == entry.get("provider"), "registry provider mismatch")
    _drift_tolerances(manifest)
    response_hash = _validate_probe_artifacts(
        manifest,
        root=root,
        provider=entry.get("provider"),
        model=model,
        dimension=dimension,
    )
    _require(
        response_hash.startswith(match.group("digest")),
        "snapshot id digest does not match the authoritative probe response hash",
    )
    _require(
        _hex_digest(entry.get("response_sha256"), "response_sha256") == response_hash,
        "registry and manifest probe response hashes differ",
    )
    status = entry.get("status")
    _require(status in {"active", "retained"}, "snapshot status must be active or retained")
    return SnapshotRecord(
        snapshot_id=model_version,
        provider=str(entry["provider"]),
        model=model,
        dimension=dimension,
        manifest_path=manifest_path,
        manifest_sha256=manifest_hash,
        response_sha256=response_hash,
        status=str(status),
    )


def active_snapshot(
    *,
    model: str,
    dimension: int,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> SnapshotRecord:
    """Return the registry's active snapshot or fail before any embedding request is made."""

    registry = _registry(registry_path)
    snapshot_id = registry.get("active_snapshot_id")
    _require(
        isinstance(snapshot_id, str) and snapshot_id,
        "no active embedding snapshot is registered; run the capture command first",
    )
    record = require_registered_snapshot(
        model=model,
        model_version=snapshot_id,
        dimension=dimension,
        registry_path=registry_path,
    )
    _require(record.status == "active", "active_snapshot_id does not point to an active snapshot")
    return record


def _response_artifact(
    results: Sequence[EmbeddingResult],
    *,
    provider: str,
    model: str,
    dimension: int,
) -> dict[str, Any]:
    _require(bool(results), "snapshot capture returned no probe vectors")
    vectors: list[list[float]] = []
    for index, result in enumerate(results):
        _require(result.provider_name == provider, f"probe {index} provider mismatch")
        _require(result.model_name == model, f"probe {index} model mismatch")
        _require(result.dimension == dimension, f"probe {index} dimension mismatch")
        vectors.append(list(result.vector))
    return {
        "schema": PROBE_RESPONSE_SCHEMA,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "vectors": vectors,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def capture_snapshot(
    provider: EmbeddingProvider,
    *,
    captured_at: datetime.datetime,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    probe_inputs_path: Path = DEFAULT_PROBE_INPUTS_PATH,
) -> SnapshotRecord:
    """Capture fixed probes as an inactive candidate with a derived, immutable identity."""

    timestamp = ensure_utc(captured_at)
    inputs = load_probe_inputs(probe_inputs_path)
    results = tuple(provider.embed(list(inputs)))
    _require(len(results) == len(inputs), "provider returned the wrong number of probe vectors")
    provider_name = getattr(provider, "provider_name", None)
    model = getattr(provider, "model_name", None)
    dimension = getattr(provider, "dimension", None)
    _require(isinstance(provider_name, str) and provider_name, "provider has no stable name")
    _require(isinstance(model, str) and model, "provider has no model name")
    _require(isinstance(dimension, int) and dimension > 0, "provider has no valid dimension")

    response = _response_artifact(results, provider=provider_name, model=model, dimension=dimension)
    response_bytes = canonical_json_bytes(response)
    response_hash = sha256_bytes(response_bytes)
    snapshot_id = f"nip-{CURRENT_SNAPSHOT_FORMAT}-{timestamp:%Y%m%d}-{response_hash[:20]}"
    relative_dir = Path(snapshot_id)
    root = registry_path.parent
    response_path = root / relative_dir / "probe-responses.json"
    manifest_path = root / relative_dir / "manifest.json"
    probe_hash = sha256_file(probe_inputs_path)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "snapshot_id": snapshot_id,
        "provider": provider_name,
        "model": model,
        "dimension": dimension,
        "captured_at": timestamp.isoformat().replace("+00:00", "Z"),
        "probe_input_path": os.fspath(probe_inputs_path.relative_to(root)),
        "probe_input_sha256": probe_hash,
        "probe_response_path": os.fspath(response_path.relative_to(root)),
        "probe_response_sha256": response_hash,
        "input_contracts": {
            "article": "article-embedding-text.v1",
            "event": "event-embedding-text.v1",
            "historical_episode": "historical-episode-onset-text.v1",
        },
        "provider_adapter": "openai-embeddings-adapter.v2",
        "drift_tolerances": {
            "max_absolute_drift": DEFAULT_MAX_ABSOLUTE_DRIFT,
            "min_cosine_similarity": DEFAULT_MIN_COSINE_SIMILARITY,
        },
    }
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_hash = sha256_bytes(manifest_bytes)

    registry = _registry(registry_path)
    existing_ids = {
        entry.get("snapshot_id") for entry in registry["snapshots"] if isinstance(entry, Mapping)
    }
    if snapshot_id in existing_ids:
        existing = require_registered_snapshot(
            model=model,
            model_version=snapshot_id,
            dimension=dimension,
            registry_path=registry_path,
        )
        _require(
            existing.response_sha256 == response_hash,
            "snapshot id collision with different response bytes",
        )
        return existing

    if response_path.exists() or manifest_path.exists():
        raise EmbeddingSnapshotError(
            f"refusing to overwrite unregistered snapshot artifacts under {relative_dir}"
        )
    _atomic_write(response_path, response_bytes)
    _atomic_write(manifest_path, manifest_bytes)

    entries: list[dict[str, Any]] = []
    for value in registry["snapshots"]:
        _require(isinstance(value, dict), "registry snapshot entries must be objects")
        entry = dict(value)
        entries.append(entry)
    entries.append(
        {
            "snapshot_id": snapshot_id,
            "provider": provider_name,
            "model": model,
            "dimension": dimension,
            "manifest_path": os.fspath(manifest_path.relative_to(root)),
            "manifest_sha256": manifest_hash,
            "response_sha256": response_hash,
            "status": "retained",
        }
    )
    entries.sort(key=lambda entry: entry["snapshot_id"])
    registry["snapshots"] = entries
    _atomic_write(registry_path, canonical_json_bytes(registry))

    return require_registered_snapshot(
        model=model,
        model_version=snapshot_id,
        dimension=dimension,
        registry_path=registry_path,
    )


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise EmbeddingSnapshotError("snapshot probe contains a zero vector")
    return numerator / (left_norm * right_norm)


def verify_snapshot(
    provider: EmbeddingProvider,
    snapshot_id: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    probe_inputs_path: Path = DEFAULT_PROBE_INPUTS_PATH,
) -> SnapshotVerification:
    """Replay a snapshot's probes and reject provider-alias drift outside its manifest bounds."""

    model = getattr(provider, "model_name", None)
    dimension = getattr(provider, "dimension", None)
    _require(isinstance(model, str), "provider has no model name")
    _require(isinstance(dimension, int), "provider has no dimension")
    record = require_registered_snapshot(
        model=model,
        model_version=snapshot_id,
        dimension=dimension,
        registry_path=registry_path,
    )
    manifest = _load_json(record.manifest_path)
    root = registry_path.parent
    response_path = root / str(manifest["probe_response_path"])
    _require(
        sha256_file(response_path) == record.response_sha256,
        "stored probe response hash mismatch",
    )
    expected = _load_json(response_path)
    inputs = load_probe_inputs(probe_inputs_path)
    _require(
        sha256_file(probe_inputs_path) == manifest["probe_input_sha256"],
        "probe input hash does not match snapshot manifest",
    )
    observed = tuple(provider.embed(list(inputs)))
    _require(len(observed) == len(inputs), "provider returned the wrong number of probes")
    expected_vectors = expected.get("vectors")
    _require(
        isinstance(expected_vectors, list) and len(expected_vectors) == len(observed),
        "stored probe response vector count mismatch",
    )

    max_drift = 0.0
    minimum_cosine = 1.0
    for index, (baseline, result) in enumerate(zip(expected_vectors, observed, strict=True)):
        _require(isinstance(baseline, list), f"stored probe {index} is not a vector")
        _require(result.model_name == model, f"observed probe {index} model mismatch")
        _require(
            result.model_version == snapshot_id,
            f"observed probe {index} snapshot identity mismatch",
        )
        _require(result.dimension == dimension, f"observed probe {index} dimension mismatch")
        current = list(result.vector)
        _require(len(baseline) == len(current), f"observed probe {index} width mismatch")
        max_drift = max(
            max_drift,
            max(
                abs(float(left) - float(right))
                for left, right in zip(baseline, current, strict=True)
            ),
        )
        minimum_cosine = min(
            minimum_cosine,
            _cosine_similarity(
                [float(value) for value in baseline],
                [float(value) for value in current],
            ),
        )

    maximum_allowed, minimum_allowed = _drift_tolerances(manifest)
    _require(
        max_drift <= maximum_allowed and minimum_cosine >= minimum_allowed,
        f"embedding snapshot {snapshot_id} drifted: max absolute drift {max_drift} "
        f"(allowed {maximum_allowed}), minimum cosine {minimum_cosine} "
        f"(required {minimum_allowed})",
    )
    return SnapshotVerification(
        snapshot_id=snapshot_id,
        probe_count=len(observed),
        max_absolute_drift=max_drift,
        minimum_cosine_similarity=minimum_cosine,
    )


def activate_snapshot(
    provider: EmbeddingProvider,
    snapshot_id: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    probe_inputs_path: Path = DEFAULT_PROBE_INPUTS_PATH,
) -> SnapshotVerification:
    """Replay a candidate and atomically make it the sole active snapshot only on success."""

    verification = verify_snapshot(
        provider,
        snapshot_id,
        registry_path=registry_path,
        probe_inputs_path=probe_inputs_path,
    )
    registry = _registry(registry_path)
    _entry_for(registry, snapshot_id)
    entries: list[dict[str, Any]] = []
    for value in registry["snapshots"]:
        _require(isinstance(value, dict), "registry snapshot entries must be objects")
        entry = dict(value)
        if entry.get("snapshot_id") == snapshot_id:
            entry["status"] = "active"
        elif entry.get("status") == "active":
            entry["status"] = "retained"
        entries.append(entry)
    registry["snapshots"] = entries
    registry["active_snapshot_id"] = snapshot_id
    _atomic_write(registry_path, canonical_json_bytes(registry))
    return verification


__all__ = [
    "DEFAULT_PROBE_INPUTS_PATH",
    "DEFAULT_REGISTRY_PATH",
    "EmbeddingSnapshotError",
    "LEGACY_MODEL_VERSION",
    "SnapshotRecord",
    "SnapshotVerification",
    "activate_snapshot",
    "active_snapshot",
    "canonical_json_bytes",
    "capture_snapshot",
    "load_probe_inputs",
    "require_registered_snapshot",
    "sha256_bytes",
    "sha256_file",
    "verify_snapshot",
]
