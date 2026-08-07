"""Fail-closed readiness boundary for ``stage9-validation.v2``.

This module opens a new validation namespace without changing or re-running v1.  It can:

* verify every Stage 9 v1 protocol/report/freeze/final artifact by its exact historical SHA-256;
* require a registered, non-``current`` embedding snapshot and its full manifest hash before a
  clustering or analogy development run is authorized;
* validate that clustering evidence is genuinely human-reviewed, grouped, and disjoint across
  train/development/final splits;
* keep analogy evaluation-only until a new, human-reviewed negative-labelled split exists; and
* preserve the entity-linking policy selected by v1 while rejecting the spent v1 final holdout.

It deliberately contains no evaluator, final-holdout loader, threshold selector, or report writer.
The only checked-in v2 report is a deterministic *readiness* scaffold.  Passing these checks permits
a later development evaluator to start; it never claims calibration and never permits a final run.
"""

from __future__ import annotations

import datetime
import json
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from services.entities.news_linking import (
    ACCEPT_THRESHOLD,
    ADJUDICATE_THRESHOLD,
    ENTITY_LINKING_POLICY_VERSION,
)
from services.evaluation.calibration import (
    CalibrationError,
    canonical_json_bytes,
    canonical_json_hash,
    sha256_file,
)
from services.nlp.snapshot_registry import (
    DEFAULT_REGISTRY_PATH,
    LEGACY_MODEL_VERSION,
    EmbeddingSnapshotError,
    SnapshotRecord,
    require_registered_snapshot,
)

PROTOCOL_ID = "stage9-validation.v2"
READINESS_SCHEMA = "stage9-validation-readiness.v2"
CLUSTERING_PAIRS_SCHEMA = "stage9-clustering-labeled-pairs.v2"
ANALOGY_PAIRS_SCHEMA = "stage9-analogy-labeled-pairs.v2"
REVIEW_MANIFEST_SCHEMA = "stage9-v2.human-review-manifest.v1"

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536
# Readiness is intentionally pinned to the reviewed, source-controlled snapshot instead of
# inheriting process environment.  A registry activation change must therefore be reviewed here
# before it can alter the canonical Stage 9 v2 artifact.
READINESS_EMBEDDING_SNAPSHOT_ID = "nip-es2-20260731-5e17cfbc0b7562ca228f"

_REPO_ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = _REPO_ROOT / "evaluation" / "stage9-v2"
READINESS_PATH = V2_ROOT / "readiness.json"
CLUSTERING_PAIRS_PATH = V2_ROOT / "gold" / "clustering-pairs.json"
ANALOGY_PAIRS_PATH = V2_ROOT / "gold" / "analogy-pairs.json"

V1_ENTITY_FINAL_HOLDOUT_PATH = (
    _REPO_ROOT / "evaluation" / "gold" / "entity_linking" / "v1" / "final_holdout.json"
)
V1_ENTITY_FINAL_HOLDOUT_SHA256 = "082797aed7892039ab7091a1b4c6fa8b00734a9075a74241464af9a65720ed7e"

# These values cite the exact bytes that opened v2.  They are intentionally literals rather than
# values rebuilt through the v1 evaluator: a v1 edit must fail loudly, not update this baseline.
V1_STAGE9_ARTIFACT_SHA256: Mapping[str, str] = {
    "docs/evaluation/stage9-validation-protocol.md": (
        "494f97fb5357ecda8d35a296567fde0d9c503be58257ea003df2bd4625fda3af"
    ),
    "evaluation/stage9/development/alerts.json": (
        "631e5c759c14be169205cb688fe65d411ce656cc8af115c92bb6ea695afca0cb"
    ),
    "evaluation/stage9/development/analogy.json": (
        "18f4331f7aecfa60ced44f55215125780c97cb50fc46373418ec26e84a7500ec"
    ),
    "evaluation/stage9/development/clustering.json": (
        "fed869dfc76d16c48add89f362f60d19855e90035eb20e0f0e775cc33e69ba96"
    ),
    "evaluation/stage9/development/entity-linking.json": (
        "3ee276f80a4c615dae1567402d5d8eca05dde2bab48ddd7c3bf22b21cd0131db"
    ),
    "evaluation/stage9/final/entity-linking.json": (
        "b2a378c91aeefd7cda304ca36b0039847cecb868ba79384548a98652a81947d3"
    ),
    "evaluation/stage9/final/entity-linking.unseal-receipt.json": (
        "c5ee3717459a61a855296266b115053b5d5674dc594bdb93e46dfb45d6f45588"
    ),
    "evaluation/stage9/frozen/manifest.json": (
        "f2e8499cc02cbecfba7aef32ac6ea52800441dee21a40ff12b781d450c24f414"
    ),
    "evaluation/stage9/frozen/parameters.json": (
        "6c1e5651bb7f6ed8ec892246b76888edca8c468b4e22eac5430e2e33915cac51"
    ),
}

