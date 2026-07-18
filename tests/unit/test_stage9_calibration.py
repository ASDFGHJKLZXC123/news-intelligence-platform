"""Unit tests for the Stage 9 shared calibration primitives.

Entirely synthetic and offline: every input is constructed in-test or written under ``tmp_path``.
Nothing here loads a gold set, imports a gold loader, opens a holdout, or touches a database or the
network -- and one test installs a process audit hook to *prove* no ``final_holdout`` file is opened
while the primitives run.
"""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from services.evaluation import calibration
from services.evaluation.calibration import (
    MAX_GRID_POINTS,
    ArtifactVerificationError,
    BinaryMetrics,
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    canonical_json_hash,
    inclusive_decimal_grid,
    sha256_file,
    validate_status,
    verify_artifact_hash,
)

# --- a process audit hook that records file opens on demand --------------------------------
_OPENED_PATHS: list[str] = []
_RECORDING = {"on": False}


def _audit_open(event: str, args: tuple[Any, ...]) -> None:
    if event == "open" and _RECORDING["on"] and args:
        _OPENED_PATHS.append(str(args[0]))


sys.addaudithook(_audit_open)


# --- inclusive_decimal_grid: endpoints, order, the three protocol grids ---------------------
def test_clustering_grid_is_inclusive_and_exact() -> None:
    grid = inclusive_decimal_grid("0.700", "0.950", "0.005")
    assert len(grid) == 51
    assert grid[0] == Decimal("0.700")
    assert grid[-1] == Decimal("0.950")
    # Every point is an exact multiple of the step off the start -- no float drift.
    assert all((point - Decimal("0.700")) % Decimal("0.005") == 0 for point in grid)


def test_entity_and_alert_protocol_grids_have_the_documented_size() -> None:
    entity = inclusive_decimal_grid("0.000", "1.000", "0.005")
    assert len(entity) == 201
    assert entity[0] == Decimal("0") and entity[-1] == Decimal("1")

    alerts = inclusive_decimal_grid("0.10", "0.90", "0.01")
    assert len(alerts) == 81
    assert alerts[0] == Decimal("0.10") and alerts[-1] == Decimal("0.90")


def test_grid_is_strictly_increasing_and_stably_ordered() -> None:
    grid = inclusive_decimal_grid("0", "1", "0.25")
    assert grid == (Decimal("0"), Decimal("0.25"), Decimal("0.50"), Decimal("0.75"), Decimal("1"))
    assert list(grid) == sorted(grid)
    assert all(later > earlier for earlier, later in zip(grid, grid[1:], strict=False))


def test_grid_accepts_a_single_point_when_start_equals_stop() -> None:
    assert inclusive_decimal_grid("0.5", "0.5", "0.005") == (Decimal("0.5"),)


def test_grid_accepts_int_and_decimal_bounds() -> None:
    assert inclusive_decimal_grid(0, 2, 1) == (Decimal("0"), Decimal("1"), Decimal("2"))
    assert inclusive_decimal_grid(Decimal("0"), Decimal("1"), Decimal("0.5")) == (
        Decimal("0"),
        Decimal("0.5"),
        Decimal("1"),
    )


@pytest.mark.parametrize(
    ("start", "stop", "step"),
    [
        ("0.700", "0.950", "0"),  # zero step
        ("0.700", "0.950", "-0.005"),  # negative step
        ("0.950", "0.700", "0.005"),  # stop below start
        ("0.700", "0.951", "0.005"),  # endpoint not reachable -> not inclusive
    ],
)
def test_grid_rejects_malformed_bounds(start: str, stop: str, step: str) -> None:
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid(start, stop, step)


def test_grid_rejects_floats_to_stay_reproducible() -> None:
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid(0.7, "0.95", "0.005")  # a float bound would drift
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid("0.7", "0.95", 0.005)


def test_grid_rejects_bool_and_non_decimal_strings() -> None:
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid(True, "1", "0.5")
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid("0", "1", "not-a-number")


def test_grid_rejects_a_grid_above_the_point_cap() -> None:
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid("0", str(MAX_GRID_POINTS + 1), "1")


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity", "inf", "-inf"])
def test_grid_rejects_non_finite_decimal_strings(bad: str) -> None:
    # A non-finite bound must be a named CalibrationError, never a leaked decimal.InvalidOperation.
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid(bad, "1", "0.005")
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid("0", bad, "0.005")
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid("0", "1", bad)


