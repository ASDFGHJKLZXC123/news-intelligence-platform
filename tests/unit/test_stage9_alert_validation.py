"""Unit tests for the Stage 9 alert composite null-model gate evaluator (§4.4).

Offline and deterministic. The evaluator loads only the tuning slice (train + development) via
``load_tuning_corpus``; it never opens the sealed final holdout, never touches a database or the
network, and fabricates no prediction, score, or lead time. One test scans the evaluator source for
any holdout / whole-corpus / DB / network / evidence-synthesis reference, and one installs a process
audit hook to prove no ``final_holdout`` file is opened while the report is built.

The evaluator selects nothing and changes nothing: no outcome-blind candidate/baseline prediction
artifacts exist, so the report records ``not_verifiable``, the 0.10..0.90 grid is enumerated but
never run, the 0.50 decision boundary stays unchanged, and the empty ExperimentalGate holds every
composite alert experimental while the single-signal carve-out stays released.
"""

from __future__ import annotations

import inspect
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from services.alerts.experimental import (
    REQUIRED_WINDOWS,
    ExperimentalGate,
    ScoreBasis,
)
from services.crisis_model.evaluation import evaluate_prediction
from services.evaluation import alert_validation
from services.evaluation.alert_episode_gold import GOLD_ROOT, SPLIT_COUNTS
from services.evaluation.alert_validation import (
    ALERT_GRID_START,
    ALERT_GRID_STEP,
    ALERT_GRID_STOP,
    DECISION_BOUNDARY,
    PROTOCOL_DOC_PATH,
    AlertValidationError,
    alert_decision_boundary_grid,
    alert_input_paths,
    build_alert_validation_report,
    write_alert_validation_report,
)
from services.evaluation.calibration import (
    CalibrationStatus,
    canonical_json_bytes,
    canonical_json_hash,
    sha256_file,
)

# --- a process audit hook that records file opens on demand --------------------------------
_OPENED: list[str] = []
_RECORDING = {"on": False}


def _audit_open(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _RECORDING["on"] and args:
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit_open)


# --- the conditional decision-boundary grid ------------------------------------------------
def test_grid_is_81_exact_inclusive_decimal_points() -> None:
    grid = alert_decision_boundary_grid()
    assert len(grid) == 81
    assert grid[0] == Decimal("0.10")
    assert grid[-1] == Decimal("0.90")
    assert (ALERT_GRID_START, ALERT_GRID_STOP, ALERT_GRID_STEP) == ("0.10", "0.90", "0.01")
    assert all(earlier < later for earlier, later in zip(grid, grid[1:], strict=False))
    assert all((point - Decimal("0.10")) % Decimal("0.01") == 0 for point in grid)


def test_report_describes_the_grid_but_never_executes_or_selects() -> None:
    grid_info = build_alert_validation_report()["evaluated_grid"]
    assert grid_info["points"] == 81
    assert grid_info["executed"] is False
    assert grid_info["conditional"] is True
    assert (grid_info["start"], grid_info["stop"], grid_info["step"]) == ("0.10", "0.90", "0.01")


def test_no_threshold_is_selected_and_the_boundary_is_unchanged() -> None:
    selected = build_alert_validation_report()["selected_parameters"]
    assert selected["selection"] == "none"
    assert selected["decision_boundary"] == DECISION_BOUNDARY == "0.50"
    assert selected["decision_boundary_changed"] is False
    assert selected["production_applied"] is False


# --- status, protocol, hashes, tuning-only counts ------------------------------------------
def test_report_is_not_verifiable_alerts_on_development() -> None:
    report = build_alert_validation_report()
    assert report["status"] == CalibrationStatus.NOT_VERIFIABLE.value == "not_verifiable"
    assert report["domain"] == "alerts"
    assert report["split"] == "development"


def test_input_and_protocol_hashes_are_the_exact_bytes_of_the_consumed_files() -> None:
    report = build_alert_validation_report()
    assert report["protocol"] == {
        "id": "stage9-validation.v1",
        "sha256": sha256_file(PROTOCOL_DOC_PATH),
    }
    assert report["input_hashes"] == {
        "manifest.json": sha256_file(GOLD_ROOT / "manifest.json"),
        "train.json": sha256_file(GOLD_ROOT / "train.json"),
        "development.json": sha256_file(GOLD_ROOT / "development.json"),
    }


