"""Stage 8 cross-dataset governance: the three gold assets together, fully offline.

The per-dataset suites already audit each asset exhaustively -- exact counts, per-window balance,
onset-as-of, ORM/DTO round-trips, provenance regexes, and the *full-corpus* holdout integrity. This
module deliberately does **not** restate those. It asserts the *cross-dataset* contract the
individual tests cannot see on their own, and it does so without ever loading holdout content:

* all three gold assets load through their **production** loaders in one process, with no network
  and no database -- and for the two split-based assets only their **tuning slices** (train +
  development) are loaded here (:func:`test_all_three_gold_assets_load_offline_with_no_network`);
* their reports are deterministic across reloads and JSON-serializable (stable hand-off to Stage 9);
* the tuning loaders exclude the final holdout **by construction**: this cross-dataset module never
  opts into, parses, copies, validates, counts, or iterates a final-holdout record. The holdout is
  exercised *only* as a rejected, gated access, and an autouse tripwire
  (:func:`_forbid_final_holdout_access`) fails any test that reads a ``final_holdout`` file or passes
  ``allow_holdout=True``. Full-corpus holdout integrity stays owned by the per-dataset suites;
* identifiers/grouping cannot leak across splits -- verified adversarially, on temporary mutated
  copies of the committed **train/development** data (never the committed files, never the holdout),
  through the public **tuning** loaders;
* the review posture of every asset is honest, never inflated;
* the alert onset->outcome hand-off is threshold-free, the label inventory is counts-only and cannot
  seed :class:`~services.alerts.experimental.WindowEvidence`, and the composite gate stays
  fail-closed;
* **no Stage 8 artifact selects or recalibrates a frozen production threshold** -- clustering 0.82,
  the 0.85/0.50 linking bands, the 0.60 analogy floor, or the 0.50 alert decision boundary all
  belong to Stage 9. This is checked structurally (no threshold-shaped key in the tuning-visible
  Stage-8 data, no threshold attribute in either loader) and against the live production constants,
  so a legitimate ADR reference in prose is never mistaken for a tuning decision.

Nothing here touches the network, a database, spaCy, or an LLM.
"""

from __future__ import annotations

import inspect
import json
import re
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

import services.analogies.corpus as analogy
import services.evaluation.alert_episode_gold as alr
import services.evaluation.entity_linking_gold as ent
from services.alerts.experimental import (
    REQUIRED_WINDOWS,
    ExperimentalGate,
    ScoreBasis,
    WindowEvidence,
)
from services.analogies.contracts import DEFAULT_MIN_SIMILARITY
from services.crisis_model.evaluation import evaluate_prediction
from services.entities.news_linking import ACCEPT_THRESHOLD, ADJUDICATE_THRESHOLD

# The physical files each split-based asset commits that this module may touch: the tuning slices
# only. The gated ``final_holdout.json`` is deliberately absent -- this cross-dataset module never
# reads it (its own schema/integrity is owned by the per-dataset ``load_corpus`` audits).
ENTITY_TUNING_FILES = ("manifest.json", "targets.json", "train.json", "development.json")
ALERT_TUNING_FILES = ("manifest.json", "train.json", "development.json")

#: Both split-based assets name their gated split's file ``final_holdout.json``. Named here so the
#: autouse tripwire and the structural key scan can recognise -- and refuse -- it by construction.
_HOLDOUT_FILENAMES = frozenset(
    {ent.SPLIT_FILES[ent.SPLIT_FINAL_HOLDOUT], alr.SPLIT_FILES[alr.SPLIT_FINAL_HOLDOUT]}
)

#: The frozen production decision boundaries Stage 9 -- not Stage 8 -- owns (implementation-order.md
#: §9). Named here so a drift in a production constant fails this governance test loudly.
FROZEN_LINKING_ACCEPT = 0.85
FROZEN_LINKING_ADJUDICATE = 0.50
FROZEN_ANALOGY_FLOOR = 0.60
FROZEN_ALERT_DECISION = 0.50


