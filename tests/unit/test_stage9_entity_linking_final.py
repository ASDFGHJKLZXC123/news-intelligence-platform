"""Unit tests for the Stage 9 entity-linking one-time final-holdout command (§1, §5, §6).

**No test in this file touches the sealed final holdout.** Every run is staged into a temp tree that
holds copies of the protocol, the four development reports, a freeze rebuilt by the real writers, and
only the four *tuning-visible* gold files -- plus a clearly synthetic ``final_holdout.json``
stand-in whose bytes exist purely so the consumed-input hash has something to hash. The loader and
the scorer are injected fakes, so the real gated loader is never invoked and the real sealed split is
never opened, stat-ed, counted, or hashed. The one test that runs the real preflight runs it against
the real freeze, which by construction reads no split file at all, and an audit hook proves it.

Nothing here uses a database, a network, spaCy, an LLM, the clock, or randomness.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from services.entities.news_linking import ACCEPT_THRESHOLD, ADJUDICATE_THRESHOLD, LinkBand
from services.evaluation import entity_linking_final as elf
from services.evaluation.calibration import canonical_json_bytes, canonical_json_hash, sha256_file
from services.evaluation.entity_linking_calibration import (
    ScoredMention,
    SplitStats,
    build_entity_linking_report,
    build_split_stats,
    reband_snapshot,
    score_corpus,
    score_mentions,
)
from services.evaluation.entity_linking_final import (
    CANONICAL_PATHS,
    DOMAIN,
    EVALUATION_STATUS,
    FINAL_REPORT_PATH,
    FROZEN_ACCEPT,
    FROZEN_ADJUDICATE,
    RECEIPT_SCHEMA,
    RECEIPT_STATE,
    REPORT_SCHEMA,
    REPORT_SPLIT,
    UNSEAL_RECEIPT_PATH,
    EntityLinkingFinalError,
    FinalPaths,
    build_parser,
    main,
    preflight,
    run_final_holdout,
)
from services.evaluation.entity_linking_gold import (
    CATALOG_FILE,
    GOLD_ROOT,
    MANIFEST_FILE,
    SPLIT_DEVELOPMENT,
    SPLIT_FILES,
    SPLIT_FINAL_HOLDOUT,
    SPLIT_TRAIN,
    load_target_catalog,
    load_tuning_corpus,
)
from services.evaluation.stage9_freeze import (
    DEVELOPMENT_DIR,
    FROZEN_MANIFEST_PATH,
    FROZEN_PARAMETERS_PATH,
    PROTOCOL_DOC_PATH,
    SELECTED_ACCEPT,
    SELECTED_ADJUDICATE,
    write_freeze_manifest,
    write_frozen_parameters,
)

_REPORT_FILES = ("alerts.json", "analogy.json", "clustering.json", "entity-linking.json")
_TUNING_FILES = (MANIFEST_FILE, CATALOG_FILE, SPLIT_FILES[SPLIT_TRAIN], SPLIT_FILES[SPLIT_DEVELOPMENT])
_FINAL_FILE = SPLIT_FILES[SPLIT_FINAL_HOLDOUT]
#: A stand-in for the sealed split. Its only job is to have bytes worth hashing.
_SYNTHETIC_FINAL = {"note": "synthetic stand-in; the real sealed split is never staged or read"}
_DECLARED_FINAL_COUNT = 50


# --- staging: a complete, mutable, holdout-free tree ----------------------------------------
def _canonical_write(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(canonical_json_bytes(payload))


def stage(tmp_path: Path, *, mutate_gold: Any = None, mutate_dev: Any = None) -> FinalPaths:
    """Build a self-consistent freeze + gold tree in ``tmp_path``, then rebuild the freeze over it.

    The real sealed split is never copied; ``final_holdout.json`` here is the synthetic stand-in.
    After any mutation, the entity development report's ``input_hashes`` are resynced to the staged
    gold bytes and the freeze artifacts are written last, so the tree passes preflight up to exactly
    the failure a test is isolating.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    protocol = tmp_path / "protocol.md"
    shutil.copy2(PROTOCOL_DOC_PATH, protocol)

    gold = tmp_path / "gold"
    gold.mkdir()
    for name in _TUNING_FILES:
        shutil.copy2(GOLD_ROOT / name, gold / name)
    _canonical_write(gold / _FINAL_FILE, _SYNTHETIC_FINAL)
    if mutate_gold is not None:
        mutate_gold(gold)

    development = tmp_path / "development"
    development.mkdir()
    for name in _REPORT_FILES:
        shutil.copy2(DEVELOPMENT_DIR / name, development / name)

    entity_report = development / "entity-linking.json"
    parsed = json.loads(entity_report.read_text(encoding="utf-8"))
    parsed["input_hashes"] = {name: sha256_file(gold / name) for name in _TUNING_FILES}
    if mutate_dev is not None:
        mutate_dev(parsed)
    _canonical_write(entity_report, parsed)

    frozen = tmp_path / "frozen"
    frozen.mkdir()
    parameters, manifest = frozen / "parameters.json", frozen / "manifest.json"
    write_frozen_parameters(parameters, development_dir=development, protocol_path=protocol)
    write_freeze_manifest(
        manifest,
        parameters_path=parameters,
        development_dir=development,
        protocol_path=protocol,
    )
    return FinalPaths(
        protocol=protocol,
        freeze_manifest=manifest,
        frozen_parameters=parameters,
        development_dir=development,
        gold_root=gold,
        output=tmp_path / "final" / "entity-linking.json",
        receipt=tmp_path / "final" / "entity-linking.unseal-receipt.json",
    )


