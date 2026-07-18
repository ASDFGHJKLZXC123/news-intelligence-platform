"""Alert composite null-model gate evaluator (not-verifiable) for ``stage9-validation.v1`` §4.4.

``docs/evaluation/stage9-validation-protocol.md`` §4.4 fixes the *written* alert procedure: the
per-model decision-boundary grid ``inclusive_decimal_grid("0.10", "0.90", "0.01")`` (81 inclusive
points) is swept **only when** genuine outcome-blind candidate and baseline per-case prediction
artifacts exist; the selected fixed threshold is then evaluated per required window and the
candidate must strictly beat the baseline on precision *and* mean true-positive lead time in every
window. Those artifacts do not exist, so this module records status ``not_verifiable`` and runs
nothing: it enumerates the grid as exact ``Decimal``/string metadata but selects no threshold, and
the production decision boundary stays **0.50**.

It is a pure validate-and-report step. It loads only the tuning slice via
:func:`services.evaluation.alert_episode_gold.load_tuning_corpus` (train + development; the sealed
final holdout is excluded by construction), hashes by their exact bytes the three files that load
consumed (``manifest.json``, ``train.json``, ``development.json``) and the protocol document, and
reports the validated tuning label inventory. No database, no network, no LLM, no clock, no
randomness, and no fabricated prediction, score, or lead time.

**It fails closed exactly as the live service does.** The gate decision is derived from a real
:class:`~services.alerts.experimental.ExperimentalGate` with an **empty** evidence tuple -- never a
:class:`~services.alerts.experimental.WindowEvidence` synthesised from label counts -- so every
composite alert stays experimental (blocking all of ``REQUIRED_WINDOWS``) and the single-signal
carve-out stays released. Every §4.4 metric (candidate/baseline precision, recall, mean true-positive
lead time, and each per-window comparison) is reported ``null`` -- unavailable, never zero -- and the
blockers are recorded honestly: no candidate/baseline prediction artifact, no defined candidate
scorer, generic onset indicator keys that do not map to canonical ``CountrySignalPanel`` signals, a
mostly-company target set against a country baseline, and one-shot cases that cannot demonstrate
lead-time superiority. Severity mapping and raise/clear hysteresis (ADR 0010) are out of scope and
unchanged.
"""

from __future__ import annotations

import argparse
import inspect
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from services.alerts.experimental import (
    REQUIRED_WINDOWS,
    ExperimentalGate,
    ScoreBasis,
)
from services.crisis_model.baseline import BASELINE_MODEL_VERSION
from services.crisis_model.evaluation import evaluate_prediction
from services.crisis_model.signals import SIGNAL_DEFINITIONS
from services.evaluation.alert_episode_gold import (
    DATASET_ID,
    GOLD_ROOT,
    MANIFEST_FILE,
    REVIEW_STATE,
    SCHEMA_VERSION,
    SPLIT_FILES,
    TUNING_SPLITS,
    AlertEpisodeGoldCorpus,
    load_tuning_corpus,
)
from services.evaluation.calibration import (
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    inclusive_decimal_grid,
    sha256_file,
)

#: Protocol this evaluator obeys, and the version stamped on the report's policy metadata.
PROTOCOL_ID = "stage9-validation.v1"
DOMAIN = "alerts"
#: The report is written against development; train + development availability is described (§2).
REPORT_SPLIT = "development"

#: §4.4 per-model decision-boundary grid (decimal strings -- never floats -- for an exact sweep).
ALERT_GRID_START = "0.10"
ALERT_GRID_STOP = "0.90"
ALERT_GRID_STEP = "0.01"

#: The frozen production alert decision boundary, reported unchanged; §4.4 selects no threshold.
DECISION_BOUNDARY = "0.50"
#: The target type the baseline (a country-signal model) predicts against.
BASELINE_TARGET_TYPE = "country"

_REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DOC_PATH = _REPO_ROOT / "docs" / "evaluation" / "stage9-validation-protocol.md"
DEFAULT_OUTPUT_PATH = _REPO_ROOT / "evaluation" / "stage9" / "development" / "alerts.json"