# --- offline holdout tripwire (autouse) -----------------------------------------------------
@pytest.fixture(autouse=True)
def _forbid_final_holdout_access(monkeypatch):
    """Runtime tripwire: this cross-dataset module must never load or parse a final-holdout record.

    It spies on the real read path and on the split loaders' opt-in, so an accidental
    ``load_corpus()`` (which parses ``final_holdout``), a direct read of a ``final_holdout.json``
    file, or a ``load_split(..., allow_holdout=True)`` fails the offending test loudly instead of
    silently pulling holdout content into a Stage-8 calculation. The guard is scoped to each test and
    undone by ``monkeypatch``, so it never makes the suite order-dependent.
    """
    real_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self.name in _HOLDOUT_FILENAMES:
            raise AssertionError(
                f"the Stage-8 cross-dataset suite parsed a final-holdout file ({self.name}); "
                "it must load only the tuning slices (train + development)"
            )
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    for module in (ent, alr):
        real_load_split = module.load_split

        def guarded_load_split(*args, _real=real_load_split, **kwargs):
            if kwargs.get("allow_holdout"):
                raise AssertionError(
                    "the Stage-8 cross-dataset suite opted into the final holdout "
                    "(allow_holdout=True); the holdout gate may only be exercised as a rejection here"
                )
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, "load_split", guarded_load_split)


# --- module-scoped loads (tuning slices only for the split-based assets) --------------------
@pytest.fixture(scope="module")
def entity_tuning():
    """Entity-linking train + development only; the final holdout is excluded by construction."""
    return ent.load_tuning_corpus()


@pytest.fixture(scope="module")
def alert_tuning():
    """Alert-episode train + development only; the final holdout is excluded by construction."""
    return alr.load_tuning_corpus()


@pytest.fixture(scope="module")
def analogy_assets():
    # Analogy is an evaluation-only asset with no train/dev/holdout split, so its full accepted
    # corpus/gold is loaded -- there is no tuning boundary to respect here.
    return analogy.load_corpus_and_gold()


# --- helpers: temporary mutated copies (committed data is never touched) --------------------
def _read_payloads(root: Path, files: Iterable[str]) -> dict[str, Any]:
    return {name: json.loads((root / name).read_text(encoding="utf-8")) for name in files}


def _materialize(dst: Path, payloads: dict[str, Any]) -> Path:
    for name, payload in payloads.items():
        (dst / name).write_text(json.dumps(payload), encoding="utf-8")
    return dst


