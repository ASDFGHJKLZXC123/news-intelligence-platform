"""Stage 9 analogy evaluation-only validation report (`stage9-validation.v1` §4.3).

Every test is offline and holdout-safe. The report is built from the tuning-visible corpus and gold
only (``load_corpus_and_gold``); no test opens a sealed final holdout, runs retrieval, or fabricates
a similarity score, and a C-level audit run proves no holdout file and no network socket are touched.
No database, network, embedding call, clock, or randomness is used anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from services.analogies.contracts import DEFAULT_MIN_SIMILARITY, DEFAULT_TOP_K
from services.analogies.corpus import (
    CorpusValidationError,
    load_corpus_and_gold,
    quota_report,
)
from services.evaluation import analogy_validation as av
from services.evaluation.analogy_validation import (
    ANALOGY_FLOOR,
    DOMAIN,
    REPORT_SPLIT,
    AnalogyValidationError,
    analogy_input_paths,
    build_analogy_validation_report,
    write_analogy_validation_report,
)
from services.evaluation.calibration import (
    CalibrationStatus,
    canonical_json_bytes,
    sha256_file,
)

EXPECTED_INPUT_NAMES = frozenset(path.name for path in analogy_input_paths())


# --- determinism and canonical serialization -----------------------------------------------
def test_the_report_is_deterministic_and_canonical() -> None:
    first = build_analogy_validation_report()
    second = build_analogy_validation_report()
    payload = canonical_json_bytes(first)
    assert payload == canonical_json_bytes(second)
    # canonical JSON round-trips and carries no NaN/Infinity.
    assert json.loads(payload.decode("utf-8")) == first


# --- input hashing and provenance ----------------------------------------------------------
def test_the_report_hashes_exactly_the_consumed_inputs_and_protocol() -> None:
    report = build_analogy_validation_report()
    paths = analogy_input_paths()
    # 9 numbered episode shards plus analogy_gold.json -- and never the authoring template.
    assert set(report["input_hashes"]) == {path.name for path in paths}
    assert set(report["input_hashes"]) == EXPECTED_INPUT_NAMES
    assert "authoring_template.json" not in report["input_hashes"]
    for path in paths:
        assert report["input_hashes"][path.name] == sha256_file(path)
    assert report["protocol"]["id"] == "stage9-validation.v1"
    assert report["protocol"]["sha256"] == sha256_file(av.PROTOCOL_DOC_PATH)


def test_the_report_records_evaluation_only_unsplit_analogy() -> None:
    report = build_analogy_validation_report()
    assert report["domain"] == DOMAIN == "analogy"
    assert report["status"] == CalibrationStatus.EVALUATION_ONLY.value == "evaluation_only"
    assert report["split"] == REPORT_SPLIT == "unsplit"


def test_the_report_carries_the_exact_counts() -> None:
    report = build_analogy_validation_report()
    provenance = report["provenance"]
    assert provenance["input_file_count"] == 10
    assert provenance["corpus_shard_count"] == 9
    assert provenance["gold_pairs"] == 40
    assert report["coverage"]["gold"]["pairs"] == 40
    assert provenance["split"] == "unsplit"


def test_the_coverage_is_the_deterministic_quota_report_of_the_validated_data() -> None:
    corpus, gold = load_corpus_and_gold()
    assert build_analogy_validation_report()["coverage"] == quota_report(corpus, gold)


# --- no sweep, no selection, floor unchanged -----------------------------------------------
def test_the_report_runs_no_grid_and_no_sweep() -> None:
    grid = build_analogy_validation_report()["evaluated_grid"]
    assert grid["swept"] is False
    assert grid["grid"] == "n/a"


def test_the_floor_is_060_unchanged_and_nothing_is_selected() -> None:
    selected = build_analogy_validation_report()["selected_parameters"]
    assert selected["selection"] == "none"
    # Reported as an exact fixed-precision decimal string, like every other Stage 9 threshold.
    assert isinstance(selected["floor"], str)
    assert selected["floor"] == ANALOGY_FLOOR == "0.60"
    assert selected["floor_changed"] is False
    assert selected["production_applied"] is False
    # The production constant itself is intact and untouched, and the string reports exactly it.
    assert DEFAULT_MIN_SIMILARITY == float(ANALOGY_FLOOR) == 0.60


def test_the_reported_floor_is_pinned_to_the_live_constant() -> None:
    """A drifted production floor stops the report from being built at all."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(av, "DEFAULT_MIN_SIMILARITY", 0.65)
        with pytest.raises(AnalogyValidationError, match="not 0.60"):
            build_analogy_validation_report()


