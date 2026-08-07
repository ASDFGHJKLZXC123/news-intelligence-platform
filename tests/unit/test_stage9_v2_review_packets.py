"""Tests for the blinded, deterministic Stage 9 v2 human-review packets."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from services.evaluation.calibration import (
    canonical_json_bytes,
    canonical_json_hash,
    sha256_file,
)
from services.evaluation.stage9_v2_review_packets import (
    ALERT_SOURCE_FILES,
    ANALOGY_GOLD_FILE,
    ENTITY_SOURCE_FILES,
    ENTITY_TARGETS_FILE,
    EPISODE_CATALOG_FILES,
    OUTPUT_ROOT,
    PACKET_FILENAMES,
    build_review_packets,
    write_review_packets,
)

_OPENED: list[str] = []
_RECORDING = {"active": False}


def _audit_open(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _RECORDING["active"] and args:
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit_open)


def _field_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            names.add(str(key))
            names.update(_field_names(child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for child in value:
            names.update(_field_names(child))
    return names


def _assert_review_slots(reviews: Mapping[str, Any]) -> None:
    assert set(reviews) == {"reviewer_a", "reviewer_b", "adjudication"}
    assert reviews["reviewer_a"] is not reviews["reviewer_b"]
    assert reviews["reviewer_a"]["reviewer_id"] is None
    assert reviews["reviewer_b"]["reviewer_id"] is None
    assert reviews["adjudication"]["adjudicator_id"] is None
    for slot in reviews.values():
        assert slot["decision"] is None
        assert slot["confidence"] is None
        assert slot["rationale"] is None
        assert slot["reviewed_on"] is None
        assert slot["evidence_source_ids"] == []


@pytest.fixture(scope="module")
def packets() -> dict[str, dict[str, Any]]:
    return build_review_packets()


def test_checked_in_packets_are_exact_canonical_builder_bytes(
    packets: dict[str, dict[str, Any]],
) -> None:
    assert tuple(sorted(packets)) == (
        "alerts.json",
        "analogy.json",
        "clustering.json",
        "entity-linking.json",
        "manifest.json",
    )
    for filename, packet in packets.items():
        assert (OUTPUT_ROOT / filename).read_bytes() == canonical_json_bytes(packet)


def test_generation_is_byte_deterministic_across_reruns_and_output_directories(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_review_packets(first)
    first_bytes = {path.name: path.read_bytes() for path in sorted(first.iterdir())}

    write_review_packets(first)
    rerun_bytes = {path.name: path.read_bytes() for path in sorted(first.iterdir())}
    write_review_packets(second)
    second_bytes = {path.name: path.read_bytes() for path in sorted(second.iterdir())}

    assert first_bytes == rerun_bytes == second_bytes
    for filename, raw in first_bytes.items():
        assert raw == canonical_json_bytes(json.loads(raw)), filename


def test_generator_never_opens_or_names_a_holdout() -> None:
    _OPENED.clear()
    _RECORDING["active"] = True
    try:
        built = build_review_packets()
    finally:
        _RECORDING["active"] = False

    assert _OPENED
    assert not [path for path in _OPENED if "holdout" in path.casefold()]
    for filename, packet in built.items():
        raw = canonical_json_bytes(packet).lower()
        assert b"holdout" not in raw, filename


def test_manifest_records_the_exact_hash_of_every_allowed_source(
    packets: dict[str, dict[str, Any]],
) -> None:
    expected_paths = (
        *ENTITY_SOURCE_FILES,
        ENTITY_TARGETS_FILE,
        *ALERT_SOURCE_FILES,
        ANALOGY_GOLD_FILE,
        *EPISODE_CATALOG_FILES,
    )
    expected = {
        path.relative_to(OUTPUT_ROOT.parents[2]).as_posix(): sha256_file(path)
        for path in expected_paths
    }
    manifest_sources = {
        source["path"]: source["sha256"] for source in packets["manifest.json"]["sources"]
    }
    assert manifest_sources == dict(sorted(expected.items()))
    assert list(manifest_sources) == sorted(manifest_sources)

    for filename in PACKET_FILENAMES:
        assert packets["manifest.json"]["artifacts"][filename]["sha256"] == sha256_file(
            OUTPUT_ROOT / filename
        )


def test_domain_counts_match_only_the_review_eligible_sources(
    packets: dict[str, dict[str, Any]],
) -> None:
    manifest = packets["manifest.json"]
    assert manifest["domains"] == {
        "alerts": {"artifact": "alerts.json", "tasks": 36},
        "analogy": {"artifact": "analogy.json", "tasks": 40},
        "clustering": {
            "artifact": "clustering.json",
            "collected_tasks": 0,
            "minimum_tasks": 100,
        },
        "entity_linking": {"artifact": "entity-linking.json", "tasks": 150},
    }
    assert packets["entity-linking.json"]["counts"] == {
        "candidate_entities": 27,
        "tasks": 150,
        "tasks_by_partition": {"development": 50, "train": 100},
    }
    assert packets["alerts.json"]["counts"] == {
        "tasks": 36,
        "tasks_by_partition": {"development": 12, "train": 24},
    }
    assert packets["analogy.json"]["counts"] == {
        "candidate_episodes": 100,
        "tasks": 40,
    }


def test_every_packet_marks_source_annotations_automated_and_untrusted(
    packets: dict[str, dict[str, Any]],
) -> None:
    for filename, packet in packets.items():
        assert packet["human_review_status"] == "not_started", filename
        assert packet["source_annotations"]["status"] == "automated_untrusted", filename
        assert packet["source_annotations"]["included_in_tasks"] is False, filename
        raw = canonical_json_bytes(packet).lower()
        assert b"human_reviewed" not in raw, filename
        assert b"human_signoff" not in raw, filename


def test_entity_tasks_are_blinded_and_use_opaque_candidate_handles(
    packets: dict[str, dict[str, Any]],
) -> None:
    packet = packets["entity-linking.json"]
    forbidden = {
        "article_key",
        "assertion_status",
        "case_tags",
        "expected_label",
        "expected_target_id",
        "fixture_uuid",
        "label",
        "leakage_group",
        "linked_target_ids",
        "mention_id",
        "note",
        "normalized_name",
        "provenance",
        "target_id",
    }
    assert forbidden.isdisjoint(_field_names(packet["tasks"]))
    assert forbidden.isdisjoint(_field_names(packet["candidate_catalog"]))

    candidate_ids = [candidate["candidate_id"] for candidate in packet["candidate_catalog"]]
    assert candidate_ids == [f"entity-candidate-{index:03d}" for index in range(1, 28)]
    task_ids = [task["task_id"] for task in packet["tasks"]]
    assert task_ids == [f"entity-linking-{index:04d}" for index in range(1, 151)]
    for task in packet["tasks"]:
        _assert_review_slots(task["reviews"])
        assert task["reviews"]["reviewer_a"]["selected_candidate_id"] is None
        assert task["reviews"]["reviewer_b"]["selected_candidate_id"] is None


def test_alert_tasks_exclude_labels_outcomes_notes_and_answer_coded_ids(
    packets: dict[str, dict[str, Any]],
) -> None:
    packet = packets["alerts.json"]
    forbidden = {
        "actual_end_date",
        "actual_start_date",
        "case_id",
        "classes",
        "control_rationale",
        "episode_group",
        "label",
        "label_available_on",
        "outcome",
        "provenance",
        "risk_type",
        "split",
        "supports",
    }
    for task in packet["tasks"]:
        assert forbidden.isdisjoint(_field_names(task["input"]))
        _assert_review_slots(task["reviews"])
        assert task["reviews"]["reviewer_a"]["event_start_date"] is None
        assert task["reviews"]["reviewer_a"]["risk_type"] is None
    assert [task["task_id"] for task in packet["tasks"]] == [
        f"alert-{index:04d}" for index in range(1, 37)
    ]


def test_alert_tasks_include_only_sources_cited_by_onset_indicators(
    packets: dict[str, dict[str, Any]],
) -> None:
    source_cases: dict[str, dict[str, Any]] = {}
    for path in ALERT_SOURCE_FILES:
        for case in json.loads(path.read_bytes())["cases"]:
            source_cases[canonical_json_hash(case)] = case

    for task in packets["alerts.json"]["tasks"]:
        source = source_cases[task["source_record_sha256"]]
        expected = {
            source_id
            for indicator in source["onset"]["indicators"]
            for source_id in indicator["source_ids"]
        }
        actual = {ref["id"] for ref in task["input"]["source_refs"]}
        assert actual == expected


def test_analogy_tasks_and_candidates_exclude_source_answers_and_outcomes(
    packets: dict[str, dict[str, Any]],
) -> None:
    packet = packets["analogy.json"]
    forbidden_task_fields = {
        "acceptable_episode_ids",
        "expected_episode_ids",
        "expects_counterexample",
        "notes",
        "pair_id",
    }
    forbidden_candidate_fields = {
        "end_date",
        "is_counterexample",
        "outcome_summary",
        "outcomes",
        "peak_date",
        "resolution_mechanism",
        "slug",
    }
    assert forbidden_task_fields.isdisjoint(_field_names(packet["tasks"]))
    assert forbidden_candidate_fields.isdisjoint(_field_names(packet["candidate_catalog"]))
    assert all("id" not in candidate for candidate in packet["candidate_catalog"])

    candidates = packet["candidate_catalog"]
    assert [candidate["candidate_id"] for candidate in candidates] == [
        f"episode-candidate-{index:03d}" for index in range(1, 101)
    ]
    for candidate in candidates:
        for indicator in candidate["onset_indicators"]:
            assert "label" not in indicator
            assert "description" in indicator
    for task in packet["tasks"]:
        _assert_review_slots(task["reviews"])
        assert task["reviews"]["reviewer_a"]["primary_candidate_ids"] is None
        assert task["reviews"]["reviewer_b"]["secondary_candidate_ids"] is None

    raw_gold = json.loads(ANALOGY_GOLD_FILE.read_bytes())
    answer_ids = {
        episode_id
        for pair in raw_gold["pairs"]
        for field in ("expected_episode_ids", "acceptable_episode_ids")
        for episode_id in pair[field]
    }
    serialized = canonical_json_bytes(packet).decode("utf-8")
    assert not [episode_id for episode_id in answer_ids if episode_id in serialized]


def test_analogy_candidates_include_only_sources_cited_by_onset_indicators(
    packets: dict[str, dict[str, Any]],
) -> None:
    source_episodes: dict[str, dict[str, Any]] = {}
    for path in EPISODE_CATALOG_FILES:
        for episode in json.loads(path.read_bytes())["episodes"]:
            source_episodes[canonical_json_hash(episode)] = episode

    for candidate in packets["analogy.json"]["candidate_catalog"]:
        source = source_episodes[candidate["source_record_sha256"]]
        expected = {indicator["source_id"] for indicator in source["onset_indicators"]}
        actual = {ref["id"] for ref in candidate["source_refs"]}
        assert actual == expected


def test_clustering_packet_requires_grouped_balanced_collection_and_dual_review(
    packets: dict[str, dict[str, Any]],
) -> None:
    packet = packets["clustering.json"]
    assert packet["status"] == "collection_required_no_existing_corpus"
    assert packet["sources"] == []
    assert packet["groups"] == []
    assert packet["counts"] == {
        "collected_groups": 0,
        "collected_pairs": 0,
        "minimum_groups": 20,
        "minimum_pairs": 100,
        "minimum_same_event_after_adjudication": 50,
        "minimum_different_event_after_adjudication": 50,
    }
    template = packet["group_schema_template"]
    assert template["group_id"] == "cluster-group-NNN"
    assert len(template["pairs"]) == 1
    _assert_review_slots(template["pairs"][0]["reviews"])


def test_all_task_ordering_and_source_fingerprints_are_stable_and_unique(
    packets: dict[str, dict[str, Any]],
) -> None:
    for filename in ("alerts.json", "analogy.json", "entity-linking.json"):
        tasks = packets[filename]["tasks"]
        fingerprints = [task["source_record_sha256"] for task in tasks]
        assert len(fingerprints) == len(set(fingerprints)), filename
        assert all(len(fingerprint) == 64 for fingerprint in fingerprints), filename