def _json_keys(obj: Any) -> Iterable[str]:
    """Every object key anywhere in a parsed JSON document (recursively)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _json_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _json_keys(item)


# --- 1. every gold asset loads through its production loader, offline -----------------------
def test_all_three_gold_assets_load_offline_with_no_network(monkeypatch):
    """All three assets load together with the socket layer disarmed; split assets as tuning only."""

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted during an offline Stage 8 gold load")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    entity = ent.load_tuning_corpus()
    alert = alr.load_tuning_corpus()
    corpus, gold = analogy.load_corpus_and_gold()

    entity_tuning_total = ent.SPLIT_COUNTS[ent.SPLIT_TRAIN] + ent.SPLIT_COUNTS[ent.SPLIT_DEVELOPMENT]
    assert entity_tuning_total == 150
    assert entity.quotas.total == entity_tuning_total and entity.includes_holdout is False
    assert {m.split for m in entity.mentions} == {ent.SPLIT_TRAIN, ent.SPLIT_DEVELOPMENT}

    alert_tuning_total = alr.SPLIT_COUNTS[alr.SPLIT_TRAIN] + alr.SPLIT_COUNTS[alr.SPLIT_DEVELOPMENT]
    assert alert_tuning_total == 36
    assert alert.quotas.total == alert_tuning_total and alert.includes_holdout is False
    assert {c.split for c in alert.cases} == {alr.SPLIT_TRAIN, alr.SPLIT_DEVELOPMENT}

    assert len(gold.pairs) == 40 and len(corpus.episodes) >= 80


# --- 2. reports are deterministic across reloads and JSON-serializable -----------------------
def test_reports_are_deterministic_and_json_serializable(entity_tuning, alert_tuning, analogy_assets):
    corpus, gold = analogy_assets
    live = {
        "entity": entity_tuning.report(),
        "alert": alert_tuning.report(),
        "analogy": analogy.quota_report(corpus, gold),
    }
    reloaded = {
        "entity": ent.load_tuning_corpus().report(),
        "alert": alr.load_tuning_corpus().report(),
        "analogy": analogy.quota_report(*analogy.load_corpus_and_gold()),
    }
    for name, report in live.items():
        # json.dumps proves serializability; a fresh reload must produce the identical serialization.
        assert json.dumps(report, sort_keys=True) == json.dumps(reloaded[name], sort_keys=True), name


# --- 3./10. tuning loaders exclude the holdout by construction; the gate is proved by rejection ---
_SPLIT_DATASETS = [
    pytest.param(ent, lambda c: c.mentions, id="entity"),
    pytest.param(alr, lambda c: c.cases, id="alert"),
]


@pytest.mark.parametrize("module,records", _SPLIT_DATASETS)
def test_tuning_loaders_exclude_final_holdout_by_construction(module, records):
    assert module.TUNING_SPLITS == (module.SPLIT_TRAIN, module.SPLIT_DEVELOPMENT)
    assert module.SPLIT_FINAL_HOLDOUT not in module.TUNING_SPLITS

    tuning = module.load_tuning_corpus()
    assert tuning.includes_holdout is False
    present = {record.split for record in records(tuning)}
    assert present == set(module.TUNING_SPLITS)
    assert module.SPLIT_FINAL_HOLDOUT not in present

    expected = module.SPLIT_COUNTS[module.SPLIT_TRAIN] + module.SPLIT_COUNTS[module.SPLIT_DEVELOPMENT]
    assert len(records(tuning)) == expected

    # The default split loader gates the holdout: an unauthorised load is a hard, actionable error.
    # This module proves the boundary by that *rejection* -- it never opts in and never reads a
    # holdout record. ``SPLIT_COUNTS`` is a manifest constant, not holdout data, so naming the
    # holdout's declared size parses no holdout file.
    with pytest.raises(ValueError, match="gated"):
        module.load_split(split=module.SPLIT_FINAL_HOLDOUT)
    assert module.SPLIT_COUNTS[module.SPLIT_FINAL_HOLDOUT] > 0


def test_the_final_holdout_guard_blocks_any_holdout_access():
    """Meta-check: the autouse tripwire is real, so this module cannot silently reload the holdout."""
    # A full-corpus load parses final_holdout -- blocked at the read path.
    for module in (ent, alr):
        with pytest.raises(AssertionError, match="final-holdout file"):
            module.load_corpus()
    # An explicit opt-in -- blocked at the loader boundary, before any file is read.
    for module in (ent, alr):
        with pytest.raises(AssertionError, match="allow_holdout=True"):
            module.load_split(split=module.SPLIT_FINAL_HOLDOUT, allow_holdout=True)


# --- 4. adversarial: the tuning loaders reject cross-split leakage (mutated tmp copies) ------
def test_entity_loader_rejects_cross_split_leakage_on_a_mutated_copy(tmp_path):
    payloads = _read_payloads(ent.GOLD_ROOT, ENTITY_TUNING_FILES)
    # Give a development mention a train mention's leakage_group: the group now straddles two splits.
    train_group = payloads["train.json"]["mentions"][0]["leakage_group"]
    payloads["development.json"]["mentions"][0]["leakage_group"] = train_group
    root = _materialize(tmp_path, payloads)
    with pytest.raises(ent.EntityGoldValidationError, match="spans splits"):
        ent.load_tuning_corpus(root)


def test_alert_loader_rejects_cross_split_leakage_on_a_mutated_copy(tmp_path):
    payloads = _read_payloads(alr.GOLD_ROOT, ALERT_TUNING_FILES)
    train_group = payloads["train.json"]["cases"][0]["episode_group"]
    payloads["development.json"]["cases"][0]["episode_group"] = train_group
    root = _materialize(tmp_path, payloads)
    with pytest.raises(alr.AlertEpisodeGoldValidationError, match="spans splits"):
        alr.load_tuning_corpus(root)


# --- 7. adversarial: a NIL entity record cannot smuggle in an expected canonical target ------
def test_entity_nil_record_cannot_smuggle_an_expected_target(tmp_path):
    payloads = _read_payloads(ent.GOLD_ROOT, ENTITY_TUNING_FILES)
    real_target = payloads["targets.json"]["targets"][0]["target_id"]
    nil = next(
        m
        for name in ("train.json", "development.json")
        for m in payloads[name]["mentions"]
        if m["expected_label"] == "nil"
    )
    nil["expected_target_id"] = real_target  # a NIL answer that names a real entity is a contradiction
    root = _materialize(tmp_path, payloads)
    with pytest.raises(ent.EntityGoldValidationError, match="null expected_target_id"):
        ent.load_tuning_corpus(root)


# --- 6. the alert hand-off is threshold-free and matches the Stage-9 scorer -------------------
def test_alert_stage9_handoff_withholds_the_decision_boundary(alert_tuning):
    scorer_params = set(inspect.signature(evaluate_prediction).parameters)
    for case in (
        next(c for c in alert_tuning.cases if c.is_positive),
        next(c for c in alert_tuning.cases if not c.is_positive),
    ):
        kwargs = case.evaluation_inputs().as_kwargs()
        assert set(kwargs) == {"horizon", "evaluation_date", "actual_outcome", "actual_start_date"}
        assert "threshold" not in kwargs
        assert set(kwargs) <= scorer_params
    # The gold supplies outcome truth but withholds exactly the scorer's decision knobs: the raw
    # prediction, its id, and the threshold. Stage 9 owns every one of those.
    withheld = scorer_params - {"horizon", "evaluation_date", "actual_outcome", "actual_start_date"}
    assert withheld == {"prediction", "prediction_id", "threshold"}
    assert "threshold" in withheld


def test_alert_label_inventory_is_counts_only_and_cannot_seed_gate_evidence(alert_tuning):
    inventory = alert_tuning.label_inventory()
    assert [item.window for item in inventory] == list(REQUIRED_WINDOWS)

    inventory_fields = set(alr.WindowLabelInventory.__dataclass_fields__)
    evidence_metrics = {
        "candidate_precision",
        "baseline_precision",
        "candidate_lead_time_days",
        "baseline_lead_time_days",
    }
    # The inventory names *what to score*; it carries none of the measured metrics a WindowEvidence
    # needs, so a passing gate cannot be assembled from the gold set alone.
    assert evidence_metrics.issubset(set(WindowEvidence.__dataclass_fields__))
    assert inventory_fields.isdisjoint(evidence_metrics)
    for item in inventory:
        for attr in evidence_metrics:
            assert not hasattr(item, attr), attr


def test_loading_the_gold_never_releases_the_composite_gate(alert_tuning):
    # The dataset module builds no gate and no evidence; the composite gate is fail-closed.
    assert not hasattr(alr, "ExperimentalGate")
    assert not hasattr(alr, "WindowEvidence")
    composite = ExperimentalGate().decide(ScoreBasis.COMPOSITE)
    assert composite.released is False and composite.experimental is True
    assert ExperimentalGate().decide(ScoreBasis.SINGLE_SIGNAL).released is True


# --- 5. every asset's review posture is honest, never inflated ------------------------------
def test_all_three_review_postures_are_honest(entity_tuning, alert_tuning, analogy_assets):
    corpus, _ = analogy_assets

    # Entity: original synthetic prose, declared as such on every record and in the manifest.
    assert entity_tuning.manifest.metadata.get("provenance") == "original_synthetic"
    assert all(m.provenance_kind == ent.PROVENANCE_KIND == "original_synthetic" for m in entity_tuning.mentions)

    # Alert: the manifest denies human and independent verification it never did.
    review = alert_tuning.manifest.review
    assert review["state"] == alr.REVIEW_STATE == "automated_contract_validation_only"
    assert review["human_verified"] is False
    assert review["independent_historical_verification"] is False
    assert all(c.provenance.review_status == alr.REVIEW_STATE for c in alert_tuning.cases)

    # Analogy: the corpus reports its (zero) human sign-offs truthfully and flags the rest unreviewed.
    unreviewed = analogy.unreviewed_episodes(corpus)
    assert {e.slug for e in unreviewed} == {e.slug for e in corpus.episodes if not e.is_human_reviewed}
    assert corpus.quotas.human_reviewed == sum(1 for e in corpus.episodes if e.is_human_reviewed)
    assert len(unreviewed) == len(corpus.episodes) - corpus.quotas.human_reviewed


# --- 8. the accepted analogy asset is unchanged: exactly 40 leaf pairs -----------------------
def test_analogy_asset_is_forty_pairs_targeting_live_leaf_episodes(analogy_assets):
    corpus, gold = analogy_assets
    assert len(gold.pairs) == 40
    known = corpus.by_id
    parents = corpus.parent_ids
    for pair in gold.pairs:
        assert pair.expected_episode_ids  # a pair always names at least one right answer
        for episode_id in pair.correct_episode_ids:
            assert episode_id in known, pair.pair_id  # target still exists in the corpus
            assert episode_id not in parents, pair.pair_id  # and is a rankable leaf, never a parent arc
    assert json.dumps(gold.coverage(), sort_keys=True)  # coverage is a stable, serializable surface


# --- 9. no Stage 8 artifact selects or recalibrates a frozen production threshold ------------
_THRESHOLD_KEY = re.compile(r"threshold", re.IGNORECASE)


def test_no_committed_stage8_gold_json_carries_a_threshold_key():
    # Scanned: the tuning-visible split files + the manifests/catalog + the analogy asset. The
    # ``final_holdout.json`` files are intentionally excluded -- this cross-dataset module never
    # parses holdout content; their threshold-freedom is owned by the per-dataset ``load_corpus``
    # audits, which reject any unknown (hence any threshold-shaped) field.
    split_files = [
        path
        for path in (*ent.GOLD_ROOT.glob("*.json"), *alr.GOLD_ROOT.glob("*.json"))
        if path.name not in _HOLDOUT_FILENAMES
    ]
    offenders: list[tuple[str, str]] = []
    for path in sorted([*split_files, analogy.GOLD_PATH]):
        document = json.loads(path.read_text(encoding="utf-8"))
        offenders += [(path.name, key) for key in _json_keys(document) if _THRESHOLD_KEY.search(key)]
    assert not offenders, f"a Stage 8 gold document carries a threshold-shaped key: {offenders}"


def test_gold_loader_modules_define_no_threshold_of_their_own():
    # A gold contract that introduced a decision boundary would be tuning; neither loader may.
    for module in (ent, alr):
        assert [name for name in dir(module) if "threshold" in name.lower()] == []


def test_the_frozen_production_thresholds_are_intact_and_owned_by_stage9():
    # The gold sets are calibrated *against* these values in Stage 9; Stage 8 must never move them.
    assert ACCEPT_THRESHOLD == FROZEN_LINKING_ACCEPT
    assert ADJUDICATE_THRESHOLD == FROZEN_LINKING_ADJUDICATE
    assert DEFAULT_MIN_SIMILARITY == FROZEN_ANALOGY_FLOOR
    assert inspect.signature(evaluate_prediction).parameters["threshold"].default == FROZEN_ALERT_DECISION


# --- 9 (worklog): the worklog claims no tuning and defers the thresholds to Stage 9 ----------
_WORKLOG = Path(__file__).resolve().parents[2] / "docs" / "worklogs" / "stage8-gold-datasets.md"

#: First-person Stage-8 tuning claims. Written narrowly so a legitimate "Stage 9 recalibrates the
#: 0.60 threshold" reference, or an ADR citation, never matches.
_FORBIDDEN_WORKLOG_CLAIMS = (
    r"\bwe (?:recalibrat|re-?calibrat|retun|re-?tun|tuned|selected|chose)\w*\b[^.\n]*\bthreshold",
    r"\bthreshold\b[^.\n]{0,60}\b(?:recalibrated|tuned|selected|chosen)\b[^.\n]{0,20}\b(?:in|by|during)\s+stage\s*8\b",
    r"\bstage\s*8\b[^.\n]{0,60}\b(?:recalibrat|tunes?|tuned|selects?|selected)\b[^.\n]{0,20}\bthreshold",
)


def test_worklog_defers_thresholds_to_stage9_and_claims_no_tuning():
    assert _WORKLOG.exists(), f"missing Stage 8 worklog at {_WORKLOG}"
    text = _WORKLOG.read_text(encoding="utf-8")
    lowered = text.lower()

    # It names each frozen value and hands tuning to Stage 9.
    for value in ("0.82", "0.85", "0.50", "0.60"):
        assert value in text, f"worklog does not name the frozen threshold {value}"
    assert "stage 9" in lowered
    assert re.search(r"stage\s*9[^.\n]*\btuning\b|\btuning\b[^.\n]*stage\s*9", lowered), (
        "worklog must state Stage 9 owns tuning"
    )

    # It makes no first-person Stage-8 tuning claim.
    for pattern in _FORBIDDEN_WORKLOG_CLAIMS:
        match = re.search(pattern, lowered)
        assert match is None, f"worklog makes an unsupported Stage-8 tuning claim: {match and match.group(0)!r}"