# --- synthetic scored mentions (never real holdout content) ---------------------------------
def _snapshot(
    mention_id: str,
    *,
    tag: str,
    score: str | None,
    is_link: bool = True,
    tie: bool = False,
) -> ScoredMention:
    top_id = uuid.uuid4() if score is not None else None
    expected = top_id if is_link else None
    candidates = [top_id] if top_id is not None else []
    if tie:
        candidates.append(uuid.uuid4())
    return ScoredMention(
        mention_id=mention_id,
        split=SPLIT_FINAL_HOLDOUT,
        case_tags=(tag,),
        is_link=is_link,
        expected_target=expected,
        candidate_ids=tuple(c for c in candidates if c is not None),
        top_id=top_id,
        top_score=Decimal(score) if score is not None else None,
        top_tie=tie,
        top_alias_type="legal_name" if top_id is not None else None,
        top_supported=top_id is not None,
    )


def _final_snapshots() -> tuple[ScoredMention, ...]:
    """50 synthetic snapshots with a known outcome at the frozen 0.070 / 0.000 bands."""
    snapshots = [_snapshot(f"a{i}", tag="legal_name", score="0.500") for i in range(30)]
    snapshots += [_snapshot(f"t{i}", tag="ambiguous", score="0.500", tie=True) for i in range(5)]
    snapshots += [_snapshot(f"b{i}", tag="brand_product", score="0.050") for i in range(5)]
    snapshots += [_snapshot(f"n{i}", tag="nil", score=None, is_link=False) for i in range(10)]
    return tuple(snapshots)


class RecordingLoader:
    """Stands in for ``load_split``: records every call and whether the receipt existed already."""

    def __init__(self, mentions: tuple[ScoredMention, ...], receipt: Path) -> None:
        self._mentions = mentions
        self._receipt = receipt
        self.calls: list[dict[str, Any]] = []

    def __call__(self, root: Path, split: str, **kwargs: Any) -> tuple[ScoredMention, ...]:
        self.calls.append(
            {
                "root": root,
                "split": split,
                "catalog": kwargs.get("catalog"),
                "allow_holdout": kwargs.get("allow_holdout"),
                "receipt_existed": self._receipt.exists(),
            }
        )
        return self._mentions


def _scorer(mentions: Any, catalog: Any) -> SplitStats:
    """Scores nothing: the synthetic snapshots *are* the scored mentions."""
    return build_split_stats(list(mentions))


def _run(paths: FinalPaths, loader: Any = None, scorer: Any = _scorer) -> tuple[bytes, Any]:
    loader = loader or RecordingLoader(_final_snapshots(), paths.receipt)
    data = run_final_holdout(
        acknowledge_final_holdout=True, paths=paths, loader=loader, scorer=scorer
    )
    return data, loader