#: Why every §4.4 metric is null: honesty, not omission.
_UNAVAILABLE_METRICS_REASON = (
    "no genuine outcome-blind candidate or baseline per-case prediction artifact (or longitudinal "
    "trajectory) exists offline and no candidate scorer is defined, so candidate/baseline precision, "
    "recall, mean true-positive lead time, and every per-window comparison cannot be computed here "
    "without fabricating predictions, scores, or lead times"
)


class AlertValidationError(CalibrationError):
    """An alert validation input is invalid. Raised before any report is emitted."""


def alert_decision_boundary_grid() -> tuple[Decimal, ...]:
    """The §4.4 grid: ``0.10..0.90`` step ``0.01`` -- 81 inclusive Decimal points (never executed)."""
    return inclusive_decimal_grid(ALERT_GRID_START, ALERT_GRID_STOP, ALERT_GRID_STEP)


def alert_input_paths(root: Path = GOLD_ROOT) -> tuple[Path, ...]:
    """The exact files ``load_tuning_corpus`` consumes: manifest, then the two tuning splits.

    Only the tuning-visible splits are ever named; the sealed split is excluded by construction and
    is never referenced, opened, hashed, or enumerated here.
    """
    root = Path(root)
    return (root / MANIFEST_FILE, *(root / SPLIT_FILES[split] for split in TUNING_SPLITS))


def _production_decision_boundary() -> float:
    """The live default alert decision boundary read from the production ``evaluate_prediction``."""
    return inspect.signature(evaluate_prediction).parameters["threshold"].default


def _assert_decision_boundary_unchanged() -> None:
    """Guard the one production constant this report asserts: the 0.50 decision boundary is frozen."""
    production = _production_decision_boundary()
    if production != 0.50 or float(DECISION_BOUNDARY) != production:
        raise AlertValidationError(
            f"the production alert decision boundary is {production!r}, not 0.50; "
            "stage9-validation.v1 freezes it and this not-verifiable report asserts it unchanged"
        )


def _gate_decisions() -> dict[str, Any]:
    """The current, real gate verdicts derived from an **empty** ExperimentalGate -- fail closed.

    Constructed with no evidence (the platform's actual state), the gate holds every composite alert
    experimental -- blocking exactly ``REQUIRED_WINDOWS`` -- and releases the single-signal carve-out.
    The blocking windows are read from the real decision, never hard-coded, and no ``WindowEvidence``
    is synthesised from label counts.
    """
    gate = ExperimentalGate()
    composite = gate.decide(ScoreBasis.COMPOSITE)
    single = gate.decide(ScoreBasis.SINGLE_SIGNAL)
    return {
        "evidence_count": len(gate.evidence),
        "required_windows": list(REQUIRED_WINDOWS),
        "composite": {
            "basis": ScoreBasis.COMPOSITE.value,
            "released": composite.released,
            "experimental": composite.experimental,
            "blocking_windows": list(composite.blocking_windows),
        },
        "single_signal": {
            "basis": ScoreBasis.SINGLE_SIGNAL.value,
            "released": single.released,
            "experimental": single.experimental,
            "blocking_windows": list(single.blocking_windows),
        },
        "description": (
            "the ExperimentalGate evidence is empty, so composite alerts stay experimental and the "
            "single-signal carve-out stays released -- the conservative default the service runs with"
        ),
    }