def test_input_paths_are_tuning_only_never_the_holdout() -> None:
    names = [path.name for path in alert_input_paths()]
    assert names == ["manifest.json", "train.json", "development.json"]
    assert all("final_holdout" not in name for name in names)


def test_counts_are_the_tuning_slice_only_holdout_excluded() -> None:
    report = build_alert_validation_report()
    assert report["splits_available"] == {"development": 12, "train": 24}
    assert "final_holdout" not in report["splits_available"]
    inventory = report["label_inventory"]
    # Exactly the three ADR windows, 12 labeled tuning cases each (6 positive / 6 control) = 36.
    assert [item["window"] for item in inventory] == list(REQUIRED_WINDOWS)
    assert sum(item["labeled_cases"] for item in inventory) == SPLIT_COUNTS["train"] + SPLIT_COUNTS["development"] == 36
    for item in inventory:
        assert (item["labeled_cases"], item["positives"], item["controls"]) == (12, 6, 6)


# --- every metric is null / unavailable, never zero, never invented ------------------------
def test_all_candidate_and_baseline_metrics_are_null() -> None:
    metrics = build_alert_validation_report()["metrics"]
    assert metrics["available"] is False
    empty = {"precision": None, "recall": None, "mean_true_positive_lead_time_days": None}
    assert metrics["candidate"] == empty
    assert metrics["baseline"] == empty


def test_every_per_window_comparison_is_null() -> None:
    per_window = build_alert_validation_report()["metrics"]["per_window"]
    assert [row["window"] for row in per_window] == list(REQUIRED_WINDOWS)
    for row in per_window:
        assert row["candidate_precision"] is None
        assert row["baseline_precision"] is None
        assert row["candidate_mean_tp_lead_time_days"] is None
        assert row["baseline_mean_tp_lead_time_days"] is None
        assert row["precision_comparison"] is None
        assert row["lead_time_comparison"] is None


def test_no_metric_value_is_ever_zero() -> None:
    # Absence must read as null, never as a real 0 that could look like a measured failure.
    metrics = build_alert_validation_report()["metrics"]
    numeric = [*metrics["candidate"].values(), *metrics["baseline"].values()]
    for row in metrics["per_window"]:
        numeric.extend(v for k, v in row.items() if k != "window")
    assert all(value is None for value in numeric)


# --- fail-closed gate: empty ExperimentalGate, composite blocks, single-signal ships -------
def test_gate_decision_is_the_empty_experimental_gate_composite_fails_closed() -> None:
    gate_report = build_alert_validation_report()["gate_decision"]
    assert gate_report["evidence_count"] == 0
    composite = gate_report["composite"]
    assert composite["released"] is False
    assert composite["experimental"] is True
    # Blocks every one of the three required windows -- fail closed, no window silently passes.
    assert composite["blocking_windows"] == list(REQUIRED_WINDOWS)


def test_gate_decision_matches_a_freshly_constructed_empty_gate() -> None:
    # The report's verdict is the real gate's, not a hand-written stand-in.
    composite = ExperimentalGate().decide(ScoreBasis.COMPOSITE)
    single = ExperimentalGate().decide(ScoreBasis.SINGLE_SIGNAL)
    gate_report = build_alert_validation_report()["gate_decision"]
    assert gate_report["composite"]["blocking_windows"] == list(composite.blocking_windows)
    assert gate_report["composite"]["released"] == composite.released
    assert gate_report["single_signal"]["released"] == single.released


def test_single_signal_carve_out_stays_released() -> None:
    single = build_alert_validation_report()["gate_decision"]["single_signal"]
    assert single["released"] is True
    assert single["experimental"] is False
    assert single["blocking_windows"] == []


def test_the_report_never_synthesises_window_evidence() -> None:
    # A WindowEvidence built from label counts would fabricate the very numbers the gate scores;
    # the module may name the type in prose, but must never construct one.
    source = Path(alert_validation.__file__).read_text(encoding="utf-8")
    assert "WindowEvidence(" not in source


# --- the production decision boundary is genuinely 0.50 ------------------------------------
def test_production_evaluate_prediction_threshold_default_is_still_050() -> None:
    default = inspect.signature(evaluate_prediction).parameters["threshold"].default
    assert default == 0.50
    # The report's asserted-unchanged boundary is that real production constant.
    assert float(DECISION_BOUNDARY) == default