@pytest.mark.parametrize(
    "bad", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_grid_rejects_non_finite_decimal_objects(bad: Decimal) -> None:
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid(bad, "1", "0.005")
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid("0", "1", bad)


@pytest.mark.parametrize(
    ("start", "stop", "step"),
    [
        ("0", "1", "1e-30"),  # quotient needs more digits than Decimal precision
        ("0", "1e20", "1e-20"),  # extreme magnitude spread
        ("0", "1e50", "1"),  # huge finite span over a unit step
    ],
)
def test_grid_rejects_extreme_precision_or_magnitude(start: str, stop: str, step: str) -> None:
    # These are finite but exceed deterministic Decimal precision at divmod; the raw
    # decimal.InvalidOperation/DivisionImpossible must surface as a CalibrationError instead.
    with pytest.raises(CalibrationError):
        inclusive_decimal_grid(start, stop, step)


# --- BinaryMetrics: normal case and every zero-denominator boundary -------------------------
def test_metrics_normal_case() -> None:
    metrics = BinaryMetrics(tp=8, fp=2, tn=5, fn=1)
    assert metrics.predicted_positives == 10
    assert metrics.actual_positives == 9
    assert metrics.actual_negatives == 7
    assert metrics.total == 16
    assert metrics.precision == pytest.approx(0.8)
    assert metrics.recall == pytest.approx(8 / 9)
    assert metrics.specificity == pytest.approx(5 / 7)
    assert metrics.f1 == pytest.approx(2 * 8 / (2 * 8 + 2 + 1))
    assert metrics.balanced_accuracy == pytest.approx((8 / 9 + 5 / 7) / 2)
    assert metrics.accuracy == pytest.approx(13 / 16)


def test_precision_is_none_not_one_when_nothing_predicted_positive() -> None:
    metrics = BinaryMetrics(tp=0, fp=0, tn=4, fn=3)
    assert metrics.precision is None  # the load-bearing rule: undefined, never 1.0
    assert metrics.recall == pytest.approx(0.0)  # 0 / 3 actual positives
    assert metrics.specificity == pytest.approx(1.0)
    assert metrics.f1 == pytest.approx(0.0)
    assert metrics.balanced_accuracy == pytest.approx(0.5)


def test_recall_and_balanced_accuracy_are_none_without_actual_positives() -> None:
    metrics = BinaryMetrics(tp=0, fp=1, tn=5, fn=0)
    assert metrics.recall is None
    assert metrics.precision == pytest.approx(0.0)
    assert metrics.specificity == pytest.approx(5 / 6)
    assert metrics.balanced_accuracy is None  # recall undefined -> mean undefined
    assert metrics.f1 == pytest.approx(0.0)


def test_empty_confusion_matrix_is_all_none() -> None:
    metrics = BinaryMetrics(tp=0, fp=0, tn=0, fn=0)
    for value in (
        metrics.precision,
        metrics.recall,
        metrics.specificity,
        metrics.f1,
        metrics.balanced_accuracy,
        metrics.accuracy,
    ):
        assert value is None
    assert metrics.total == 0


def test_metrics_as_dict_is_json_safe_and_carries_none() -> None:
    payload = BinaryMetrics(tp=0, fp=0, tn=1, fn=1).as_dict()
    assert payload["precision"] is None
    encoded = json.loads(json.dumps(payload))  # round-trips through JSON without error
    assert encoded["tp"] == 0 and encoded["precision"] is None


def test_metrics_from_predictions_tallies_and_validates() -> None:
    metrics = BinaryMetrics.from_predictions(
        predicted=[True, True, False, False],
        actual=[True, False, True, False],
    )
    assert (metrics.tp, metrics.fp, metrics.fn, metrics.tn) == (1, 1, 1, 1)
    with pytest.raises(CalibrationError):
        BinaryMetrics.from_predictions([True], [True, False])  # length mismatch
    with pytest.raises(CalibrationError):
        BinaryMetrics.from_predictions([1], [True])  # not a boolean


def test_metrics_reject_negative_and_boolean_counts() -> None:
    with pytest.raises(CalibrationError):
        BinaryMetrics(tp=-1, fp=0, tn=0, fn=0)
    with pytest.raises(CalibrationError):
        BinaryMetrics(tp=True, fp=0, tn=0, fn=0)  # bool is not an int count here


# --- canonical serialization and hashing ---------------------------------------------------
def test_canonical_json_is_order_independent_with_sorted_keys_and_newline() -> None:
    left = canonical_json_bytes({"b": 1, "a": 2})
    right = canonical_json_bytes({"a": 2, "b": 1})
    assert left == right
    assert left.endswith(b"\n")
    assert left == b'{"a":2,"b":1}\n'  # sorted keys, compact separators


def test_canonical_json_hash_is_deterministic() -> None:
    value = {"grid": ["0.700", "0.950"], "n": 51, "nested": {"z": 1, "a": [1, 2, 3]}}
    assert canonical_json_hash(value) == canonical_json_hash(value)
    assert canonical_json_hash(value) == hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def test_canonical_json_encodes_utf8_not_ascii_escapes() -> None:
    encoded = canonical_json_bytes({"name": "café"})
    assert "café".encode() in encoded  # real UTF-8 bytes, not a \\u escape
    assert b"\\u" not in encoded


def test_canonical_json_refuses_nan_and_non_serializable() -> None:
    with pytest.raises(CalibrationError):
        canonical_json_bytes({"x": float("nan")})
    with pytest.raises(CalibrationError):
        canonical_json_bytes({"x": {1, 2, 3}})  # a set is not JSON-serializable


# --- file hashing and frozen-artifact verification ------------------------------------------
def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    payload = canonical_json_bytes({"threshold": "0.80"})
    artifact.write_bytes(payload)
    assert sha256_file(artifact) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_raises_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError):
        sha256_file(tmp_path / "does-not-exist.json")


