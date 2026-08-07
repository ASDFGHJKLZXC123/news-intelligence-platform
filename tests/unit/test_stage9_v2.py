"""Safety and readiness checks for the isolated Stage 9 validation-v2 boundary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from services.evaluation import stage9_v2
from services.evaluation.calibration import canonical_json_bytes, canonical_json_hash, sha256_file
from services.evaluation.stage9_v2 import (
    ANALOGY_PAIRS_SCHEMA,
    CLUSTERING_PAIRS_SCHEMA,
    PROTOCOL_ID,
    READINESS_EMBEDDING_SNAPSHOT_ID,
    READINESS_PATH,
    READINESS_SCHEMA,
    REVIEW_MANIFEST_SCHEMA,
    V1_ENTITY_FINAL_HOLDOUT_PATH,
    V1_ENTITY_FINAL_HOLDOUT_SHA256,
    V1_STAGE9_ARTIFACT_SHA256,
    Stage9V2Error,
    authorize_embedding_development_run,
    build_readiness_report,
    require_fresh_entity_holdout,
    validate_analogy_pairs,
    validate_clustering_pairs,
    validate_v2_development_output_path,
    verify_v1_artifacts,
)
from services.nlp.snapshot_registry import SnapshotRecord, active_snapshot

_MODEL = "text-embedding-3-small"
_SNAPSHOT_ID = "nip-es1-20260729-0123456789abcdefabcd"
_MANIFEST_HASH = "b" * 64


def _write_canonical(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _pair_artifact(
    *,
    domain: str,
    count: int,
) -> dict[str, Any]:
    schema = CLUSTERING_PAIRS_SCHEMA if domain == "clustering" else ANALOGY_PAIRS_SCHEMA
    label_field = "same_event" if domain == "clustering" else "reliable_analogy"
    endpoint_fields = (
        ("article_a_id", "article_b_id")
        if domain == "clustering"
        else ("query_episode_id", "candidate_episode_id")
    )
    split_sizes = {
        "train": count // 3 + (1 if count % 3 else 0),
        "development": count // 3 + (1 if count % 3 == 2 else 0),
        "final_holdout": count // 3,
    }
    records: list[dict[str, Any]] = []
    index = 0
    for split, size in split_sizes.items():
        for split_index in range(size):
            records.append(
                {
                    endpoint_fields[0]: f"{domain}-{split}-left-{split_index:03d}",
                    endpoint_fields[1]: f"{domain}-{split}-right-{split_index:03d}",
                    "leakage_group": f"{domain}-{split}-group-{split_index // 2:03d}",
                    label_field: split_index % 2 == 0,
                    "pair_id": f"pair-{index:03d}",
                    "split": split,
                }
            )
            index += 1
    return {
        "provenance": {
            "label_source": "human_reviewed",
            "review_manifest_sha256": "pending",
            "review_status": "adjudicated",
            "synthetic": False,
        },
        "records": records,
        "schema": schema,
    }


def _write_reviewed_pair_artifact(path: Path, artifact: dict[str, Any], *, domain: str) -> None:
    manifest_path = path.with_name(f"{domain}-review-manifest.json")
    manifest = {
        "domain": domain,
        "protocol_id": PROTOCOL_ID,
        "record_count": len(artifact["records"]),
        "records_sha256": canonical_json_hash(artifact["records"]),
        "review_status": "adjudicated",
        "reviewers": {
            "adjudicator": {
                "completed_on": "2026-07-28",
                "reviewer_id": "test-adjudicator",
            },
            "reviewer_a": {
                "completed_on": "2026-07-27",
                "reviewer_id": "test-reviewer-a",
            },
            "reviewer_b": {
                "completed_on": "2026-07-27",
                "reviewer_id": "test-reviewer-b",
            },
        },
        "schema": REVIEW_MANIFEST_SCHEMA,
        "synthetic": False,
    }
    _write_canonical(manifest_path, manifest)
    artifact["provenance"]["review_manifest_sha256"] = sha256_file(manifest_path)
    _write_canonical(path, artifact)


def _snapshot_record(tmp_path: Path) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=_SNAPSHOT_ID,
        provider="openai",
        model=_MODEL,
        dimension=1536,
        manifest_path=tmp_path / "manifest.json",
        manifest_sha256=_MANIFEST_HASH,
        response_sha256="c" * 64,
        status="active",
    )


def test_v2_protocol_identity_and_checked_in_scaffold_are_canonical() -> None:
    expected = build_readiness_report()

    assert expected["schema"] == READINESS_SCHEMA
    assert expected["protocol"] == {"id": PROTOCOL_ID}
    assert READINESS_PATH.read_bytes() == canonical_json_bytes(expected)
    assert json.loads(READINESS_PATH.read_text(encoding="utf-8")) == expected


def test_default_readiness_snapshot_matches_the_verified_active_registry_entry() -> None:
    record = active_snapshot(model=_MODEL, dimension=1536)
    report = build_readiness_report()

    assert record.snapshot_id == READINESS_EMBEDDING_SNAPSHOT_ID
    assert report["embedding_snapshot"] == {
        "dimension": record.dimension,
        "manifest_sha256": record.manifest_sha256,
        "model": record.model,
        "model_version": record.snapshot_id,
        "ready": True,
        "status": "registered",
    }


def test_v1_artifact_hashes_remain_the_exact_opening_baseline() -> None:
    verified = verify_v1_artifacts()

    assert {path: verified[path] for path in V1_STAGE9_ARTIFACT_SHA256} == dict(
        V1_STAGE9_ARTIFACT_SHA256
    )
    assert (
        verified["evaluation/gold/entity_linking/v1/final_holdout.json"]
        == V1_ENTITY_FINAL_HOLDOUT_SHA256
    )
    assert sha256_file(V1_ENTITY_FINAL_HOLDOUT_PATH) == V1_ENTITY_FINAL_HOLDOUT_SHA256


@pytest.mark.parametrize(
    ("model_version", "expected_status"),
    [
        (None, "not_configured"),
        ("current", "legacy_unverifiable"),
        (_SNAPSHOT_ID, "unregistered_or_invalid"),
    ],
)
def test_missing_current_and_unregistered_snapshots_fail_readiness(
    model_version: str | None,
    expected_status: str,
) -> None:
    report = build_readiness_report(embedding_model_version=model_version)

    assert report["embedding_snapshot"]["ready"] is False
    assert report["embedding_snapshot"]["status"] == expected_status
    assert report["embedding_snapshot"]["manifest_sha256"] is None
    assert report["domains"]["clustering"]["status"] == "not_verifiable"
    assert report["domains"]["analogy"]["status"] == "evaluation_only"
    assert report["execution"]["permitted_development_domains"] == []
    assert report["execution"]["permitted_final_domains"] == []


def test_scaffold_inherits_v1_entity_policy_and_marks_holdout_spent() -> None:
    entity = build_readiness_report()["domains"]["entity_linking"]

    assert entity["production_policy"] == {
        "accept": "0.070",
        "adjudicate": "0.000",
        "policy_version": "entity-linking-bands.stage9-validation.v1",
        "source_protocol": "stage9-validation.v1",
    }
    assert entity["status"] == "inherited_production_policy"
    assert entity["final_holdout"] == {
        "new_unseen_holdout_required": True,
        "v1_reuse_allowed": False,
    }


def test_v1_and_final_output_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(Stage9V2Error, match="stage9-v2/development"):
        validate_v2_development_output_path(
            tmp_path / "evaluation/stage9/development/clustering.json",
            repo_root=tmp_path,
        )
    with pytest.raises(Stage9V2Error, match="stage9-v2/development"):
        validate_v2_development_output_path(
            tmp_path / "evaluation/stage9-v2/final/clustering.json",
            repo_root=tmp_path,
        )

    accepted = tmp_path / "evaluation/stage9-v2/development/clustering.json"
    assert validate_v2_development_output_path(accepted, repo_root=tmp_path) == accepted.resolve()


def test_spent_entity_holdout_is_rejected_by_path_and_copied_bytes(tmp_path: Path) -> None:
    with pytest.raises(Stage9V2Error, match="spent"):
        require_fresh_entity_holdout(V1_ENTITY_FINAL_HOLDOUT_PATH)

    copied = tmp_path / "evaluation/gold/entity_linking/v2/final_holdout.json"
    copied.parent.mkdir(parents=True)
    shutil.copyfile(V1_ENTITY_FINAL_HOLDOUT_PATH, copied)
    with pytest.raises(Stage9V2Error, match="bytes are spent"):
        require_fresh_entity_holdout(copied, repo_root=tmp_path)


def test_spent_entity_holdout_is_rejected_after_reordering_and_rewrapping(
    tmp_path: Path,
) -> None:
    copied = json.loads(V1_ENTITY_FINAL_HOLDOUT_PATH.read_text(encoding="utf-8"))
    spent = tmp_path / "evaluation/gold/entity_linking/v1/final_holdout.json"
    spent.parent.mkdir(parents=True)
    shutil.copyfile(V1_ENTITY_FINAL_HOLDOUT_PATH, spent)
    copied["schema_version"] = "v2"
    copied["mentions"] = list(reversed(copied["mentions"]))
    copied["new_metadata"] = {"collection": "renamed-copy"}
    path = tmp_path / "evaluation/gold/entity_linking/v2/final_holdout.json"
    _write_canonical(path, copied)

    with pytest.raises(Stage9V2Error, match="records are spent"):
        require_fresh_entity_holdout(path, repo_root=tmp_path)


def test_clustering_stays_unverifiable_without_grouped_disjoint_reviewed_pairs() -> None:
    report = build_readiness_report()
    clustering = report["domains"]["clustering"]

    assert clustering["status"] == "not_verifiable"
    assert clustering["pair_count"] == 0
    assert "grouped_disjoint_human_labels_required" in clustering["blockers"]


def test_clustering_pair_contract_accepts_100_reviewed_pairs_and_rejects_split_leakage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clustering-pairs.json"
    artifact = _pair_artifact(domain="clustering", count=100)
    _write_reviewed_pair_artifact(path, artifact, domain="clustering")

    summary = validate_clustering_pairs(path)
    assert summary["pair_count"] == 100
    assert summary["split_counts"] == {
        "train": 34,
        "development": 33,
        "final_holdout": 33,
    }
    assert summary["artifact_sha256"] == sha256_file(path)

    artifact["records"][34]["leakage_group"] = artifact["records"][0]["leakage_group"]
    _write_reviewed_pair_artifact(path, artifact, domain="clustering")
    with pytest.raises(Stage9V2Error, match="leakage_group overlaps"):
        validate_clustering_pairs(path)


def test_reviewed_pair_claim_requires_a_real_hash_bound_review_manifest(tmp_path: Path) -> None:
    path = tmp_path / "clustering-pairs.json"
    artifact = _pair_artifact(domain="clustering", count=100)
    artifact["provenance"]["review_manifest_sha256"] = "a" * 64
    _write_canonical(path, artifact)

    with pytest.raises(Stage9V2Error, match="review manifest does not exist"):
        validate_clustering_pairs(path)

    _write_reviewed_pair_artifact(path, artifact, domain="clustering")
    artifact["records"][0]["same_event"] = not artifact["records"][0]["same_event"]
    _write_canonical(path, artifact)
    with pytest.raises(Stage9V2Error, match="not bound to these exact labeled records"):
        validate_clustering_pairs(path)


def test_analogy_remains_evaluation_only_until_new_negative_labeled_splits_exist(
    tmp_path: Path,
) -> None:
    path = tmp_path / "analogy-pairs.json"
    artifact = _pair_artifact(domain="analogy", count=6)
    for record in artifact["records"]:
        record["reliable_analogy"] = True
    _write_reviewed_pair_artifact(path, artifact, domain="analogy")

    with pytest.raises(Stage9V2Error, match="positive and negative"):
        validate_analogy_pairs(path)

    artifact = _pair_artifact(domain="analogy", count=6)
    _write_reviewed_pair_artifact(path, artifact, domain="analogy")
    assert validate_analogy_pairs(path)["split_counts"] == {
        "train": 2,
        "development": 2,
        "final_holdout": 2,
    }


def test_development_authorization_carries_full_snapshot_manifest_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = tmp_path / "analogy-pairs.json"
    _write_reviewed_pair_artifact(
        pairs,
        _pair_artifact(domain="analogy", count=6),
        domain="analogy",
    )
    snapshot = _snapshot_record(tmp_path)
    monkeypatch.setattr(stage9_v2, "require_registered_snapshot", lambda **_kwargs: snapshot)

    authorization = authorize_embedding_development_run(
        "analogy",
        output_path=tmp_path / "evaluation/stage9-v2/development/analogy.json",
        embedding_model=_MODEL,
        embedding_model_version=_SNAPSHOT_ID,
        embedding_dimension=1536,
        registry_path=tmp_path / "registry.json",
        labeled_pairs_path=pairs,
        repo_root=tmp_path,
    )

    assert authorization["protocol"] == PROTOCOL_ID
    assert authorization["run_kind"] == "development"
    assert authorization["embedding_snapshot"] == {
        "manifest_sha256": _MANIFEST_HASH,
        "model": _MODEL,
        "model_version": _SNAPSHOT_ID,
    }
    assert "final" not in authorization["output_path"]


def test_current_snapshot_cannot_authorize_a_development_run(tmp_path: Path) -> None:
    pairs = tmp_path / "analogy-pairs.json"
    _write_reviewed_pair_artifact(
        pairs,
        _pair_artifact(domain="analogy", count=6),
        domain="analogy",
    )

    with pytest.raises(Stage9V2Error, match="registered non-current"):
        authorize_embedding_development_run(
            "analogy",
            output_path=tmp_path / "evaluation/stage9-v2/development/analogy.json",
            embedding_model=_MODEL,
            embedding_model_version="current",
            embedding_dimension=1536,
            registry_path=tmp_path / "registry.json",
            labeled_pairs_path=pairs,
            repo_root=tmp_path,
        )
