"""The historical alert-episode gold v1 contract: schema, loaders, validators (no DB, no network).

The committed gold data does not exist yet, so these tests build a *valid* 48-case dataset in
``tmp_path`` -- a manifest plus three split files that meet every window/split quota -- and assert
the loaders accept it, then feed the validator deliberately broken copies and assert it refuses each
one. A validator that only ever sees valid input is not evidence of anything. A separate cluster of
tests pins the Stage-9 surfaces: the threshold-free evaluation DTO, the per-window label inventory,
and the fact that loading the dataset never releases the null-model gate.
"""

from __future__ import annotations

import copy
import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import services.evaluation.alert_episode_gold as mod
from services.alerts.experimental import REQUIRED_WINDOWS, ExperimentalGate, ScoreBasis
from services.evaluation.alert_episode_gold import (
    CASES_PER_WINDOW,
    HORIZON_ORDER,
    SPLIT_COUNTS,
    SPLIT_FILES,
    SPLIT_ORDER,
    TOTAL_COUNT,
    WINDOW_BOUNDS,
    AlertEpisodeGoldValidationError,
    OutcomeEvaluationInputs,
    WindowLabelInventory,
    load_corpus,
    load_manifest,
    load_split,
    load_tuning_corpus,
)

Mutation = Callable[[dict[str, Any]], None]

#: as-of dates inside each ADR window; the whole case timeline hangs off these.
AS_OF = {
    "2007-2009": datetime.date(2007, 8, 15),
    "2020": datetime.date(2020, 3, 2),
    "2023": datetime.date(2023, 3, 6),
}
#: (positives, controls) per (split) block within a single window.
BLOCKS = {"train": (4, 4), "development": (2, 2), "final_holdout": (2, 2)}
RISK_CYCLE = ["banking", "company", "currency", "sovereign"]
TYPE_CYCLE = ["company", "country", "market", "region", "global"]
GEO_CYCLE = ["US", "GB", "DE", "FR", "JP", "CH", "EU", "GLOBAL"]

ONSET_TITLE = "Central bank money market operations review"
OUTCOME_TITLE = "Sovereign and banking outcome assessment note"


def _iso(day: datetime.date) -> str:
    return day.isoformat()