def _never_called(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the gated holdout loader must not be reached")


# --- 1. refusals that never reach the loader and never create a receipt ---------------------
def test_no_acknowledgement_refuses_before_reading_any_file(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    _OPENED.clear()
    _RECORDING["on"] = True
    try:
        with pytest.raises(EntityLinkingFinalError, match="acknowledge"):
            run_final_holdout(paths=paths, loader=_never_called, scorer=_never_called)
    finally:
        _RECORDING["on"] = False
    assert _OPENED == []  # not one file was opened
    assert not paths.receipt.exists()
    assert not paths.output.exists()


def test_an_existing_report_or_receipt_refuses_before_any_holdout_access(tmp_path: Path) -> None:
    for existing in ("output", "receipt"):
        paths = stage(tmp_path / existing)
        target = getattr(paths, existing)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"{}\n")
        with pytest.raises(EntityLinkingFinalError, match="already exists"):
            run_final_holdout(
                acknowledge_final_holdout=True,
                paths=paths,
                loader=_never_called,
                scorer=_never_called,
            )


def _append(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b" ")


def _rewrite(path: Path, mutate: Any) -> None:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    mutate(parsed)
    path.write_bytes(canonical_json_bytes(parsed))


#: Each case edits the *staged* tree after it was built, so the edit is the only thing wrong.
_TAMPERED_CASES: dict[str, tuple[Any, str]] = {
    "edited_parameters": (
        lambda p: _rewrite(p.frozen_parameters, lambda d: d.update({"policy_version": "edited"})),
        "does not match the parameters rebuilt",
    ),
    "edited_development_report": (
        lambda p: _rewrite(p.development_dir / "alerts.json", lambda d: d.update({"x": "edited"})),
        "does not match the manifest rebuilt",
    ),
    "noncanonical_development_report": (
        lambda p: _append(p.development_dir / "clustering.json"),
        "not canonical bytes",
    ),
    "missing_development_report": (
        lambda p: (p.development_dir / "analogy.json").unlink(),
        "missing freeze input",
    ),
    "drifted_train_input": (
        lambda p: _append(p.gold_root / SPLIT_FILES[SPLIT_TRAIN]),
        "hash mismatch",
    ),
    "drifted_catalog_input": (
        lambda p: _append(p.gold_root / CATALOG_FILE),
        "hash mismatch",
    ),
}


@pytest.mark.parametrize("case", sorted(_TAMPERED_CASES))
def test_a_tampered_or_drifted_input_never_unseals(tmp_path: Path, case: str) -> None:
    tamper, match = _TAMPERED_CASES[case]
    paths = stage(tmp_path)
    tamper(paths)
    with pytest.raises(ValueError, match=match):
        run_final_holdout(
            acknowledge_final_holdout=True, paths=paths, loader=_never_called, scorer=_never_called
        )
    assert not paths.receipt.exists()
    assert not paths.output.exists()


def _drop_final_declaration(gold: Path) -> None:
    """Break the gold manifest's final-split declaration -- the count the command reads by metadata."""
    manifest = json.loads((gold / MANIFEST_FILE).read_text(encoding="utf-8"))
    for split in manifest["splits"]:
        if split["name"] == SPLIT_FINAL_HOLDOUT:
            split["count"] = 0
    (gold / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")


#: Each case is staged *into* the tree, so the freeze is rebuilt over it and stays self-consistent.
_STAGED_CASES: dict[str, tuple[Any, Any, str]] = {
    "smuggled_holdout_input": (
        None,
        lambda r: r["input_hashes"].update({_FINAL_FILE: "0" * 64}),
        "must hash exactly",
    ),
    "wrong_dataset": (
        None,
        lambda r: r["dataset"].update({"dataset_id": "some_other_gold"}),
        "not the loaded gold set",
    ),
    "undeclared_final_split": (_drop_final_declaration, None, "count must be"),
}


@pytest.mark.parametrize("case", sorted(_STAGED_CASES))
def test_a_dishonest_report_or_gold_declaration_never_unseals(tmp_path: Path, case: str) -> None:
    mutate_gold, mutate_dev, match = _STAGED_CASES[case]
    paths = stage(tmp_path, mutate_gold=mutate_gold, mutate_dev=mutate_dev)
    with pytest.raises(ValueError, match=match):
        run_final_holdout(
            acknowledge_final_holdout=True, paths=paths, loader=_never_called, scorer=_never_called
        )
    assert not paths.receipt.exists()
    assert not paths.output.exists()


@pytest.mark.parametrize(
    ("attribute", "value", "match"),
    [
        ("ACCEPT_THRESHOLD", 0.85, "production drifted"),
        ("ADJUDICATE_THRESHOLD", 0.5, "production drifted"),
        ("SIGNAL_WEIGHTS", {}, "signal weights"),
    ],
)
def test_a_live_model_mismatch_never_unseals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str, value: Any, match: str
) -> None:
    paths = stage(tmp_path)
    monkeypatch.setattr(elf, attribute, value)
    with pytest.raises(EntityLinkingFinalError, match=match):
        run_final_holdout(
            acknowledge_final_holdout=True, paths=paths, loader=_never_called, scorer=_never_called
        )
    assert not paths.receipt.exists()


# --- 2. the unseal itself: receipt first, exactly one gated loader call ---------------------
def test_the_receipt_is_created_before_the_loader_is_ever_called(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    assert not paths.receipt.exists()
    _, loader = _run(paths)
    assert loader.calls[0]["receipt_existed"] is True


def test_exactly_one_gated_loader_call_with_the_validated_catalog(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    _, loader = _run(paths)
    assert len(loader.calls) == 1
    call = loader.calls[0]
    assert call["allow_holdout"] is True
    assert call["split"] == SPLIT_FINAL_HOLDOUT
    assert call["root"] == paths.gold_root
    # The catalog validated during preflight is passed through, so it is never reloaded.
    staged = load_target_catalog(paths.gold_root)
    assert call["catalog"] is not None
    assert [t.target_id for t in call["catalog"].targets] == [t.target_id for t in staged.targets]


def test_the_receipt_is_created_exclusively_and_a_second_run_is_refused(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    _run(paths)
    assert paths.output.exists() and paths.receipt.exists()
    with pytest.raises(EntityLinkingFinalError, match="already exists"):
        _run(paths, loader=_never_called, scorer=_never_called)
    # Even with the report removed, the receipt alone keeps the split spent.
    paths.output.unlink()
    with pytest.raises(EntityLinkingFinalError, match="irrevocably spent"):
        _run(paths, loader=_never_called, scorer=_never_called)


def test_a_failure_after_the_unseal_still_refuses_every_rerun(tmp_path: Path) -> None:
    paths = stage(tmp_path)

    def _explode(mentions: Any, catalog: Any) -> SplitStats:
        raise RuntimeError("scoring blew up after the split was opened")

    with pytest.raises(RuntimeError):
        _run(paths, scorer=_explode)
    assert paths.receipt.exists()
    assert not paths.output.exists()
    with pytest.raises(EntityLinkingFinalError, match="irrevocably spent"):
        _run(paths)


def test_a_short_final_split_refuses_after_the_receipt_and_writes_no_report(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    loader = RecordingLoader(_final_snapshots()[:10], paths.receipt)
    with pytest.raises(EntityLinkingFinalError, match="not the declared"):
        _run(paths, loader=loader)
    assert paths.receipt.exists()
    assert not paths.output.exists()


# --- 3. the two canonical artifacts ---------------------------------------------------------
def test_the_receipt_is_canonical_and_carries_no_clock_git_or_result(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    _run(paths)
    raw = paths.receipt.read_bytes()
    receipt = json.loads(raw.decode("utf-8"))
    assert raw == canonical_json_bytes(receipt)
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["state"] == RECEIPT_STATE
    assert receipt["domain"] == DOMAIN
    assert receipt["split"] == SPLIT_FINAL_HOLDOUT
    assert receipt["selected_parameters"] == {
        "accept": SELECTED_ACCEPT,
        "adjudicate": SELECTED_ADJUDICATE,
    }
    assert receipt["protocol"]["sha256"] == sha256_file(paths.protocol)
    assert receipt["freeze"]["manifest"]["sha256"] == sha256_file(paths.freeze_manifest)
    assert receipt["freeze"]["frozen_parameters"]["sha256"] == sha256_file(paths.frozen_parameters)
    assert receipt["freeze"]["development_report"]["sha256"] == sha256_file(paths.development_report)
    assert receipt["model_versions"]["linker_weight_set"] == "adr0005-stage2-initial.v1"
    assert receipt["output_path"].endswith("entity-linking.json")
    for forbidden in ("generated_at", "timestamp", "revision", "commit", "metrics"):
        assert forbidden not in raw.decode("utf-8")


def test_the_final_report_is_canonical_and_states_its_disposition(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    data, _ = _run(paths)
    report = json.loads(data.decode("utf-8"))
    assert data == paths.output.read_bytes() == canonical_json_bytes(report)
    assert report["schema"] == REPORT_SCHEMA
    assert report["protocol"] == {
        "id": "stage9-validation.v1",
        "sha256": sha256_file(paths.protocol),
    }
    assert report["domain"] == DOMAIN
    assert report["split"] == REPORT_SPLIT == "final"
    assert report["status"] == "calibrated"
    assert report["evaluation_status"] == EVALUATION_STATUS == "final_reported_once"
    assert report["no_retuning"] is True
    assert report["selected_parameters"]["accept"] == SELECTED_ACCEPT == "0.070"
    assert report["selected_parameters"]["adjudicate"] == SELECTED_ADJUDICATE == "0.000"
    assert report["selected_parameters"]["changed_by_this_run"] is False
    assert report["model_versions"]["band_policy_version"] == "entity-linking-bands.stage9-validation.v1"
    assert report["dataset"] == {"dataset_id": "entity_linking_gold", "schema_version": "v1"}
    assert report["limitations"] == list(elf.LIMITATIONS)
    assert any("synthetic" in line for line in report["limitations"])
    assert any("stage-3" in line for line in report["limitations"])
    assert any("human" in line for line in report["limitations"])


def test_the_report_records_every_freeze_artifact_and_consumed_input_hash(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    data, _ = _run(paths)
    report = json.loads(data.decode("utf-8"))

    freeze = report["freeze"]
    assert freeze["manifest"]["sha256"] == sha256_file(paths.freeze_manifest)
    assert freeze["frozen_parameters"]["sha256"] == sha256_file(paths.frozen_parameters)
    assert freeze["development_report"]["sha256"] == sha256_file(paths.development_report)
    assert freeze["unseal_receipt"]["sha256"] == sha256_file(paths.receipt)
    assert freeze["unseal_receipt"]["sha256"] == canonical_json_hash(
        json.loads(paths.receipt.read_text(encoding="utf-8"))
    )
    assert freeze["unseal_receipt"]["path"].endswith("entity-linking.unseal-receipt.json")

    assert set(report["input_hashes"]) == {*_TUNING_FILES, _FINAL_FILE}
    for name in (*_TUNING_FILES, _FINAL_FILE):
        assert report["input_hashes"][name] == sha256_file(paths.gold_root / name)


def test_the_report_carries_no_grid_sweep_or_train_development_selection_metrics(
    tmp_path: Path,
) -> None:
    paths = stage(tmp_path)
    data, _ = _run(paths)
    report = json.loads(data.decode("utf-8"))
    for absent in ("evaluated_grid", "eligibility", "initial_parameters"):
        assert absent not in report
    assert set(report["metrics"]) == set(report["counts"]) == {"final"}
    text = data.decode("utf-8")
    for forbidden in ("policy_count", "train_eligible", "generated_at", "timestamp", "commit"):
        assert forbidden not in text
    # The run changed nothing: the live bands are still the frozen ones.
    assert (ACCEPT_THRESHOLD, ADJUDICATE_THRESHOLD) == (0.07, 0.00)


# --- 4. metric correctness on the synthetic snapshots ---------------------------------------
def test_the_reported_metrics_are_the_frozen_policy_over_the_scored_mentions(
    tmp_path: Path,
) -> None:
    paths = stage(tmp_path)
    data, _ = _run(paths)
    metrics = json.loads(data.decode("utf-8"))["metrics"]["final"]
    assert metrics["total"] == _DECLARED_FINAL_COUNT
    assert (metrics["links"], metrics["nils"]) == (40, 10)
    assert metrics["accepted_total"] == metrics["accepted_correct"] == 30
    assert metrics["accepted_wrong_target"] == metrics["accepted_nil"] == 0
    assert metrics["adjudications"] == 10  # 5 tie-gated + 5 below accept but at/above adjudicate
    assert metrics["nil_decisions"] == 10
    assert metrics["auto_accept_precision"] == 1.0
    assert metrics["correct_auto_link_recall"] == 0.75
    assert metrics["routed_link_recall"] == 1.0
    assert metrics["nil_recall"] == 1.0
    assert metrics["adjudication_rate"] == 0.2
    assert json.loads(data.decode("utf-8"))["counts"] == {"final": _DECLARED_FINAL_COUNT}


def test_the_bands_used_are_the_frozen_ones_not_the_initial_ones(tmp_path: Path) -> None:
    assert (FROZEN_ACCEPT, FROZEN_ADJUDICATE) == (Decimal("0.070"), Decimal("0.000"))
    # A 0.050 mention adjudicates at the frozen bands and would be NIL at the ADR 0005 initial ones.
    low = _snapshot("low", tag="brand_product", score="0.050")
    assert reband_snapshot(low, FROZEN_ACCEPT, FROZEN_ADJUDICATE)[0] is LinkBand.ADJUDICATE
    assert reband_snapshot(low, Decimal("0.850"), Decimal("0.500"))[0] is LinkBand.NIL
    paths = stage(tmp_path)
    data, _ = _run(paths)
    assert json.loads(data.decode("utf-8"))["metrics"]["final"]["adjudications"] == 10


def test_the_per_case_tag_breakdown_is_exact_and_in_case_tag_order(tmp_path: Path) -> None:
    paths = stage(tmp_path)
    data, _ = _run(paths)
    diagnostics = json.loads(data.decode("utf-8"))["diagnostics"]
    assert diagnostics["candidate_recall"] == 1.0
    assert diagnostics["top_target_recall"] == 1.0
    by_tag = diagnostics["final_by_case_tag"]
    assert set(by_tag) == {"legal_name", "ambiguous", "brand_product", "nil"}
    assert by_tag["legal_name"]["mentions"] == by_tag["legal_name"]["accepted_correct"] == 30
    assert by_tag["ambiguous"] == {
        "mentions": 5,
        "links": 5,
        "nils": 0,
        "accepted_total": 0,
        "accepted_correct": 0,
        "accepted_wrong_target": 0,
        "accepted_nil": 0,
        "adjudications": 5,
        "nil_decisions": 0,
        "candidate_recall": 1.0,
        "top_target_recall": 1.0,
        "nil_recall": None,
    }
    assert by_tag["brand_product"]["adjudications"] == 5
    assert by_tag["nil"]["nil_decisions"] == 10
    assert by_tag["nil"]["nil_recall"] == 1.0


# --- 5. the CLI ------------------------------------------------------------------------------
def test_the_cli_exposes_only_the_acknowledgement_flag() -> None:
    parser = build_parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert options == {"-h", "--help", "--acknowledge-final-holdout"}
    assert parser.parse_args([]).acknowledge_final_holdout is False
    for rejected in (["--output", "/tmp/x.json"], ["--root", "/tmp"], ["--accept", "0.5"]):
        with pytest.raises(SystemExit):
            parser.parse_args(rejected)


def test_the_cli_without_acknowledgement_refuses_and_writes_nothing() -> None:
    paths = (FINAL_REPORT_PATH, UNSEAL_RECEIPT_PATH)
    before = {path: (path.exists(), path.read_bytes() if path.exists() else None) for path in paths}

    with pytest.raises(EntityLinkingFinalError, match="acknowledge"):
        main([])

    after = {path: (path.exists(), path.read_bytes() if path.exists() else None) for path in paths}
    assert after == before


def test_the_canonical_output_paths_are_fixed_and_beside_each_other() -> None:
    assert CANONICAL_PATHS.output == FINAL_REPORT_PATH
    assert CANONICAL_PATHS.receipt == UNSEAL_RECEIPT_PATH
    assert FINAL_REPORT_PATH.parent == UNSEAL_RECEIPT_PATH.parent
    assert FINAL_REPORT_PATH.name == "entity-linking.json"
    assert (CANONICAL_PATHS.freeze_manifest, CANONICAL_PATHS.frozen_parameters) == (
        FROZEN_MANIFEST_PATH,
        FROZEN_PARAMETERS_PATH,
    )
    assert CANONICAL_PATHS.gold_root == GOLD_ROOT


# --- 6. the scoring seam is the same one the development run used ---------------------------
def test_score_mentions_reproduces_score_corpus_split_for_split() -> None:
    corpus = load_tuning_corpus()
    by_split = score_corpus(corpus)
    for split in (SPLIT_TRAIN, SPLIT_DEVELOPMENT):
        direct = score_mentions(corpus.mentions_for(split), corpus.catalog)
        assert direct == by_split[split]


def test_the_checked_in_development_report_bytes_are_unchanged() -> None:
    path = DEVELOPMENT_DIR / "entity-linking.json"
    assert canonical_json_bytes(build_entity_linking_report()) == path.read_bytes()
    manifest = json.loads(FROZEN_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["development_reports"][DOMAIN]["sha256"] == sha256_file(path)


# --- 7. provenance guardrails ----------------------------------------------------------------
def test_the_command_unseals_in_exactly_one_place_and_never_the_alert_holdout() -> None:
    source = Path(elf.__file__).read_text(encoding="utf-8")
    unseals = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and any(
            keyword.arg == "allow_holdout" and keyword.value.value is True
            for keyword in node.keywords
            if isinstance(keyword.value, ast.Constant)
        )
    ]
    assert len(unseals) == 1  # exactly one call site may unseal, and it is the loader seam
    for forbidden in (
        "alert_episodes",  # the alert holdout stays sealed; this command is entity-only
        "alert_validation",
        "load_corpus(",  # the whole-dataset audit loader would open the holdout unasked
        "requests",
        "httpx",
        "socket.",
        "sqlalchemy",
        "psycopg",
        "Session(",
        "datetime",  # no clock: nothing here is timestamped
        "time.time(",
        "import random",
        "subprocess",
    ):
        assert forbidden not in source


# A C-level audit hook installed once, below any monkeypatch, recording only while armed. It is
# never armed around a run that could reach a real holdout: only around the read-only preflight.
_OPENED: list[str] = []
_SOCKETS: list[str] = []
_RECORDING = {"on": False}


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _RECORDING["on"]:
        return
    if event == "open" and args:
        _OPENED.append(str(args[0]))
    elif event.startswith("socket."):
        _SOCKETS.append(event)


sys.addaudithook(_audit)


def test_the_real_preflight_passes_and_opens_no_split_file_at_all() -> None:
    _OPENED.clear()
    _SOCKETS.clear()
    _RECORDING["on"] = True
    try:
        verified = preflight(CANONICAL_PATHS)
    finally:
        _RECORDING["on"] = False

    assert verified.final_count == _DECLARED_FINAL_COUNT
    assert verified.protocol_sha256 == sha256_file(PROTOCOL_DOC_PATH)
    assert verified.development_report_sha256 == sha256_file(DEVELOPMENT_DIR / "entity-linking.json")
    assert set(verified.tuning_input_hashes) == set(_TUNING_FILES)

    assert _SOCKETS == []  # no network, no DB-over-socket, no LLM call
    assert not any("final_holdout" in path for path in _OPENED)
    gold_opens = {
        Path(path).name for path in _OPENED if "entity_linking" in path and path.endswith(".json")
    }
    # Only the four tuning-visible files are opened (the train/development bytes are re-hashed to
    # prove no drift); the sealed split is known to exist by its manifest declaration alone.
    assert gold_opens <= set(_TUNING_FILES)
    assert _FINAL_FILE not in gold_opens