def test_build_raises_if_the_production_boundary_ever_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alert_validation, "DECISION_BOUNDARY", "0.55")
    with pytest.raises(AlertValidationError, match="decision boundary"):
        build_alert_validation_report()


# --- severity / hysteresis explicitly out of scope and unchanged ---------------------------
def test_severity_and_hysteresis_are_out_of_scope_and_unchanged() -> None:
    section = build_alert_validation_report()["severity_and_hysteresis"]
    assert section["in_scope"] is False
    assert section["changed"] is False
    note = section["note"].lower()
    assert "severity" in note and "hysteresis" in note


def test_evaluator_does_not_touch_severity_or_hysteresis_code() -> None:
    source = Path(alert_validation.__file__).read_text(encoding="utf-8")
    # The section may name severity/hysteresis in prose, but the actual band code
    # (services.alerts.hysteresis) is never imported or exercised here.
    for forbidden in (
        "services.alerts.hysteresis",
        "canonical_severity",
        "next_severity",
        "severity_rank",
        "HYSTERESIS_MARGIN",
        "SEVERITY_ORDER",
    ):
        assert forbidden not in source


# --- the honest blockers and automated-label caveat ----------------------------------------
def test_blockers_name_every_reason_the_backtest_cannot_run() -> None:
    blockers = " ".join(build_alert_validation_report()["blockers"]).lower()
    assert "candidate prediction artifact" in blockers
    assert "baseline prediction artifact" in blockers
    assert "candidate scorer" in blockers
    assert "countrysignalpanel" in blockers  # generic onset keys do not map to signal values
    assert "company" in blockers  # target mismatch vs the country baseline
    assert "one-shot" in blockers  # cannot establish lead-time superiority


def test_indicator_mapping_audit_is_derived_from_tuning_data_only() -> None:
    audit = build_alert_validation_report()["indicator_mapping_audit"]
    assert audit["canonical_signal_definitions"] == 22
    assert audit["tuning_onset_indicator_keys"] > 0
    assert audit["mappable_indicator_keys"] == 0  # zero honest overlap
    assert audit["mappable_indicator_key_examples"] == []
    assert audit["baseline_target_type"] == "country"
    assert "company" in audit["target_type_counts"]


def test_limitations_record_the_labels_as_automated_and_unverified() -> None:
    limitations = " ".join(build_alert_validation_report()["limitations"]).lower()
    assert "automated" in limitations
    assert "unchanged" in limitations
    assert "experimental" in limitations


# --- deterministic canonical report and overwrite refusal ----------------------------------
def test_report_is_canonical_json_safe_and_deterministic() -> None:
    encoded = canonical_json_bytes(build_alert_validation_report())  # raises on Decimal/NaN
    assert encoded.endswith(b"\n")
    assert canonical_json_hash(build_alert_validation_report()) == canonical_json_hash(
        build_alert_validation_report()
    )


def test_writer_creates_parents_and_returns_canonical_bytes(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "alerts.json"
    data = write_alert_validation_report(output)
    assert output.exists()
    assert data == canonical_json_bytes(build_alert_validation_report())
    assert output.read_bytes() == data


def test_writer_refuses_to_overwrite_an_existing_report(tmp_path: Path) -> None:
    output = tmp_path / "alerts.json"
    write_alert_validation_report(output)
    with pytest.raises(AlertValidationError, match="refusing to overwrite"):
        write_alert_validation_report(output)


# --- source and runtime guards: never a holdout, never a DB / network ----------------------
def test_evaluator_source_references_no_holdout_or_whole_corpus_or_db_or_network() -> None:
    source = Path(alert_validation.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "final_holdout",
        "allow_holdout",
        "load_corpus",  # the whole-dataset (holdout-including) loader
        "load_split",
        "SPLIT_FINAL_HOLDOUT",
        "sqlalchemy",
        "psycopg",
        "import requests",
        "httpx",
        "urllib.request",
        "datetime.now",
        "import random",
        "random.",
    ):
        assert forbidden not in source


def test_build_and_write_open_no_holdout_path(tmp_path: Path) -> None:
    _OPENED.clear()
    _RECORDING["on"] = True
    try:
        build_alert_validation_report()
        write_alert_validation_report(tmp_path / "alerts.json")
    finally:
        _RECORDING["on"] = False
    assert _OPENED  # the hook really observed file opens
    assert not any("final_holdout" in path for path in _OPENED)