def _case(index: int, window: str, split: str, label: str) -> dict[str, Any]:
    as_of = AS_OF[window]
    # Cycle through all four canonical horizons so the corpus exercises every one-item horizon.
    horizon = HORIZON_ORDER[index % len(HORIZON_ORDER)]
    bounds = mod.horizon_bounds(as_of, horizon)
    end = bounds.upper  # evaluation_end must equal the selected bucket's (inclusive) upper bound.
    available = end + datetime.timedelta(days=30)
    observation_start = as_of - datetime.timedelta(days=60)
    obs_a = as_of - datetime.timedelta(days=30)
    obs_b = as_of - datetime.timedelta(days=10)
    positive = label == "positive"
    # A positive's onset must fall inside its selected bucket; the midpoint is safely interior for
    # both the closed head buckets and the half-open tail buckets.
    actual_start = bounds.lower + (bounds.upper - bounds.lower) // 2

    source_refs = [
        {
            "id": "src-01",
            "title": ONSET_TITLE,
            "publisher": "Bank for International Settlements",
            "url": "https://www.bis.org/publ/qtrpdf/r_qt.htm",
            "accessed": "2026-06-01",
            "kind": "institutional",
            "supports": ["onset"],
        },
        {
            "id": "src-02",
            "title": OUTCOME_TITLE,
            "publisher": "International Monetary Fund",
            "url": "https://www.imf.org/en/Publications",
            "accessed": "2026-06-01",
            "kind": "institutional",
            "supports": ["outcome" if positive else "control"],
        },
    ]
    onset = {
        "observation_start": _iso(observation_start),
        "summary": (
            "Funding conditions for the target tightened noticeably as short-term spreads widened "
            "and dealer balance sheets thinned; desk commentary in reference block "
            f"r{index:03d} flagged rollover strain into quarter end."
        ),
        "indicators": [
            {
                "key": "funding_spread_bps",
                "description": "Overnight secured funding spread widened relative to the trailing month.",
                "observed_on": _iso(obs_a),
                "value": 45,
                "unit": "basis_points",
                "source_ids": ["src-01"],
            },
            {
                "key": "repo_volume_index",
                "description": "Primary dealer repo volumes declined versus the prior quarter.",
                "observed_on": _iso(obs_b),
                "value": 82.5,
                "unit": "index",
                "source_ids": ["src-01"],
            },
        ],
    }
    if positive:
        outcome = {
            "occurred": True,
            "actual_start_date": _iso(actual_start),
            "actual_end_date": None,
            "summary": (
                "The targeted entity moved into acute distress and required extraordinary official "
                "support before conditions stabilised across the evaluation window."
            ),
            "classes": ["failure", "systemic_crisis"],
            "source_ids": ["src-02"],
        }
        rationale = None
    else:
        outcome = {
            "occurred": False,
            "actual_start_date": None,
            "actual_end_date": None,
            "summary": (
                "Funding strains eased over the following reporting periods and no disorderly "
                "outcome materialised through the evaluation window; the entity retained access."
            ),
            "classes": [],
            "source_ids": ["src-02"],
        }
        rationale = (
            "Elevated spreads at the observation point were absorbed by capital buffers and "
            "official backstops, so the alert condition never resolved through the window."
        )
    return {
        "case_id": f"case-{index:03d}",
        "split": split,
        "window": window,
        "episode_group": f"grp-{index:03d}",
        "label": label,
        "risk_type": RISK_CYCLE[index % len(RISK_CYCLE)],
        "target": {
            "type": TYPE_CYCLE[index % len(TYPE_CYCLE)],
            "id": f"tgt-{index:03d}",
            "name": f"Target Entity {index:02d}",
            "geography": GEO_CYCLE[index % len(GEO_CYCLE)],
        },
        "evaluation_as_of": _iso(as_of),
        "evaluation_horizons": [horizon],
        "evaluation_end": _iso(end),
        "label_available_on": _iso(available),
        "onset": onset,
        "outcome": outcome,
        "control_rationale": rationale,
        "source_refs": source_refs,
        "provenance": {
            "kind": "original_paraphrase",
            "created_on": "2026-07-01",
            "review_status": "automated_contract_validation_only",
        },
    }


def _dataset() -> dict[str, Any]:
    cases_by_split: dict[str, list[dict[str, Any]]] = {split: [] for split in SPLIT_ORDER}
    index = 0
    for window in REQUIRED_WINDOWS:
        for split in SPLIT_ORDER:
            positives, controls = BLOCKS[split]
            for k in range(positives + controls):
                label = "positive" if k < positives else "control"
                cases_by_split[split].append(_case(index, window, split, label))
                index += 1
    splits = {
        split: {
            "schema_version": "v1",
            "split": split,
            "cases": sorted(cases, key=lambda case: case["case_id"]),
        }
        for split, cases in cases_by_split.items()
    }
    manifest = {
        "dataset_id": "historical_alert_episodes",
        "schema_version": "v1",
        "windows": [
            {"name": window, "start": _iso(WINDOW_BOUNDS[window][0]), "end": _iso(WINDOW_BOUNDS[window][1])}
            for window in REQUIRED_WINDOWS
        ],
        "splits": [{"name": s, "file": SPLIT_FILES[s], "count": SPLIT_COUNTS[s]} for s in SPLIT_ORDER],
        "total_count": TOTAL_COUNT,
        "risk_types": sorted(set(RISK_CYCLE)),
        "horizons": list(HORIZON_ORDER),
        "metadata": {
            "created_on": "2026-07-01",
            "description": "Synthetic historical alert episodes for the ADR 0010 null-model backtest.",
            "provenance": "Original paraphrase drafted for automated contract validation.",
        },
        "labeling": {
            "positive_rule": "The risk materialised inside the case's one selected horizon bucket: outcome.occurred is true and actual_start_date falls in that bucket.",
            "control_rule": "A near-miss with no target outcome inside the selected horizon bucket through evaluation_end.",
            "label_timing": "Labels are known only on label_available_on, on or after evaluation_end.",
            "onset_outcome_separation": "Onset holds only as-of-observable evidence; outcome is future.",
            "controls": "Controls carry a substantive rationale and control-supporting sources.",
            "lead_time": "Lead time is computed downstream from actual_start_date; not stored here.",
        },
        "source_policy": "Only institutional, primary or statistical sources with resolvable https URLs.",
        "license": {"id": "CC-BY-4.0", "note": "Original paraphrase; no copyrighted excerpts."},
        "review": {
            "state": "automated_contract_validation_only",
            "human_verified": False,
            "independent_historical_verification": False,
        },
    }
    return {"manifest": manifest, "splits": splits}