def test_verify_artifact_hash_success_and_case_insensitive(tmp_path: Path) -> None:
    artifact = tmp_path / "params.json"
    artifact.write_bytes(canonical_json_bytes({"accept": "0.85", "adjudicate": "0.50"}))
    digest = sha256_file(artifact)
    assert verify_artifact_hash(artifact, digest) == digest
    assert verify_artifact_hash(artifact, digest.upper()) == digest  # normalized


def test_verify_artifact_hash_raises_on_mismatch(tmp_path: Path) -> None:
    artifact = tmp_path / "params.json"
    artifact.write_bytes(canonical_json_bytes({"accept": "0.85"}))
    wrong = "0" * 64
    with pytest.raises(ArtifactVerificationError):
        verify_artifact_hash(artifact, wrong)


def test_verify_artifact_hash_rejects_a_malformed_expected_digest(tmp_path: Path) -> None:
    artifact = tmp_path / "params.json"
    artifact.write_bytes(canonical_json_bytes({"accept": "0.85"}))
    with pytest.raises(CalibrationError):
        verify_artifact_hash(artifact, "not-a-sha256")


def test_verify_artifact_hash_wraps_unreadable_file_as_artifact_error(tmp_path: Path) -> None:
    missing = tmp_path / "gone.json"
    # sha256_file keeps its standalone contract: the parent class, not the verification subclass.
    with pytest.raises(CalibrationError) as raw:
        sha256_file(missing)
    assert not isinstance(raw.value, ArtifactVerificationError)
    # verify_artifact_hash upgrades an unreadable/missing artifact to ArtifactVerificationError.
    with pytest.raises(ArtifactVerificationError):
        verify_artifact_hash(missing, "0" * 64)


# --- status vocabulary ----------------------------------------------------------------------
def test_status_vocabulary_is_exactly_the_four_protocol_values() -> None:
    assert {status.value for status in CalibrationStatus} == {
        "calibrated",
        "retained_insufficient_evidence",
        "evaluation_only",
        "not_verifiable",
    }
    for status in CalibrationStatus:
        assert validate_status(status.value) is status
    with pytest.raises(CalibrationError):
        validate_status("approved")


# --- the module never reaches gold data or a holdout ----------------------------------------
def test_calibration_module_is_domain_neutral() -> None:
    source = Path(calibration.__file__).read_text(encoding="utf-8")
    assert "final_holdout" not in source
    for forbidden in (
        "load_corpus",
        "load_split",
        "load_tuning_corpus",
        "entity_linking_gold",
        "alert_episode_gold",
        "evaluation/gold",
    ):
        assert forbidden not in source


def test_primitives_open_no_final_holdout_path(tmp_path: Path) -> None:
    artifact = tmp_path / "params.json"
    artifact.write_bytes(canonical_json_bytes({"threshold": "0.80"}))
    digest = sha256_file(artifact)  # written and hashed before recording begins

    _OPENED_PATHS.clear()
    _RECORDING["on"] = True
    try:
        inclusive_decimal_grid("0.700", "0.950", "0.005")
        BinaryMetrics(tp=1, fp=1, tn=1, fn=1).as_dict()
        canonical_json_hash({"a": 1, "b": [2, 3]})
        assert sha256_file(artifact) == digest
        assert verify_artifact_hash(artifact, digest) == digest
    finally:
        _RECORDING["on"] = False

    assert _OPENED_PATHS, "the audit hook should have observed the artifact being read"
    assert not any("final_holdout" in path for path in _OPENED_PATHS)
    assert all("evaluation/gold" not in path for path in _OPENED_PATHS)