# --- honest, unavailable metrics -----------------------------------------------------------
def test_every_required_metric_is_unavailable_and_null() -> None:
    metrics = build_analogy_validation_report()["metrics"]
    assert metrics["available"] is False
    assert metrics["reason"]
    for key in ("hit_at_1", "hit_at_3", "hit_at_5", "recall_at_5", "mrr", "abstention_rate"):
        assert metrics[key] is None, key
    diagnostics = metrics["threshold_diagnostics"]
    assert isinstance(diagnostics["min_similarity"], str)
    assert diagnostics["min_similarity"] == ANALOGY_FLOOR == "0.60"
    for key in ("correct_above_threshold", "correct_below_threshold", "correct_absent"):
        assert diagnostics[key] is None, key
    # hit@5 / recall@5 correspond to the shipped k.
    assert DEFAULT_TOP_K == 5


def test_the_report_names_the_stated_blockers_and_limitations() -> None:
    report = build_analogy_validation_report()
    limitations = " ".join(report["limitations"]).lower()
    assert "unsplit" in limitations
    assert "no reliable analogy" in limitations
    assert "human_reviewed = 0" in limitations
    assert "no reranker-quality claim" in limitations
    assert any("outcome-blind" in blocker for blocker in report["blockers"])


def test_the_report_never_names_a_final_holdout() -> None:
    assert b"final_holdout" not in canonical_json_bytes(build_analogy_validation_report())


# --- the writer ----------------------------------------------------------------------------
def test_write_writes_once_and_refuses_to_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "analogy.json"
    data = write_analogy_validation_report(output)
    assert output.read_bytes() == data == canonical_json_bytes(build_analogy_validation_report())
    with pytest.raises(AnalogyValidationError, match="refusing to overwrite"):
        write_analogy_validation_report(output)


def test_input_inventory_rejects_a_directory_with_no_shards(tmp_path: Path) -> None:
    with pytest.raises(AnalogyValidationError, match="no corpus shard files"):
        analogy_input_paths(tmp_path)


def test_building_from_an_empty_directory_raises_a_validation_error(tmp_path: Path) -> None:
    with pytest.raises(CorpusValidationError):
        build_analogy_validation_report(directory=tmp_path, gold_path=tmp_path / "gold.json")


# --- provenance guardrails -----------------------------------------------------------------
def test_the_evaluator_source_never_reaches_for_a_holdout_or_a_live_run() -> None:
    source = Path(av.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "final_holdout",
        "allow_holdout",
        "retrieve_analogy_candidates",
        "evaluate_gold_set",
        "evaluate_pair",
        "embed_texts",
        "SessionLocal",
        "sqlalchemy",
    ):
        assert forbidden not in source, forbidden


# A C-level audit hook installed once, below any monkeypatch, recording only while armed.
_ANALOGY_AUDIT_OPENS: list[str] = []
_ANALOGY_AUDIT_SOCKETS: list[str] = []
_ANALOGY_AUDIT_ARMED = False


def _analogy_audit_hook(event: str, args: tuple) -> None:
    if not _ANALOGY_AUDIT_ARMED:
        return
    if event == "open":
        _ANALOGY_AUDIT_OPENS.append(str(args[0]))
    elif event.startswith("socket."):
        _ANALOGY_AUDIT_SOCKETS.append(event)


sys.addaudithook(_analogy_audit_hook)


def test_building_the_report_opens_only_allowed_files_and_no_network() -> None:
    global _ANALOGY_AUDIT_ARMED
    _ANALOGY_AUDIT_OPENS.clear()
    _ANALOGY_AUDIT_SOCKETS.clear()
    _ANALOGY_AUDIT_ARMED = True
    try:
        build_analogy_validation_report()
    finally:
        _ANALOGY_AUDIT_ARMED = False

    assert not any("final_holdout" in path for path in _ANALOGY_AUDIT_OPENS)
    assert _ANALOGY_AUDIT_SOCKETS == []  # no network, no DB-over-socket, no embedding/LLM call
    episode_json_opens = {
        Path(path).name
        for path in _ANALOGY_AUDIT_OPENS
        if "episodes" in path and path.endswith(".json")
    }
    assert episode_json_opens <= set(EXPECTED_INPUT_NAMES)
    assert any("stage9-validation-protocol.md" in path for path in _ANALOGY_AUDIT_OPENS)