_SPLITS = ("train", "development", "final_holdout")
_MIN_CLUSTERING_PAIRS = 100
_HEX = frozenset("0123456789abcdef")


class Stage9V2Error(RuntimeError):
    """A v2 input is unsafe, incomplete, non-canonical, or attempts to reuse v1."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Stage9V2Error(message)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise Stage9V2Error(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _load_canonical_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Stage9V2Error(f"missing v2 evidence artifact {path.name}") from exc
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stage9V2Error(f"invalid JSON in v2 evidence artifact {path.name}") from exc
    _require(isinstance(value, dict), f"v2 evidence artifact {path.name} must be an object")
    _require(
        raw == canonical_json_bytes(value),
        f"v2 evidence artifact {path.name} is not canonical JSON",
    )
    return value


def verify_v1_artifacts(*, repo_root: Path = _REPO_ROOT) -> dict[str, str]:
    """Verify the exact v1 bytes inherited by v2, including the spent entity holdout."""

    verified: dict[str, str] = {}
    expected = dict(V1_STAGE9_ARTIFACT_SHA256)
    expected["evaluation/gold/entity_linking/v1/final_holdout.json"] = (
        V1_ENTITY_FINAL_HOLDOUT_SHA256
    )
    for relative_path, expected_hash in sorted(expected.items()):
        path = repo_root / relative_path
        try:
            actual_hash = sha256_file(path)
        except CalibrationError as exc:
            raise Stage9V2Error(f"missing immutable v1 artifact {relative_path}") from exc
        _require(
            actual_hash == expected_hash,
            f"immutable v1 artifact hash mismatch: {relative_path}",
        )
        verified[relative_path] = actual_hash
    return verified


def _reviewer_identity(
    reviewers: Mapping[str, Any],
    role: str,
    *,
    domain: str,
) -> str:
    review = reviewers.get(role)
    _require(isinstance(review, Mapping), f"{domain} review manifest needs {role}")
    reviewer_id = review.get("reviewer_id")
    _require(
        isinstance(reviewer_id, str) and bool(reviewer_id.strip()),
        f"{domain} review manifest {role} needs a reviewer_id",
    )
    completed_on = review.get("completed_on")
    _require(
        isinstance(completed_on, str) and bool(completed_on),
        f"{domain} review manifest {role} needs completed_on",
    )
    try:
        datetime.date.fromisoformat(completed_on)
    except ValueError as exc:
        raise Stage9V2Error(
            f"{domain} review manifest {role} completed_on must be an ISO date"
        ) from exc
    return reviewer_id.strip()


def _human_reviewed_provenance(
    value: object,
    *,
    domain: str,
    artifact_path: Path,
    records: list[Any],
) -> None:
    """Verify a concrete dual-review/adjudication manifest bound to these exact labels."""

    _require(isinstance(value, Mapping), f"{domain} provenance must be an object")
    _require(
        value.get("label_source") == "human_reviewed",
        f"{domain} labels must be human_reviewed",
    )
    _require(value.get("review_status") == "adjudicated", f"{domain} labels must be adjudicated")
    _require(value.get("synthetic") is False, f"{domain} labels must not be synthetic")
    claimed_hash = value.get("review_manifest_sha256")
    _require(_is_sha256(claimed_hash), f"{domain} provenance needs a full review_manifest_sha256")

    manifest_path = artifact_path.with_name(f"{domain}-review-manifest.json")
    _require(manifest_path.is_file(), f"{domain} review manifest does not exist")
    _require(
        sha256_file(manifest_path) == claimed_hash,
        f"{domain} review manifest SHA-256 does not match provenance",
    )
    manifest = _load_canonical_object(manifest_path)
    _require(
        manifest.get("schema") == REVIEW_MANIFEST_SCHEMA,
        f"unexpected {domain} review-manifest schema",
    )
    _require(manifest.get("protocol_id") == PROTOCOL_ID, f"{domain} review protocol mismatch")
    _require(manifest.get("domain") == domain, f"{domain} review-manifest domain mismatch")
    _require(
        manifest.get("review_status") == "adjudicated",
        f"{domain} review manifest must be adjudicated",
    )
    _require(manifest.get("synthetic") is False, f"{domain} review manifest must not be synthetic")
    _require(
        manifest.get("record_count") == len(records),
        f"{domain} review manifest record count mismatch",
    )
    _require(
        manifest.get("records_sha256") == canonical_json_hash(records),
        f"{domain} review manifest is not bound to these exact labeled records",
    )
    reviewers = manifest.get("reviewers")
    _require(isinstance(reviewers, Mapping), f"{domain} review manifest needs reviewers")
    reviewer_ids = {
        _reviewer_identity(reviewers, role, domain=domain)
        for role in ("reviewer_a", "reviewer_b", "adjudicator")
    }
    _require(
        len(reviewer_ids) == 3,
        f"{domain} reviewer A, reviewer B, and adjudicator must be distinct",
    )


def _nonempty_string(record: Mapping[str, Any], field: str, *, domain: str) -> str:
    value = record.get(field)
    _require(
        isinstance(value, str) and bool(value),
        f"{domain} record field {field!r} must be a non-empty string",
    )
    return value


def _validate_pair_records(
    artifact: Mapping[str, Any],
    *,
    domain: str,
    label_field: str,
    endpoint_fields: tuple[str, str],
) -> dict[str, Any]:
    records = artifact.get("records")
    _require(isinstance(records, list) and bool(records), f"{domain} records must be non-empty")
    _require(all(isinstance(record, Mapping) for record in records), f"invalid {domain} record")

    ids: list[str] = []
    groups_by_split: dict[str, set[str]] = {split: set() for split in _SPLITS}
    endpoints_by_split: dict[str, set[str]] = {split: set() for split in _SPLITS}
    labels_by_split: dict[str, set[bool]] = {split: set() for split in _SPLITS}
    unordered_pairs: set[tuple[str, str]] = set()

    for item in records:
        record = dict(item)
        pair_id = _nonempty_string(record, "pair_id", domain=domain)
        split = _nonempty_string(record, "split", domain=domain)
        _require(split in _SPLITS, f"{domain} record has invalid split {split!r}")
        leakage_group = _nonempty_string(record, "leakage_group", domain=domain)
        left = _nonempty_string(record, endpoint_fields[0], domain=domain)
        right = _nonempty_string(record, endpoint_fields[1], domain=domain)
        _require(left != right, f"{domain} pair {pair_id!r} repeats one endpoint")
        label = record.get(label_field)
        _require(type(label) is bool, f"{domain} pair {pair_id!r} needs a boolean {label_field}")

        normalized_pair = tuple(sorted((left, right)))
        _require(normalized_pair not in unordered_pairs, f"{domain} repeats pair {normalized_pair}")
        unordered_pairs.add(normalized_pair)
        ids.append(pair_id)
        groups_by_split[split].add(leakage_group)
        endpoints_by_split[split].update((left, right))
        labels_by_split[split].add(label)

    _require(len(ids) == len(set(ids)), f"{domain} pair_id values must be unique")
    _require(ids == sorted(ids), f"{domain} records must be sorted by pair_id")
    for split in _SPLITS:
        _require(groups_by_split[split], f"{domain} split {split!r} is empty")
        _require(
            labels_by_split[split] == {False, True},
            f"{domain} split {split!r} must contain positive and negative labels",
        )
    for index, left_split in enumerate(_SPLITS):
        for right_split in _SPLITS[index + 1 :]:
            _require(
                groups_by_split[left_split].isdisjoint(groups_by_split[right_split]),
                f"{domain} leakage_group overlaps {left_split} and {right_split}",
            )
            _require(
                endpoints_by_split[left_split].isdisjoint(endpoints_by_split[right_split]),
                f"{domain} endpoint overlaps {left_split} and {right_split}",
            )

    return {
        "artifact_sha256": None,
        "pair_count": len(records),
        "split_counts": {
            split: sum(1 for record in records if record["split"] == split) for split in _SPLITS
        },
    }


def validate_clustering_pairs(path: Path = CLUSTERING_PAIRS_PATH) -> dict[str, Any]:
    """Validate human clustering labels and train/development/final group isolation."""

    artifact = _load_canonical_object(path)
    _require(
        artifact.get("schema") == CLUSTERING_PAIRS_SCHEMA,
        "unexpected clustering-pairs schema",
    )
    summary = _validate_pair_records(
        artifact,
        domain="clustering",
        label_field="same_event",
        endpoint_fields=("article_a_id", "article_b_id"),
    )
    _human_reviewed_provenance(
        artifact.get("provenance"),
        domain="clustering",
        artifact_path=path,
        records=artifact["records"],
    )
    _require(
        summary["pair_count"] >= _MIN_CLUSTERING_PAIRS,
        f"clustering requires at least {_MIN_CLUSTERING_PAIRS} reviewed pairs",
    )
    summary["artifact_sha256"] = sha256_file(path)
    return summary


def validate_analogy_pairs(path: Path = ANALOGY_PAIRS_PATH) -> dict[str, Any]:
    """Validate a new split analogy set with explicit negative/no-reliable-analogy labels."""

    artifact = _load_canonical_object(path)
    _require(artifact.get("schema") == ANALOGY_PAIRS_SCHEMA, "unexpected analogy-pairs schema")
    summary = _validate_pair_records(
        artifact,
        domain="analogy",
        label_field="reliable_analogy",
        endpoint_fields=("query_episode_id", "candidate_episode_id"),
    )
    _human_reviewed_provenance(
        artifact.get("provenance"),
        domain="analogy",
        artifact_path=path,
        records=artifact["records"],
    )
    summary["artifact_sha256"] = sha256_file(path)
    return summary


def _snapshot_readiness(
    *,
    model: str,
    model_version: str | None,
    dimension: int,
    registry_path: Path,
) -> tuple[dict[str, Any], SnapshotRecord | None]:
    base: dict[str, Any] = {
        "dimension": dimension,
        "manifest_sha256": None,
        "model": model,
        "model_version": model_version,
        "ready": False,
        "status": "not_configured",
    }
    if model_version is None or not model_version:
        return base, None
    if model_version == LEGACY_MODEL_VERSION:
        base["status"] = "legacy_unverifiable"
        return base, None
    try:
        record = require_registered_snapshot(
            model=model,
            model_version=model_version,
            dimension=dimension,
            registry_path=registry_path,
        )
    except EmbeddingSnapshotError:
        base["status"] = "unregistered_or_invalid"
        return base, None
    _require(_is_sha256(record.manifest_sha256), "snapshot record lacks a full manifest SHA-256")
    base.update(
        {
            "manifest_sha256": record.manifest_sha256,
            "ready": True,
            "status": "registered",
        }
    )
    return base, record


def _dataset_readiness(
    *,
    domain: str,
    path: Path,
) -> tuple[dict[str, Any], bool]:
    validator = validate_clustering_pairs if domain == "clustering" else validate_analogy_pairs
    missing_status = "not_verifiable" if domain == "clustering" else "evaluation_only"
    base: dict[str, Any] = {
        "artifact_path": os.fspath(path.relative_to(_REPO_ROOT))
        if path.is_relative_to(_REPO_ROOT)
        else path.name,
        "artifact_sha256": None,
        "pair_count": 0,
        "split_counts": {split: 0 for split in _SPLITS},
        "status": missing_status,
    }
    if not path.is_file():
        return base, False
    try:
        summary = validator(path)
    except Stage9V2Error:
        base["evidence_state"] = "invalid"
        return base, False
    base.update(summary)
    base["evidence_state"] = "verified"
    base["status"] = "ready_for_development"
    return base, True


def build_readiness_report(
    *,
    embedding_model: str = EMBEDDING_MODEL,
    embedding_model_version: str | None = READINESS_EMBEDDING_SNAPSHOT_ID,
    embedding_dimension: int = EMBEDDING_DIMENSION,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    clustering_pairs_path: Path = CLUSTERING_PAIRS_PATH,
    analogy_pairs_path: Path = ANALOGY_PAIRS_PATH,
    repo_root: Path = _REPO_ROOT,
) -> dict[str, Any]:
    """Build a deterministic, readiness-only report; no evaluator or holdout is opened."""

    verified_v1 = verify_v1_artifacts(repo_root=repo_root)
    snapshot, _record = _snapshot_readiness(
        model=embedding_model,
        model_version=embedding_model_version,
        dimension=embedding_dimension,
        registry_path=registry_path,
    )
    clustering, clustering_evidence_ready = _dataset_readiness(
        domain="clustering", path=clustering_pairs_path
    )
    analogy, analogy_evidence_ready = _dataset_readiness(domain="analogy", path=analogy_pairs_path)
    clustering_ready = snapshot["ready"] and clustering_evidence_ready
    analogy_ready = snapshot["ready"] and analogy_evidence_ready

    clustering["ready_for_development"] = clustering_ready
    clustering["status"] = "ready_for_development" if clustering_ready else "not_verifiable"
    clustering["blockers"] = [
        blocker
        for blocked, blocker in (
            (not snapshot["ready"], "registered_embedding_snapshot_required"),
            (not clustering_evidence_ready, "grouped_disjoint_human_labels_required"),
        )
        if blocked
    ]
    analogy["ready_for_development"] = analogy_ready
    analogy["status"] = "ready_for_development" if analogy_ready else "evaluation_only"
    analogy["blockers"] = [
        blocker
        for blocked, blocker in (
            (not snapshot["ready"], "registered_embedding_snapshot_required"),
            (not analogy_evidence_ready, "new_negative_labeled_split_required"),
        )
        if blocked
    ]

    return {
        "domains": {
            "alerts": {
                "production_applied": False,
                "ready_for_development": False,
                "status": "not_verifiable",
            },
            "analogy": analogy,
            "clustering": clustering,
            "entity_linking": {
                "final_holdout": {
                    "new_unseen_holdout_required": True,
                    "v1_reuse_allowed": False,
                },
                "production_policy": {
                    "accept": format(ACCEPT_THRESHOLD, ".3f"),
                    "adjudicate": format(ADJUDICATE_THRESHOLD, ".3f"),
                    "policy_version": ENTITY_LINKING_POLICY_VERSION,
                    "source_protocol": "stage9-validation.v1",
                },
                "status": "inherited_production_policy",
            },
        },
        "embedding_snapshot": snapshot,
        "execution": {
            "creates_final_outputs": False,
            "kind": "readiness_only",
            "permitted_development_domains": [
                domain
                for domain, ready in (
                    ("analogy", analogy_ready),
                    ("clustering", clustering_ready),
                )
                if ready
            ],
            "permitted_final_domains": [],
        },
        "protocol": {"id": PROTOCOL_ID},
        "schema": READINESS_SCHEMA,
        "v1_baseline": {
            "artifacts": [
                {"path": path, "sha256": digest}
                for path, digest in sorted(V1_STAGE9_ARTIFACT_SHA256.items())
            ],
            "spent_entity_final_holdout": {
                "path": "evaluation/gold/entity_linking/v1/final_holdout.json",
                "sha256": verified_v1["evaluation/gold/entity_linking/v1/final_holdout.json"],
            },
            "verified": True,
        },
    }


def validate_v2_development_output_path(
    path: Path,
    *,
    repo_root: Path = _REPO_ROOT,
) -> Path:
    """Accept only a fresh JSON path under v2/development; all v1/final paths fail closed."""

    resolved_root = repo_root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise Stage9V2Error("v2 output path must stay inside the repository") from exc
    _require(
        relative.parts[:3] == ("evaluation", "stage9-v2", "development"),
        "v2 development output must be under evaluation/stage9-v2/development",
    )
    _require(resolved.suffix == ".json", "v2 development output must be JSON")
    _require(not resolved.exists(), "v2 development output is write-once and already exists")
    return resolved


def require_fresh_entity_holdout(
    path: Path,
    *,
    repo_root: Path = _REPO_ROOT,
) -> dict[str, str]:
    """Reject the spent v1 entity holdout by location, bytes, or reused mention content."""

    resolved = path.resolve()
    v1_path = (repo_root / "evaluation/gold/entity_linking/v1/final_holdout.json").resolve()
    _require(resolved != v1_path, "the Stage 9 v1 entity final holdout is spent")
    try:
        relative = resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise Stage9V2Error("entity holdout must stay inside the repository") from exc
    _require(
        relative.parts[:4] == ("evaluation", "gold", "entity_linking", "v2"),
        "Stage 9 v2 requires a new entity_linking/v2 holdout",
    )
    _require(resolved.is_file(), "the new Stage 9 v2 entity holdout does not exist")
    digest = sha256_file(resolved)
    _require(
        digest != V1_ENTITY_FINAL_HOLDOUT_SHA256,
        "the Stage 9 v1 entity final holdout bytes are spent and cannot be copied into v2",
    )
    spent_fingerprints = _entity_holdout_input_fingerprints(v1_path)
    candidate_fingerprints = _entity_holdout_input_fingerprints(resolved)
    overlap = spent_fingerprints & candidate_fingerprints
    _require(
        not overlap,
        "the Stage 9 v1 entity final holdout records are spent and cannot be reformatted, "
        "reordered, or relabeled into v2",
    )
    return {"path": os.fspath(relative), "sha256": digest}


def _normalized_holdout_text(value: Any, field: str) -> str:
    _require(isinstance(value, str) and bool(value), f"entity holdout {field} must be text")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _entity_holdout_input_fingerprints(path: Path) -> set[str]:
    """Fingerprint blinded mention inputs while ignoring ids, labels, ordering, and wrapper metadata."""

    try:
        artifact = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stage9V2Error(f"invalid entity holdout {path.name}") from exc
    _require(isinstance(artifact, Mapping), "entity holdout must be a JSON object")
    mentions = artifact.get("mentions")
    _require(
        isinstance(mentions, list) and bool(mentions),
        "entity holdout mentions must be a non-empty list",
    )
    fingerprints: set[str] = set()
    for index, mention in enumerate(mentions):
        _require(isinstance(mention, Mapping), f"entity holdout mention {index} must be an object")
        sentence = mention.get("sentence")
        _require(
            isinstance(sentence, Mapping),
            f"entity holdout mention {index} sentence must be an object",
        )
        start = mention.get("start")
        end = mention.get("end")
        _require(
            type(start) is int and type(end) is int and 0 <= start < end,
            f"entity holdout mention {index} offsets are invalid",
        )
        fingerprint = canonical_json_hash(
            {
                "document_text": _normalized_holdout_text(
                    mention.get("document_text"),
                    f"mention {index} document_text",
                ),
                "mention_text": _normalized_holdout_text(
                    mention.get("mention_text"),
                    f"mention {index} mention_text",
                ),
                "sentence_text": _normalized_holdout_text(
                    sentence.get("text"),
                    f"mention {index} sentence.text",
                ),
                "start": start,
                "end": end,
            }
        )
        _require(
            fingerprint not in fingerprints,
            "entity holdout repeats a mention input under multiple records",
        )
        fingerprints.add(fingerprint)
    return fingerprints


def authorize_embedding_development_run(
    domain: str,
    *,
    output_path: Path,
    embedding_model: str,
    embedding_model_version: str,
    embedding_dimension: int,
    registry_path: Path,
    labeled_pairs_path: Path,
    repo_root: Path = _REPO_ROOT,
) -> dict[str, Any]:
    """Authorize only a clustering/analogy *development* run after all evidence checks."""

    _require(domain in {"clustering", "analogy"}, "unsupported embedding validation domain")
    output = validate_v2_development_output_path(output_path, repo_root=repo_root)
    try:
        snapshot = require_registered_snapshot(
            model=embedding_model,
            model_version=embedding_model_version,
            dimension=embedding_dimension,
            registry_path=registry_path,
        )
    except EmbeddingSnapshotError as exc:
        raise Stage9V2Error("a registered non-current embedding snapshot is required") from exc
    _require(_is_sha256(snapshot.manifest_sha256), "snapshot record lacks a full manifest SHA-256")
    evidence = (
        validate_clustering_pairs(labeled_pairs_path)
        if domain == "clustering"
        else validate_analogy_pairs(labeled_pairs_path)
    )
    return {
        "domain": domain,
        "embedding_snapshot": {
            "manifest_sha256": snapshot.manifest_sha256,
            "model": snapshot.model,
            "model_version": snapshot.snapshot_id,
        },
        "evidence": evidence,
        "output_path": os.fspath(output.relative_to(repo_root.resolve())),
        "protocol": PROTOCOL_ID,
        "run_kind": "development",
    }
