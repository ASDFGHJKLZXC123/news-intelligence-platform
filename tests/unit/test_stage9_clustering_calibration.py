"""Unit tests for the Stage 9 clustering (single-link cosine) calibration evaluator.

Entirely synthetic and offline: every observation is constructed in-test. Nothing here loads a gold
set, imports a gold loader, opens a holdout, or touches a database or the network -- one test scans
the evaluator's source for any such reference, and one installs a process audit hook to prove no
``final_holdout`` file is opened while a full sweep and the report run.

The evaluator selects nothing in production: no labeled clustering pair dataset exists, so the report
records ``not_verifiable`` and the live 0.80 threshold stays unchanged.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

from services.evaluation import clustering_calibration
from services.evaluation.calibration import (
    BinaryMetrics,
    CalibrationStatus,
    canonical_json_bytes,
    canonical_json_hash,
)
from services.evaluation.clustering_calibration import (
    LIVE_CLUSTERING_THRESHOLD,
    PROTOCOL_ID,
    ROADMAP_CLUSTERING_THRESHOLD,
    ClusteringCalibrationError,
    PairObservation,
    ThresholdEvaluation,
    clustering_grid,
    clustering_not_verifiable_report,
    evaluate_threshold,
    select_precision_knee,
    sweep,
)
from services.nlp import DEFAULT_CLUSTERING_THRESHOLD

_HASH = "a" * 64

# A separable synthetic pair set: five same-event pairs above every different-event pair.
_POS = [PairObservation(f"pos{i}", sim, True) for i, sim in enumerate((0.90, 0.91, 0.92, 0.93, 0.94))]
_NEG = [PairObservation(f"neg{i}", sim, False) for i, sim in enumerate((0.80, 0.81, 0.82, 0.83, 0.84))]


# --- a process audit hook that records file opens on demand --------------------------------
_OPENED: list[str] = []
_RECORDING = {"on": False}


def _audit_open(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _RECORDING["on"] and args:
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit_open)


def _ev(threshold: str, *, tp: int, fp: int, tn: int, fn: int) -> ThresholdEvaluation:
    return ThresholdEvaluation(Decimal(threshold), BinaryMetrics(tp=tp, fp=fp, tn=tn, fn=fn))


# --- the candidate grid ---------------------------------------------------------------------
def test_clustering_grid_is_51_exact_inclusive_points() -> None:
    grid = clustering_grid()
    assert len(grid) == 51
    assert grid[0] == Decimal("0.700")
    assert grid[-1] == Decimal("0.950")
    assert all(earlier < later for earlier, later in zip(grid, grid[1:], strict=False))
    assert all((point - Decimal("0.700")) % Decimal("0.005") == 0 for point in grid)


# --- PairObservation validation -------------------------------------------------------------
def test_pair_observation_accepts_boundary_similarities() -> None:
    for similarity in (-1.0, 0.0, 1.0, 1, -1):
        observation = PairObservation("pair", similarity, True)
        assert observation.same_event is True


@pytest.mark.parametrize("bad_id", ["", 0, None, 123, True])
def test_pair_observation_rejects_empty_or_non_string_id(bad_id: object) -> None:
    with pytest.raises(ClusteringCalibrationError):
        PairObservation(bad_id, 0.5, True)  # type: ignore[arg-type]


@pytest.mark.parametrize("blank_id", [" ", "   ", "\t", "\n", " \t\n "])
def test_pair_observation_rejects_whitespace_only_id(blank_id: str) -> None:
    with pytest.raises(ClusteringCalibrationError):
        PairObservation(blank_id, 0.5, True)


def test_pair_observation_preserves_a_legitimate_id_verbatim() -> None:
    # A valid id with surrounding whitespace is stored as-is, never stripped or rewritten.
    observation = PairObservation("  pair-7  ", 0.5, True)
    assert observation.pair_id == "  pair-7  "


@pytest.mark.parametrize(
    "bad_similarity",
    [float("nan"), float("inf"), float("-inf"), 1.5, -1.5, "0.5", True, None],
)
def test_pair_observation_rejects_non_finite_or_out_of_range_similarity(
    bad_similarity: object,
) -> None:
    with pytest.raises(ClusteringCalibrationError):
        PairObservation("pair", bad_similarity, True)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_label", [1, 0, "yes", None])
def test_pair_observation_rejects_non_bool_same_event(bad_label: object) -> None:
    with pytest.raises(ClusteringCalibrationError):
        PairObservation("pair", 0.5, bad_label)  # type: ignore[arg-type]


def test_collection_rejects_duplicate_pair_ids() -> None:
    duplicated = [PairObservation("dup", 0.9, True), PairObservation("dup", 0.8, False)]
    with pytest.raises(ClusteringCalibrationError):
        evaluate_threshold(duplicated, Decimal("0.85"))


def test_collection_rejects_empty_observations() -> None:
    with pytest.raises(ClusteringCalibrationError):
        evaluate_threshold([], Decimal("0.85"))
    with pytest.raises(ClusteringCalibrationError):
        sweep([])


# --- evaluate_threshold: confusion counts and inclusive boundary ----------------------------
def test_confusion_counts_at_a_clean_separating_threshold() -> None:
    evaluation = evaluate_threshold(_POS + _NEG, Decimal("0.85"))
    metrics = evaluation.metrics
    assert (metrics.tp, metrics.fp, metrics.fn, metrics.tn) == (5, 0, 0, 5)
    assert evaluation.threshold == Decimal("0.85")


def test_confusion_counts_when_a_lower_threshold_admits_negatives() -> None:
    metrics = evaluate_threshold(_POS + _NEG, Decimal("0.82")).metrics
    # every positive links; the 0.82/0.83/0.84 negatives cross the cutoff -> three false positives.
    assert (metrics.tp, metrics.fp, metrics.fn, metrics.tn) == (5, 3, 0, 2)


def test_prediction_is_inclusive_at_the_exact_threshold() -> None:
    boundary = [PairObservation("on", 0.85, True), PairObservation("below", 0.8499, False)]
    metrics = evaluate_threshold(boundary, Decimal("0.850")).metrics
    assert (metrics.tp, metrics.fp, metrics.fn, metrics.tn) == (1, 0, 0, 1)


def test_evaluate_threshold_rejects_float_and_bool_thresholds() -> None:
    with pytest.raises(ClusteringCalibrationError):
        evaluate_threshold(_POS + _NEG, 0.85)  # type: ignore[arg-type]
    with pytest.raises(ClusteringCalibrationError):
        evaluate_threshold(_POS + _NEG, True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "out_of_range",
    [
        Decimal("-0.001"),
        Decimal("1.001"),
        Decimal("-1"),
        Decimal("2"),
        "-0.001",
        "1.001",
        -1,
        2,
    ],
)
def test_evaluate_threshold_rejects_finite_thresholds_outside_unit_interval(
    out_of_range: object,
) -> None:
    with pytest.raises(ClusteringCalibrationError):
        evaluate_threshold(_POS + _NEG, out_of_range)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("boundary", "expected"),
    [
        (Decimal("0"), Decimal("0")),
        (Decimal("1"), Decimal("1")),
        (0, Decimal("0")),
        (1, Decimal("1")),
        ("0", Decimal("0")),
        ("1", Decimal("1")),
    ],
)
def test_evaluate_threshold_accepts_exact_unit_interval_boundaries(
    boundary: object, expected: Decimal
) -> None:
    evaluation = evaluate_threshold(_POS + _NEG, boundary)  # type: ignore[arg-type]
    assert evaluation.threshold == expected


# --- sweep ----------------------------------------------------------------------------------
def test_sweep_covers_the_whole_grid_in_ascending_threshold_order() -> None:
    evaluations = sweep(_POS + _NEG)
    assert len(evaluations) == 51
    assert [evaluation.threshold for evaluation in evaluations] == list(clustering_grid())
    assert all(a.threshold < b.threshold for a, b in zip(evaluations, evaluations[1:], strict=False))


# --- select_precision_knee: feasibility and every tie-break ---------------------------------
def test_zero_predicted_positives_is_infeasible_never_perfect() -> None:
    nothing = _ev("0.95", tp=0, fp=0, tn=5, fn=5)
    assert nothing.metrics.precision is None  # undefined, not 1.0
    assert nothing.is_feasible is False
    assert select_precision_knee([nothing]) is None


def test_knee_returns_none_when_no_threshold_is_feasible() -> None:
    low_precision = _ev("0.80", tp=1, fp=9, tn=0, fn=0)  # precision 0.1 < 0.90
    nothing = _ev("0.95", tp=0, fp=0, tn=5, fn=5)  # nothing predicted positive
    assert select_precision_knee([low_precision, nothing]) is None


def test_knee_prefers_higher_recall_first() -> None:
    lower_recall = _ev("0.90", tp=9, fp=1, tn=0, fn=1)  # precision 0.90, recall 0.90
    higher_recall = _ev("0.80", tp=18, fp=2, tn=0, fn=0)  # precision 0.90, recall 1.00
    assert select_precision_knee([lower_recall, higher_recall]) is higher_recall


def test_knee_breaks_a_recall_tie_by_f1() -> None:
    lower_f1 = _ev("0.90", tp=9, fp=1, tn=0, fn=1)  # recall 0.90, precision 0.90, f1 0.900
    higher_f1 = _ev("0.80", tp=9, fp=0, tn=0, fn=1)  # recall 0.90, precision 1.00, f1 ~0.947
    assert select_precision_knee([lower_f1, higher_f1]) is higher_f1


def test_knee_breaks_a_recall_and_f1_tie_by_proximity_to_080() -> None:
    near = _ev("0.80", tp=9, fp=0, tn=0, fn=1)
    far = _ev("0.90", tp=9, fp=0, tn=0, fn=1)  # identical metrics, farther from 0.80
    assert select_precision_knee([far, near]) is near


def test_knee_breaks_a_full_tie_by_the_higher_threshold() -> None:
    lower = _ev("0.75", tp=9, fp=0, tn=0, fn=1)  # |0.75 - 0.80| == 0.05
    higher = _ev("0.85", tp=9, fp=0, tn=0, fn=1)  # |0.85 - 0.80| == 0.05
    assert select_precision_knee([lower, higher]) is higher


def test_end_to_end_sweep_and_knee_pick_the_precision_knee() -> None:
    selected = select_precision_knee(sweep(_POS + _NEG))
    assert selected is not None
    # The lowest feasible threshold (precision 1.0, recall 1.0), closest to 0.80.
    assert selected.threshold == Decimal("0.845")
    assert selected.metrics.precision == 1.0
    assert selected.metrics.recall == 1.0


# --- the not-verifiable report --------------------------------------------------------------
def test_report_is_not_verifiable_and_leaves_the_threshold_unchanged() -> None:
    report = clustering_not_verifiable_report(_HASH)
    assert report["status"] == CalibrationStatus.NOT_VERIFIABLE.value == "not_verifiable"
    assert report["domain"] == "clustering"
    assert report["split"] == "development"
    assert report["protocol"] == {"id": PROTOCOL_ID, "sha256": _HASH}
    assert report["selected_parameters"] is None
    assert report["metrics"] is None
    assert report["input_hashes"] == {}
    effective = report["effective_parameters"]
    assert effective["clustering_threshold"] == LIVE_CLUSTERING_THRESHOLD == "0.80"
    assert effective["changed"] is False
    assert effective["roadmap_threshold"] == ROADMAP_CLUSTERING_THRESHOLD == "0.82"
    assert effective["roadmap_threshold_informational_only"] is True
    # The reported live threshold is the real production constant, not an invented value.
    assert float(effective["clustering_threshold"]) == DEFAULT_CLUSTERING_THRESHOLD


def test_report_makes_no_calibration_claim() -> None:
    report = clustering_not_verifiable_report(_HASH)
    text = canonical_json_bytes(report).decode("utf-8")
    assert "calibrated" not in text
    assert report["status"] == "not_verifiable"
    assert report["selected_parameters"] is None


def test_report_names_the_blocking_limitations() -> None:
    report = clustering_not_verifiable_report(_HASH)
    blockers = " ".join(report["blockers"]).lower()
    assert "pair" in blockers  # the missing ~100 same/different-event pairs
    assert "current" in blockers  # the unpinned embedding model version
    assert report["limitations"]


def test_report_describes_the_grid_but_runs_no_metrics() -> None:
    grid_info = clustering_not_verifiable_report(_HASH)["evaluated_grid"]
    assert grid_info["points"] == 51
    assert grid_info["metrics_run"] is False
    assert (grid_info["start"], grid_info["stop"], grid_info["step"]) == ("0.700", "0.950", "0.005")


def test_report_is_canonical_json_safe_and_deterministic() -> None:
    encoded = canonical_json_bytes(clustering_not_verifiable_report(_HASH))  # raises on Decimal/NaN
    assert encoded.endswith(b"\n")
    assert canonical_json_hash(clustering_not_verifiable_report(_HASH)) == canonical_json_hash(
        clustering_not_verifiable_report(_HASH)
    )


def test_report_normalizes_the_hash_and_rejects_malformed_hashes() -> None:
    assert clustering_not_verifiable_report("A" * 64)["protocol"]["sha256"] == "a" * 64
    for bad in ["", "xyz", "g" * 64, "a" * 63, "a" * 65, 123, None, True]:
        with pytest.raises(ClusteringCalibrationError):
            clustering_not_verifiable_report(bad)  # type: ignore[arg-type]


# --- source and runtime guards: never a holdout, never a gold loader ------------------------
def test_evaluator_source_references_no_holdout_or_gold_loader() -> None:
    source = Path(clustering_calibration.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "final_holdout",
        "load_corpus",
        "load_split",
        "load_tuning_corpus",
        "allow_holdout",
        "entity_linking_gold",
        "alert_episode_gold",
        "evaluation/gold",
    ):
        assert forbidden not in source


def test_sweep_and_report_open_no_holdout_or_gold_path() -> None:
    _OPENED.clear()
    _RECORDING["on"] = True
    try:
        select_precision_knee(sweep(_POS + _NEG))
        clustering_not_verifiable_report(_HASH)
    finally:
        _RECORDING["on"] = False
    assert not any(("final_holdout" in path or "evaluation/gold" in path) for path in _OPENED)