def _indicator_mapping_audit(corpus: AlertEpisodeGoldCorpus) -> dict[str, Any]:
    """Overlap of tuning-visible onset indicator keys with the canonical signal definitions.

    Derived only from the tuning slice and :data:`SIGNAL_DEFINITIONS`; it opens no other data and
    guesses no counts. The onset indicator keys are free-form observation handles, not
    ``CountrySignalPanel`` inputs, so the overlap is the honest measure of how many could be mapped.
    """
    canonical = set(SIGNAL_DEFINITIONS)
    keys = {indicator.key for case in corpus.cases for indicator in case.onset.indicators}
    mappable = sorted(keys & canonical)
    target_types = Counter(case.target.type for case in corpus.cases)
    return {
        "tuning_onset_indicator_keys": len(keys),
        "canonical_signal_definitions": len(canonical),
        "mappable_indicator_keys": len(mappable),
        "mappable_indicator_key_examples": mappable,
        "target_type_counts": dict(sorted(target_types.items())),
        "baseline_target_type": BASELINE_TARGET_TYPE,
        "description": (
            "onset indicator keys are free-form observation handles; the count that overlaps the "
            "canonical signal definitions is the most that could be honestly mapped to numeric "
            "CountrySignalPanel values -- the rest have no defined mapping"
        ),
    }


def _render_counts(counts: dict[str, int]) -> str:
    """A stable ``"25 company, 10 country"`` rendering of a count map, for a readable blocker line."""
    return ", ".join(f"{value} {key}" for key, value in sorted(counts.items()))


def _unavailable_metrics() -> dict[str, Any]:
    """The §4.4 metric schema, every value ``null``: measured only when real artifacts can be pinned."""
    empty = {"precision": None, "recall": None, "mean_true_positive_lead_time_days": None}
    per_window = [
        {
            "window": window,
            "candidate_precision": None,
            "baseline_precision": None,
            "candidate_mean_tp_lead_time_days": None,
            "baseline_mean_tp_lead_time_days": None,
            "precision_comparison": None,
            "lead_time_comparison": None,
        }
        for window in REQUIRED_WINDOWS
    ]
    return {
        "available": False,
        "reason": _UNAVAILABLE_METRICS_REASON,
        "candidate": dict(empty),
        "baseline": dict(empty),
        "per_window": per_window,
    }