def _materialize(tmp_path: Path, mutate: Mutation | None = None) -> Path:
    data = copy.deepcopy(_dataset())
    if mutate is not None:
        mutate(data)
    (tmp_path / "manifest.json").write_text(json.dumps(data["manifest"]), encoding="utf-8")
    for name, payload in data["splits"].items():
        (tmp_path / SPLIT_FILES[name]).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _cases(data: dict[str, Any], split: str) -> list[dict[str, Any]]:
    return data["splits"][split]["cases"]


def _first_positive(data: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in _cases(data, "train") if c["label"] == "positive")


def _first_control(data: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in _cases(data, "train") if c["label"] == "control")


def _problems(tmp_path: Path, mutate: Mutation) -> tuple[str, ...]:
    with pytest.raises(AlertEpisodeGoldValidationError) as excinfo:
        load_corpus(_materialize(tmp_path, mutate))
    return excinfo.value.problems


# --- the valid dataset loads ---------------------------------------------------------------
def test_valid_corpus_loads_and_reports(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    assert corpus.quotas.total == TOTAL_COUNT == 48
    assert corpus.includes_holdout is True
    assert corpus.quotas.positives == 24 and corpus.quotas.controls == 24
    assert set(corpus.quotas.horizons) == set(HORIZON_ORDER)
    assert corpus.quotas.risk_types >= 3
    assert corpus.quotas.targets >= 6 and corpus.quotas.geographies >= 6
    for window in REQUIRED_WINDOWS:
        assert len(corpus.cases_for_window(window)) == CASES_PER_WINDOW == 16
    assert corpus.report()["dataset_id"] == "historical_alert_episodes"


def test_manifest_loads_with_windows_and_review(tmp_path):
    manifest = load_manifest(_materialize(tmp_path))
    assert [name for name, _, _ in manifest.windows] == list(REQUIRED_WINDOWS)
    assert [name for name, _, _ in manifest.splits] == list(SPLIT_ORDER)
    assert manifest.horizons == HORIZON_ORDER
    assert manifest.review["human_verified"] is False


def test_split_load_and_holdout_is_gated(tmp_path):
    root = _materialize(tmp_path)
    assert len(load_split(root, "train")) == SPLIT_COUNTS["train"] == 24
    with pytest.raises(AlertEpisodeGoldValidationError, match="gated"):
        load_split(root, "final_holdout")
    opted_in = load_split(root, "final_holdout", allow_holdout=True)
    assert len(opted_in) == SPLIT_COUNTS["final_holdout"] == 12


def test_tuning_corpus_excludes_holdout(tmp_path):
    corpus = load_tuning_corpus(_materialize(tmp_path))
    assert corpus.includes_holdout is False
    assert len(corpus.cases) == SPLIT_COUNTS["train"] + SPLIT_COUNTS["development"] == 36
    assert not corpus.cases_for("final_holdout")


# --- Stage-9 surfaces: threshold-free DTO, inventory, and a gate that stays shut -----------
def test_evaluation_inputs_are_threshold_free(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    positive = next(c for c in corpus.cases if c.is_positive)
    inputs = positive.evaluation_inputs(positive.selected_horizon)
    assert isinstance(inputs, OutcomeEvaluationInputs)
    assert inputs.horizon == positive.selected_horizon
    assert inputs.evaluation_date == positive.evaluation_end
    assert inputs.actual_outcome is True
    assert inputs.actual_start_date == positive.outcome.actual_start_date
    assert set(inputs.as_kwargs()) == {"horizon", "evaluation_date", "actual_outcome", "actual_start_date"}
    assert "threshold" not in inputs.as_kwargs()
    # The DTO's horizon defaults to the case's own selected horizon, and the outcome sits in-bucket.
    assert positive.evaluation_inputs().horizon == positive.selected_horizon
    assert positive.evaluation_end == positive.selected_bounds.upper
    assert positive.selected_bounds.contains(positive.outcome.actual_start_date)

    control = next(c for c in corpus.cases if not c.is_positive)
    control_inputs = control.evaluation_inputs(control.selected_horizon)
    assert control_inputs.actual_outcome is False
    assert control_inputs.actual_start_date is None
    with pytest.raises(AlertEpisodeGoldValidationError):
        positive.evaluation_inputs("nonexistent")


def test_label_inventory_is_counts_only(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    inventory = corpus.label_inventory()
    assert [item.window for item in inventory] == list(REQUIRED_WINDOWS)
    for item in inventory:
        assert isinstance(item, WindowLabelInventory)
        assert item.labeled_cases == 16
        assert item.positives == 8 and item.controls == 8
        # Inventory names what to backtest; it never claims a measured result.
        assert not hasattr(item, "candidate_precision")
        assert not hasattr(item, "baseline_precision")
        assert not hasattr(item, "lead_time_days")


def test_dataset_module_never_releases_the_gate(tmp_path):
    # Loading the dataset must not construct evidence or a gate, nor flip it open.
    load_corpus(_materialize(tmp_path))
    assert not hasattr(mod, "ExperimentalGate")
    assert not hasattr(mod, "WindowEvidence")
    composite = ExperimentalGate().decide(ScoreBasis.COMPOSITE)
    assert composite.released is False
    assert composite.experimental is True
    assert ExperimentalGate().decide(ScoreBasis.SINGLE_SIGNAL).released is True


# --- the validator refuses broken data -----------------------------------------------------
def _mutations() -> list[tuple[str, Mutation, str]]:
    return [
        # structure / types
        ("unknown case field", lambda d: _first_positive(d).__setitem__("bogus", 1), "unknown field"),
        ("missing case field", lambda d: _first_positive(d).pop("onset"), "missing field"),
        ("non-date as_of", lambda d: _first_positive(d).__setitem__("evaluation_as_of", 20070815), "expected an ISO date string"),
        ("dropped case", lambda d: _cases(d, "train").pop(), "expected"),
        ("unsorted cases", lambda d: _cases(d, "train").reverse(), "sorted by case_id"),
        # manifest
        ("bad dataset id", lambda d: d["manifest"].__setitem__("dataset_id", "wrong"), "dataset_id must be"),
        ("windows out of order", lambda d: d["manifest"]["windows"].reverse(), "must be exactly"),
        ("bad window bounds", lambda d: d["manifest"]["windows"][0].__setitem__("start", "2006-01-01"), "bounds must be"),
        ("bad split count", lambda d: d["manifest"]["splits"][0].__setitem__("count", 99), "count must be"),
        ("horizons wrong", lambda d: d["manifest"].__setitem__("horizons", ["6_12m", "0_6m"]), "canonical order"),
        ("license not paraphrase", lambda d: d["manifest"]["license"].__setitem__("note", "All rights reserved."), "original paraphrase"),
        ("review human claim", lambda d: d["manifest"]["review"].__setitem__("human_verified", True), "must be false"),
        # enums / vocab
        ("bad risk type", lambda d: _first_positive(d).__setitem__("risk_type", "meltdown"), "not a canonical RiskType"),
        ("bad label", lambda d: _first_positive(d).__setitem__("label", "maybe"), "label: must be one of"),
        ("bad target type", lambda d: _first_positive(d)["target"].__setitem__("type", "bank"), "target.type: must be one of"),
        ("bad geography", lambda d: _first_positive(d)["target"].__setitem__("geography", "usa"), "target.geography"),
        ("bad horizon", lambda d: _first_positive(d).__setitem__("evaluation_horizons", ["bogus"]), "unknown value"),
        ("empty horizons", lambda d: _first_positive(d).__setitem__("evaluation_horizons", []), "exactly one canonical evaluation horizon"),
        ("bad source kind", lambda d: _first_positive(d)["source_refs"][0].__setitem__("kind", "blog"), "kind: must be one of"),
        ("empty supports", lambda d: _first_positive(d)["source_refs"][0].__setitem__("supports", []), "at least 1"),
        # positive / control consistency
        ("positive not occurred", lambda d: _first_positive(d)["outcome"].__setitem__("occurred", False), "positive iff occurred"),
        ("positive no start", lambda d: _first_positive(d)["outcome"].__setitem__("actual_start_date", None), "requires an actual start date"),
        ("positive start out of range", lambda d: _first_positive(d)["outcome"].__setitem__("actual_start_date", "2006-01-01"), "must fall within"),
        ("control occurred", lambda d: _first_control(d)["outcome"].__setitem__("occurred", True), "positive iff occurred"),
        ("control with classes", lambda d: _first_control(d)["outcome"].__setitem__("classes", ["failure"]), "must have no outcome classes"),
        ("control with dates", lambda d: _first_control(d)["outcome"].__setitem__("actual_start_date", "2007-09-01"), "must have null"),
        ("control no rationale", lambda d: _first_control(d).__setitem__("control_rationale", None), "control_rationale"),
        ("positive with rationale", lambda d: _first_positive(d).__setitem__("control_rationale", "should be null here"), "must be null"),
        # timing / label availability
        ("end before as_of", lambda d: _first_positive(d).__setitem__("evaluation_end", "2007-01-01"), "must be after evaluation_as_of"),
        ("end not equal to bucket upper", lambda d: _first_positive(d).__setitem__("evaluation_end", "2007-09-04"), "must equal the selected horizon"),
        ("as_of outside window", lambda d: _first_positive(d).__setitem__("evaluation_as_of", "2011-01-01"), "outside window"),
        ("label leaks to inputs", lambda d: _first_positive(d).__setitem__("label_available_on", "2007-09-01"), "labels leak to inputs"),
        ("onset after as_of", lambda d: _first_positive(d)["onset"].__setitem__("observation_start", "2007-09-01"), "on or before evaluation_as_of"),
        ("indicator after as_of", lambda d: _first_positive(d)["onset"]["indicators"][0].__setitem__("observed_on", "2007-09-01"), "on or before evaluation_as_of"),
        ("too few indicators", lambda d: _first_positive(d)["onset"]["indicators"].pop(), "at least 2 signals"),
        # source support / provenance
        ("onset source no support", lambda d: _first_positive(d)["source_refs"][0].__setitem__("supports", ["outcome"]), "does not declare 'onset' support"),
        ("indicator source unresolved", lambda d: _first_positive(d)["onset"]["indicators"][0].__setitem__("source_ids", ["src-99"]), "does not resolve"),
        ("fake url", lambda d: _first_positive(d)["source_refs"][0].__setitem__("url", "https://example.com/x"), "placeholder/fake host"),
        ("non-https url", lambda d: _first_positive(d)["source_refs"][0].__setitem__("url", "http://www.bis.org/x"), "must be https"),
        ("provenance kind", lambda d: _first_positive(d)["provenance"].__setitem__("kind", "scraped"), "provenance.kind"),
        ("human review claim", lambda d: _first_positive(d)["provenance"].__setitem__("review_status", "human_reviewed"), "no human-review claim"),
        # onset leakage
        ("long quote in onset", lambda d: _first_positive(d)["onset"].__setitem__("summary", 'Funding stress was noted and a source said "' + "a" * 260 + '" at quarter end.'), "quoted run"),
        ("future year in onset", lambda d: _first_positive(d)["onset"].__setitem__("summary", "Funding conditions tightened as spreads widened well before the 2011 reporting cycle closed."), "later than the evaluation_as_of year"),
        ("hindsight in onset", lambda d: _first_positive(d)["onset"].__setitem__("summary", "Funding conditions tightened and the target would eventually see spreads widen across desks."), "hindsight phrase"),
        ("outcome date in onset", _outcome_date_in_onset, "leaks the outcome date"),
        ("outcome class in onset", lambda d: _first_positive(d)["onset"].__setitem__("summary", "Funding conditions tightened as a systemic crisis narrative built while spreads widened at desks."), "outcome class"),
        ("source title in onset", lambda d: _first_positive(d)["onset"].__setitem__("summary", f"Funding tightened; see the {ONSET_TITLE} for the widening spreads noted across dealer desks."), "repeats a source title"),
        ("outcome id in onset key", lambda d: _first_positive(d)["onset"]["indicators"][0].__setitem__("key", "systemic_crisis_watch"), "must not embed an outcome/source id"),
        ("source id in onset key", lambda d: _first_positive(d)["onset"]["indicators"][0].__setitem__("key", "spread_src_01"), "must not embed an outcome/source id"),
        ("bad indicator key", lambda d: _first_positive(d)["onset"]["indicators"][0].__setitem__("key", "BadKey"), "snake_case"),
        # cross-case leakage / uniqueness / quotas
        ("duplicate case id", lambda d: _cases(d, "development")[0].__setitem__("case_id", "case-000"), "duplicate case_id"),
        ("duplicate onset fingerprint", lambda d: _cases(d, "development")[0]["onset"].__setitem__("summary", _cases(d, "train")[0]["onset"]["summary"]), "onset-summary fingerprint"),
        ("cross-split episode group", lambda d: _cases(d, "development")[0].__setitem__("episode_group", _cases(d, "train")[0]["episode_group"]), "spans splits"),
        ("cross-split overlap", _overlap, "overlap on"),
        ("window count wrong", lambda d: _cases(d, "train")[0].__setitem__("window", "2020"), "expected exactly"),
        ("split window coverage", _drop_controls_in_first_window, "at least one positive and one control"),
    ]


def _overlap(data: dict[str, Any]) -> None:
    train = _cases(data, "train")[0]
    dev = _cases(data, "development")[0]
    dev["risk_type"] = train["risk_type"]
    dev["target"]["id"] = train["target"]["id"]
    dev["evaluation_as_of"] = train["evaluation_as_of"]
    dev["evaluation_end"] = train["evaluation_end"]


def _outcome_date_in_onset(data: dict[str, Any]) -> None:
    # Reference this case's *own* realised outcome date so the leak is exact regardless of horizon.
    case = _first_positive(data)
    outcome_date = case["outcome"]["actual_start_date"]
    case["onset"]["summary"] = (
        f"Funding conditions tightened notably around the {outcome_date} observation window as "
        "short-term spreads widened across dealer desks into quarter end."
    )


def _drop_controls_in_first_window(data: dict[str, Any]) -> None:
    for case in _cases(data, "train"):
        if case["window"] == "2007-2009" and case["label"] == "control":
            case["label"] = "positive"


_MUTATIONS = _mutations()


@pytest.mark.parametrize("name,mutate,expected", _MUTATIONS, ids=[m[0] for m in _MUTATIONS])
def test_validator_rejects(tmp_path, name, mutate, expected):
    problems = _problems(tmp_path, mutate)
    assert any(expected in problem for problem in problems), (name, problems)


# --- the single-horizon bucket contract (the Stage-8-item-4 repair) ------------------------
def _reconfigure_positive(case: dict[str, Any], horizon: str, start: datetime.date) -> None:
    """Retarget a positive case onto ``horizon`` with a chosen onset date, keeping end/available valid."""
    as_of = datetime.date.fromisoformat(case["evaluation_as_of"])
    bounds = mod.horizon_bounds(as_of, horizon)
    case["evaluation_horizons"] = [horizon]
    case["evaluation_end"] = _iso(bounds.upper)
    case["label_available_on"] = _iso(bounds.upper + datetime.timedelta(days=30))
    case["outcome"]["actual_start_date"] = _iso(start)


def _boundary_dates(as_of: datetime.date) -> tuple[datetime.date, datetime.date, datetime.date]:
    """The three shared bucket boundaries off ``as_of``: as_of+6m, as_of+12m, as_of+18m."""
    return (
        mod.horizon_bounds(as_of, "0_6m").upper,
        mod.horizon_bounds(as_of, "6_12m").upper,
        mod.horizon_bounds(as_of, "12_18m").upper,
    )


def test_horizon_bounds_have_the_declared_inclusivity():
    as_of = datetime.date(2007, 8, 15)
    plus6, plus12, plus18 = _boundary_dates(as_of)
    # 0_6m is closed on both ends; +6m is inside it but is the *excluded* lower edge of 6_12m.
    assert mod.horizon_bounds(as_of, "0_6m").contains(as_of)
    assert mod.horizon_bounds(as_of, "0_6m").contains(plus6)
    assert not mod.horizon_bounds(as_of, "6_12m").contains(plus6)
    assert mod.horizon_bounds(as_of, "6_12m").contains(plus12)
    assert not mod.horizon_bounds(as_of, "12_18m").contains(plus12)
    assert mod.horizon_bounds(as_of, "12_18m").contains(plus18)
    assert mod.horizon_bounds(as_of, "within_18m").contains(as_of)
    assert mod.horizon_bounds(as_of, "within_18m").contains(plus18)
    with pytest.raises(AlertEpisodeGoldValidationError):
        mod.horizon_bounds(as_of, "bogus")


def test_fixture_cycles_all_one_item_horizons(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    seen: set[str] = set()
    for case in corpus.cases:
        assert len(case.evaluation_horizons) == 1
        seen.add(case.selected_horizon)
        assert case.evaluation_end == case.selected_bounds.upper
        if case.is_positive:
            assert case.selected_bounds.contains(case.outcome.actual_start_date)
        else:
            assert case.outcome.actual_start_date is None
    assert seen == set(HORIZON_ORDER)


def test_multi_horizon_case_rejected(tmp_path):
    problems = _problems(tmp_path, lambda d: _first_positive(d).__setitem__("evaluation_horizons", ["0_6m", "6_12m"]))
    assert any("exactly one canonical evaluation horizon" in p for p in problems), problems


@pytest.mark.parametrize(
    "horizon,at",
    [
        ("0_6m", "plus6"),        # +6m is the inclusive upper bound of 0_6m
        ("6_12m", "plus12"),      # +12m is the inclusive upper bound of 6_12m
        ("12_18m", "plus18"),     # +18m is the inclusive upper bound of 12_18m
        ("within_18m", "plus18"),  # +18m is the inclusive upper bound of within_18m
    ],
)
def test_positive_at_bucket_upper_bound_is_accepted(tmp_path, horizon, at):
    def mutate(data: dict[str, Any]) -> None:
        case = _first_positive(data)
        as_of = datetime.date.fromisoformat(case["evaluation_as_of"])
        start = dict(zip(("plus6", "plus12", "plus18"), _boundary_dates(as_of), strict=True))[at]
        _reconfigure_positive(case, horizon, start)

    corpus = load_corpus(_materialize(tmp_path, mutate))
    assert corpus.quotas.total == TOTAL_COUNT


@pytest.mark.parametrize(
    "name,horizon,offset",
    [
        ("6_12m rejects a start before its excluded lower bound", "6_12m", "before6"),
        ("6_12m rejects a start exactly at its excluded +6m edge", "6_12m", "plus6"),
        ("12_18m rejects a start exactly at its excluded +12m edge", "12_18m", "plus12"),
    ],
)
def test_positive_outside_selected_bucket_rejected(tmp_path, name, horizon, offset):
    def mutate(data: dict[str, Any]) -> None:
        case = _first_positive(data)
        as_of = datetime.date.fromisoformat(case["evaluation_as_of"])
        plus6, plus12, _plus18 = _boundary_dates(as_of)
        start = {
            "before6": plus6 - datetime.timedelta(days=30),
            "plus6": plus6,
            "plus12": plus12,
        }[offset]
        _reconfigure_positive(case, horizon, start)

    problems = _problems(tmp_path, mutate)
    assert any("must fall within the selected" in p for p in problems), (name, problems)


def _shift_end(days: int) -> Mutation:
    def mutate(data: dict[str, Any]) -> None:
        case = _first_positive(data)
        as_of = datetime.date.fromisoformat(case["evaluation_as_of"])
        upper = mod.horizon_bounds(as_of, case["evaluation_horizons"][0]).upper
        case["evaluation_end"] = _iso(upper + datetime.timedelta(days=days))

    return mutate


@pytest.mark.parametrize("days", [-1, 1], ids=["one day early", "one day late"])
def test_evaluation_end_off_by_one_rejected(tmp_path, days):
    problems = _problems(tmp_path, _shift_end(days))
    assert any("must equal the selected horizon" in p for p in problems), problems


def test_dto_refuses_a_non_selected_horizon(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    case = corpus.cases[0]
    assert case.evaluation_inputs(case.selected_horizon).horizon == case.selected_horizon
    assert case.evaluation_inputs().horizon == case.selected_horizon
    other = next(h for h in HORIZON_ORDER if h != case.selected_horizon)
    with pytest.raises(AlertEpisodeGoldValidationError, match="selected horizon"):
        case.evaluation_inputs(other)