def build_alert_validation_report(
    *, root: Path = GOLD_ROOT, protocol_path: Path = PROTOCOL_DOC_PATH
) -> dict[str, Any]:
    """Build the canonical §5 alert report: validate the tuning slice, hash inputs, report honestly.

    Loads only the tuning corpus (the sealed final holdout is excluded by construction), hashes the
    exact bytes of the three consumed inputs and the protocol document, and records the validated
    label inventory. No metric is computed, no grid is executed, and no parameter is selected: the
    0.50 decision boundary is retained unchanged and ``production_applied`` is ``false``.
    """
    _assert_decision_boundary_unchanged()
    corpus = load_tuning_corpus(root)
    inventory = corpus.label_inventory()
    grid = alert_decision_boundary_grid()
    paths = alert_input_paths(root)
    audit = _indicator_mapping_audit(corpus)
    return {
        "protocol": {"id": PROTOCOL_ID, "sha256": sha256_file(protocol_path)},
        "domain": DOMAIN,
        "status": CalibrationStatus.NOT_VERIFIABLE.value,
        "split": REPORT_SPLIT,
        "dataset": {"dataset_id": DATASET_ID, "schema_version": SCHEMA_VERSION},
        "input_hashes": {path.name: sha256_file(path) for path in paths},
        "model_versions": {
            "baseline_model": BASELINE_MODEL_VERSION,
            "candidate_model": None,
            "policy_version": PROTOCOL_ID,
        },
        "artifacts": {
            "candidate_prediction_artifact_present": False,
            "baseline_prediction_artifact_present": False,
            "candidate_scorer_defined": False,
        },
        "splits_available": dict(sorted(corpus.quotas.by_split.items())),
        "label_inventory": [item.as_dict() | {"window": item.window} for item in inventory],
        "evaluated_grid": {
            "start": ALERT_GRID_START,
            "stop": ALERT_GRID_STOP,
            "step": ALERT_GRID_STEP,
            "points": len(grid),
            "executed": False,
            "conditional": True,
            "description": (
                "the §4.4 per-model decision-boundary grid, usable only when genuine outcome-blind "
                "candidate and baseline per-case prediction artifacts exist; they do not, so it is "
                "enumerated as metadata and never run -- no threshold is selected"
            ),
        },
        "selected_parameters": {
            "selection": "none",
            "decision_boundary": DECISION_BOUNDARY,
            "decision_boundary_changed": False,
            "production_applied": False,
            "reason": (
                "no outcome-blind candidate/baseline prediction artifacts exist, so the grid is not "
                "run and the 0.50 production decision boundary is retained unchanged"
            ),
        },
        "gate_decision": _gate_decisions(),
        "severity_and_hysteresis": {
            "in_scope": False,
            "changed": False,
            "note": (
                "ADR 0010's severity mapping and raise/clear hysteresis bands are authoritative and "
                "out of scope for stage9-validation.v1; they are unchanged and untouched here"
            ),
        },
        "metrics": _unavailable_metrics(),
        "indicator_mapping_audit": audit,
        "blockers": [
            "no candidate prediction artifact exists: no outcome-blind per-case candidate "
            "predictions were produced without seeing the label, so nothing can be scored",
            "no baseline prediction artifact exists: the baseline model code is present, but no "
            "committed outcome-blind per-case baseline prediction (or longitudinal trajectory) does",
            "no candidate scorer is defined: there is no pinned candidate composite model to score "
            "the tuning cases with",
            "generic onset indicator keys cannot be honestly mapped to numeric CountrySignalPanel / "
            f"SIGNAL_DEFINITIONS values ({audit['mappable_indicator_keys']} of "
            f"{audit['tuning_onset_indicator_keys']} tuning onset indicator keys match the "
            f"{audit['canonical_signal_definitions']} canonical signal definitions)",
            "target mismatch: the tuning cases are mostly company-level "
            f"({_render_counts(audit['target_type_counts'])}) while the baseline is a "
            "country-signal model",
            "each case is a single as-of snapshot (one-shot per window); it cannot establish an "
            "earlier threshold crossing or demonstrate lead-time superiority over the baseline",
        ],
        "limitations": [
            f"the alert-episode labels are automated and unverified (review state {REVIEW_STATE!r}); "
            "no human or independent historical verification has happened",
            "no threshold was selected and no production value changed: the 0.50 decision boundary "
            "is retained unchanged and production_applied is false",
            "ADR 0010's severity mapping and raise/clear hysteresis bands are out of scope and "
            "unchanged; this report tunes neither",
            "the ExperimentalGate evidence stays empty, so every composite alert remains experimental "
            "and the single-signal carve-out remains released",
        ],
    }


def write_alert_validation_report(
    output_path: Path | str, *, root: Path = GOLD_ROOT, protocol_path: Path = PROTOCOL_DOC_PATH
) -> bytes:
    """Serialize the report to ``output_path`` as canonical bytes, refusing to overwrite.

    Refuses if the path already exists (a checked-in report is never silently overwritten), builds
    the report before touching the filesystem (so invalid inputs raise first), creates parent
    directories, and writes atomically (temp file then ``os.replace``). Returns the exact bytes.
    """
    path = Path(output_path)
    if path.exists():
        raise AlertValidationError(f"refusing to overwrite existing report at {path}")
    data = canonical_json_bytes(
        build_alert_validation_report(root=root, protocol_path=protocol_path)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def main(argv: list[str] | None = None) -> int:
    """CLI: write the canonical not-verifiable report (no clock, no randomness, no git revision)."""
    parser = argparse.ArgumentParser(
        description="Stage 9 alert composite null-model gate not-verifiable report."
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args(argv)
    data = write_alert_validation_report(args.output)
    print(f"wrote {len(data)} bytes to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ALERT_GRID_START",
    "ALERT_GRID_STEP",
    "ALERT_GRID_STOP",
    "BASELINE_TARGET_TYPE",
    "DECISION_BOUNDARY",
    "DOMAIN",
    "PROTOCOL_ID",
    "REPORT_SPLIT",
    "AlertValidationError",
    "alert_decision_boundary_grid",
    "alert_input_paths",
    "build_alert_validation_report",
    "write_alert_validation_report",
]
