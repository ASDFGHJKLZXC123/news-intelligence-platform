"""Unit tests for the Stage 9 freeze: frozen parameters, freeze manifest, and the one applied change.

Every adversarial case runs against a **temp copy** of the four development reports -- the checked-in
reports and the two frozen artifacts are only ever read. Two tests prove the checked-in bytes are
exactly what the builders produce, and one records their SHA-256s so a silent edit shows up here.

Nothing here loads a gold set, opens a holdout, or touches a database or the network: one test scans
the freeze module's imports, and one installs a process audit hook to prove no gold or holdout path is
opened while both artifacts are built.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from services.entities.news_linking import (
    ACCEPT_THRESHOLD,
    ADJUDICATE_THRESHOLD,
    ENTITY_LINKING_POLICY_VERSION,
    LinkBand,
    band_for_score,
)
from services.evaluation import stage9_freeze
from services.evaluation.calibration import canonical_json_bytes, sha256_file
from services.evaluation.entity_linking_calibration import LINKER_WEIGHT_SET_ID
from services.evaluation.stage9_freeze import (
    DEVELOPMENT_DIR,
    FROZEN_MANIFEST_PATH,
    FROZEN_PARAMETERS_PATH,
    MANIFEST_SCHEMA,
    PARAMETERS_SCHEMA,
    POLICY_VERSION,
    PROTOCOL_DOC_PATH,
    PROTOCOL_ID,
    SELECTED_ACCEPT,
    SELECTED_ADJUDICATE,
    Stage9FreezeError,
    build_freeze_manifest,
    build_frozen_parameters,
    verify_frozen_artifacts,
    verify_production_matches,
    write_freeze_manifest,
    write_frozen_parameters,
)

_REPORTS = {
    "alerts": "alerts.json",
    "analogy": "analogy.json",
    "clustering": "clustering.json",
    "entity_linking": "entity-linking.json",
}
_DOMAINS = tuple(sorted(_REPORTS))


# --- a process audit hook that records file opens on demand --------------------------------
_OPENED: list[str] = []
_RECORDING = {"on": False}


def _audit_open(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _RECORDING["on"] and args:
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit_open)


# --- temp-copy helpers ----------------------------------------------------------------------
def _copy_reports(tmp_path: Path) -> Path:
    """A writable copy of the four checked-in development reports; the originals stay read-only."""
    target = tmp_path / "development"
    target.mkdir()
    for filename in _REPORTS.values():
        shutil.copy2(DEVELOPMENT_DIR / filename, target / filename)
    return target


def _rewrite(path: Path, mutate: Any) -> None:
    """Re-canonicalize one report after ``mutate`` edits its parsed body in place."""
    parsed = json.loads(path.read_text(encoding="utf-8"))
    mutate(parsed)
    path.write_bytes(canonical_json_bytes(parsed))


def _build(development: Path) -> dict[str, Any]:
    return build_frozen_parameters(development_dir=development, protocol_path=PROTOCOL_DOC_PATH)


# --- the checked-in artifacts are exactly what the builders produce -------------------------
def test_checked_in_parameters_are_the_canonical_builder_bytes() -> None:
    assert FROZEN_PARAMETERS_PATH.read_bytes() == canonical_json_bytes(build_frozen_parameters())


def test_checked_in_manifest_is_the_canonical_builder_bytes() -> None:
    assert FROZEN_MANIFEST_PATH.read_bytes() == canonical_json_bytes(build_freeze_manifest())


def test_verify_frozen_artifacts_accepts_the_checked_in_freeze() -> None:
    manifest = verify_frozen_artifacts()
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["policy_version"] == POLICY_VERSION
    assert manifest["protocol"] == {
        "id": PROTOCOL_ID,
        "path": "docs/evaluation/stage9-validation-protocol.md",
        "sha256": sha256_file(PROTOCOL_DOC_PATH),
    }


def test_manifest_hashes_are_the_exact_bytes_of_every_input() -> None:
    manifest = json.loads(FROZEN_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["frozen_parameters"] == {
        "path": "evaluation/stage9/frozen/parameters.json",
        "sha256": sha256_file(FROZEN_PARAMETERS_PATH),
    }
    reports = manifest["development_reports"]
    assert tuple(sorted(reports)) == _DOMAINS
    for domain, filename in _REPORTS.items():
        path = DEVELOPMENT_DIR / filename
        assert reports[domain]["path"] == f"evaluation/stage9/development/{filename}"
        assert reports[domain]["sha256"] == sha256_file(path)
        assert reports[domain]["status"] == json.loads(path.read_text(encoding="utf-8"))["status"]


def test_manifest_records_no_timestamp_or_git_revision() -> None:
    raw = FROZEN_MANIFEST_PATH.read_text(encoding="utf-8")
    for forbidden in ("generated_at", "timestamp", "git", "revision", "commit"):
        assert forbidden not in raw


# --- the four dispositions --------------------------------------------------------------------
def test_parameters_carry_exactly_the_four_sorted_domains() -> None:
    parameters = json.loads(FROZEN_PARAMETERS_PATH.read_text(encoding="utf-8"))
    assert parameters["schema"] == PARAMETERS_SCHEMA
    assert parameters["protocol"] == {"id": PROTOCOL_ID, "sha256": sha256_file(PROTOCOL_DOC_PATH)}
    assert tuple(parameters["domains"]) == _DOMAINS


def test_only_entity_linking_is_applied_and_changed() -> None:
    domains = build_frozen_parameters()["domains"]
    applied = {name for name, record in domains.items() if record["production_applied"]}
    changed = {name for name, record in domains.items() if record["changed"]}
    assert applied == changed == {"entity_linking"}


def test_entity_disposition_records_the_applied_band_change() -> None:
    entity = build_frozen_parameters()["domains"]["entity_linking"]
    assert entity["status"] == "calibrated"
    assert entity["initial"] == {"accept": "0.850", "adjudicate": "0.500"}
    assert entity["selected"] == {"accept": SELECTED_ACCEPT, "adjudicate": SELECTED_ADJUDICATE}
    assert entity["effective"] == entity["selected"]
    assert entity["production_policy_version"] == ENTITY_LINKING_POLICY_VERSION
    assert entity["weight_set"] == LINKER_WEIGHT_SET_ID
    assert entity["weight_set_changed"] is False


def test_entity_development_report_stays_the_pre_application_artifact() -> None:
    report = json.loads((DEVELOPMENT_DIR / "entity-linking.json").read_text(encoding="utf-8"))
    assert report["selected_parameters"]["production_applied"] is False
    assert report["model_versions"]["linker_weight_set"] == LINKER_WEIGHT_SET_ID


def test_clustering_analogy_and_alerts_are_recorded_unchanged() -> None:
    domains = build_frozen_parameters()["domains"]
    clustering = domains["clustering"]
    assert clustering["status"] == "not_verifiable"
    assert clustering["effective"] == {"clustering_threshold": "0.80"}
    assert clustering["roadmap_threshold"] == "0.82"
    assert clustering["roadmap_threshold_informational_only"] is True

    analogy = domains["analogy"]
    assert analogy["status"] == "evaluation_only"
    assert analogy["effective"] == {"floor": "0.60"}

    alerts = domains["alerts"]
    assert alerts["status"] == "not_verifiable"
    assert alerts["effective"] == {"decision_boundary": "0.50"}
    assert alerts["gate"]["evidence_count"] == 0
    assert alerts["gate"]["composite"] == {
        "released": False,
        "experimental": True,
        "blocking_windows": ["2007-2009", "2020", "2023"],
    }
    assert alerts["gate"]["single_signal"] == {
        "released": True,
        "experimental": False,
        "blocking_windows": [],
    }


#: Decimal places each domain reports its thresholds at: alerts/analogy/clustering 2, entity 3.
_THRESHOLD_DECIMALS = {"alerts": 2, "analogy": 2, "clustering": 2, "entity_linking": 3}
_BAND_KEYS = ("initial", "selected", "effective")


def test_every_frozen_threshold_leaf_is_an_exact_decimal_string() -> None:
    """No threshold or band is ever a float: each is a fixed-precision decimal string.

    A float leaf reintroduces binary rounding into a frozen artifact (0.60 serializing as ``0.6``),
    so the whole freeze is checked, not just the domain that regressed. Booleans and statuses are
    deliberately left as-is.
    """
    domains = json.loads(FROZEN_PARAMETERS_PATH.read_text(encoding="utf-8"))["domains"]
    assert tuple(sorted(domains)) == _DOMAINS
    leaves = 0
    for name, record in domains.items():
        places = _THRESHOLD_DECIMALS[name]
        values = [value for key in _BAND_KEYS if key in record for value in record[key].values()]
        if "roadmap_threshold" in record:
            values.append(record["roadmap_threshold"])
        assert values, name
        for value in values:
            assert isinstance(value, str), (name, value)
            assert re.fullmatch(rf"\d+\.\d{{{places}}}", value), (name, value)
            leaves += 1
        # Dispositions stay typed as themselves -- nothing was stringified wholesale.
        assert isinstance(record["status"], str)
        assert isinstance(record["changed"], bool)
        assert isinstance(record["production_applied"], bool)
    assert leaves == 10  # alerts 1, analogy 1, clustering 2, entity linking 6


# --- the frozen parameters and the live production policy agree -------------------------------
def test_live_production_constants_equal_the_frozen_entity_parameters() -> None:
    entity = json.loads(FROZEN_PARAMETERS_PATH.read_text(encoding="utf-8"))["domains"][
        "entity_linking"
    ]
    assert (ACCEPT_THRESHOLD, ADJUDICATE_THRESHOLD) == (
        float(entity["effective"]["accept"]),
        float(entity["effective"]["adjudicate"]),
    )
    assert ENTITY_LINKING_POLICY_VERSION == entity["production_policy_version"]
    assert ENTITY_LINKING_POLICY_VERSION != POLICY_VERSION  # the band policy is its own id


def test_live_banding_matches_the_frozen_bands_at_the_boundaries() -> None:
    accept = float(SELECTED_ACCEPT)
    assert band_for_score(accept) is LinkBand.ACCEPT
    assert band_for_score(0.0699) is LinkBand.ADJUDICATE
    assert band_for_score(float(SELECTED_ADJUDICATE)) is LinkBand.ADJUDICATE


@pytest.mark.parametrize(
    ("attribute", "value", "match"),
    [
        ("ACCEPT_THRESHOLD", 0.85, "cannot claim it applied"),
        ("ADJUDICATE_THRESHOLD", 0.50, "cannot claim it applied"),
        ("ENTITY_LINKING_POLICY_VERSION", "some-other-policy.v1", "does not equal the frozen"),
        ("LINKER_WEIGHT_SET_ID", "reweighted.v2", "must be the unchanged"),
    ],
)
def test_production_drift_from_the_frozen_parameters_is_detected(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object, match: str
) -> None:
    """If production drifts away from what was frozen, the checked-in freeze stops verifying."""
    frozen = json.loads(FROZEN_PARAMETERS_PATH.read_text(encoding="utf-8"))
    monkeypatch.setattr(stage9_freeze, attribute, value)
    with pytest.raises(Stage9FreezeError, match=match):
        verify_production_matches(frozen)
    with pytest.raises(Stage9FreezeError):
        build_freeze_manifest()


@pytest.mark.parametrize("attribute", ["ACCEPT_THRESHOLD", "ADJUDICATE_THRESHOLD"])
def test_the_freeze_refuses_to_claim_an_application_production_has_not_made(
    monkeypatch: pytest.MonkeyPatch, attribute: str
) -> None:
    monkeypatch.setattr(stage9_freeze, attribute, 0.5)
    with pytest.raises(Stage9FreezeError, match="cannot claim it applied"):
        build_frozen_parameters()


def test_verify_production_matches_rejects_a_second_applied_domain() -> None:
    parameters = build_frozen_parameters()
    parameters["domains"]["clustering"]["production_applied"] = True
    with pytest.raises(Stage9FreezeError, match="only entity linking is applied"):
        verify_production_matches(parameters)


def test_verify_production_matches_rejects_a_missing_domain() -> None:
    parameters = build_frozen_parameters()
    del parameters["domains"]["analogy"]
    with pytest.raises(Stage9FreezeError, match="must cover exactly"):
        verify_production_matches(parameters)


# --- missing, tampered and stale inputs --------------------------------------------------------
def test_a_missing_development_report_is_rejected(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    (development / "analogy.json").unlink()
    with pytest.raises(Stage9FreezeError, match="missing freeze input"):
        _build(development)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    path = development / "alerts.json"
    text = path.read_text(encoding="utf-8")
    path.write_text('{"domain": "alerts", ' + text[1:], encoding="utf-8")
    with pytest.raises(Stage9FreezeError, match="duplicate JSON key"):
        _build(development)


def test_non_canonical_bytes_are_rejected(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    path = development / "clustering.json"
    parsed = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
    with pytest.raises(Stage9FreezeError, match="not canonical bytes"):
        _build(development)


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    (development / "analogy.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(Stage9FreezeError, match="not valid JSON"):
        _build(development)


def test_a_report_pinned_to_other_protocol_bytes_is_stale(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    _rewrite(
        development / "entity-linking.json",
        lambda report: report["protocol"].__setitem__("sha256", "b" * 64),
    )
    with pytest.raises(Stage9FreezeError, match="stale"):
        _build(development)


def test_a_report_naming_another_protocol_is_rejected(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    _rewrite(
        development / "alerts.json",
        lambda report: report["protocol"].__setitem__("id", "stage9-validation.v2"),
    )
    with pytest.raises(Stage9FreezeError, match="does not name protocol"):
        _build(development)


@pytest.mark.parametrize(
    ("filename", "field", "value", "match"),
    [
        ("clustering.json", "domain", "entity_linking", "domain is not"),
        ("clustering.json", "status", "calibrated", "status is not"),
        ("analogy.json", "split", "development", "split is not"),
    ],
)
def test_an_off_disposition_report_is_rejected(
    tmp_path: Path, filename: str, field: str, value: str, match: str
) -> None:
    development = _copy_reports(tmp_path)
    _rewrite(development / filename, lambda report: report.__setitem__(field, value))
    with pytest.raises(Stage9FreezeError, match=match):
        _build(development)


@pytest.mark.parametrize(
    ("filename", "mutate", "match"),
    [
        (
            "entity-linking.json",
            lambda r: r["selected_parameters"].__setitem__("production_applied", True),
            "pre-application artifact",
        ),
        (
            "entity-linking.json",
            lambda r: r["selected_parameters"].__setitem__("accept", "0.075"),
            "selected bands are not",
        ),
        (
            "entity-linking.json",
            lambda r: r["initial_parameters"].__setitem__("accept", "0.800"),
            "initial bands are not",
        ),
        (
            "entity-linking.json",
            lambda r: r["model_versions"].__setitem__("linker_weight_set", "reweighted.v2"),
            "weight set is not the unchanged",
        ),
        (
            "clustering.json",
            lambda r: r["effective_parameters"].__setitem__("changed", True),
            "claims a change",
        ),
        (
            "clustering.json",
            lambda r: r["effective_parameters"].__setitem__("clustering_threshold", "0.82"),
            "not the live 0.80",
        ),
        (
            "analogy.json",
            lambda r: r["selected_parameters"].__setitem__("floor_changed", True),
            "claims a floor change",
        ),
        (
            "analogy.json",
            lambda r: r["selected_parameters"].__setitem__("production_applied", True),
            "claims it was applied",
        ),
        (
            "alerts.json",
            lambda r: r["selected_parameters"].__setitem__("decision_boundary", "0.60"),
            "not the live 0.50",
        ),
        (
            "alerts.json",
            lambda r: r["gate_decision"]["composite"].__setitem__("released", True),
            "not fail-closed",
        ),
        (
            "alerts.json",
            lambda r: r["gate_decision"]["single_signal"].__setitem__("released", False),
            "carve-out is not released",
        ),
        (
            "alerts.json",
            lambda r: r["gate_decision"].__setitem__("evidence_count", 3),
            "evidence is not empty",
        ),
    ],
)
def test_a_tampered_disposition_is_rejected(
    tmp_path: Path, filename: str, mutate: Any, match: str
) -> None:
    development = _copy_reports(tmp_path)
    _rewrite(development / filename, mutate)
    with pytest.raises(Stage9FreezeError, match=match):
        _build(development)


def test_the_manifest_rejects_stale_frozen_parameters(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    parameters = tmp_path / "parameters.json"
    write_frozen_parameters(parameters, development_dir=development)
    stale = json.loads(parameters.read_text(encoding="utf-8"))
    stale["domains"]["clustering"]["effective"]["clustering_threshold"] = "0.82"
    parameters.write_bytes(canonical_json_bytes(stale))
    with pytest.raises(Stage9FreezeError, match="does not match the parameters rebuilt"):
        build_freeze_manifest(parameters_path=parameters, development_dir=development)


def test_the_manifest_rejects_non_canonical_frozen_parameters(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    parameters = tmp_path / "parameters.json"
    write_frozen_parameters(parameters, development_dir=development)
    parsed = json.loads(parameters.read_text(encoding="utf-8"))
    parameters.write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
    with pytest.raises(Stage9FreezeError, match="not canonical bytes"):
        build_freeze_manifest(parameters_path=parameters, development_dir=development)


def test_verify_frozen_artifacts_rejects_a_tampered_manifest(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    parameters = tmp_path / "parameters.json"
    manifest = tmp_path / "manifest.json"
    write_frozen_parameters(parameters, development_dir=development)
    write_freeze_manifest(manifest, parameters_path=parameters, development_dir=development)
    tampered = json.loads(manifest.read_text(encoding="utf-8"))
    tampered["development_reports"]["analogy"]["sha256"] = "c" * 64
    manifest.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(Stage9FreezeError, match="stale or tampered"):
        verify_frozen_artifacts(
            parameters_path=parameters, manifest_path=manifest, development_dir=development
        )


# --- write-once, atomic writers -----------------------------------------------------------------
def test_writers_round_trip_into_a_temp_freeze(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    parameters = tmp_path / "frozen" / "parameters.json"
    manifest = tmp_path / "frozen" / "manifest.json"
    parameter_bytes = write_frozen_parameters(parameters, development_dir=development)
    manifest_bytes = write_freeze_manifest(
        manifest, parameters_path=parameters, development_dir=development
    )
    assert parameters.read_bytes() == parameter_bytes == FROZEN_PARAMETERS_PATH.read_bytes()
    assert manifest.read_bytes() == manifest_bytes
    assert json.loads(manifest_bytes)["frozen_parameters"]["sha256"] == sha256_file(parameters)
    assert not list(parameters.parent.glob("*.tmp"))
    verify_frozen_artifacts(
        parameters_path=parameters, manifest_path=manifest, development_dir=development
    )


def test_writers_refuse_to_overwrite(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    parameters = tmp_path / "parameters.json"
    manifest = tmp_path / "manifest.json"
    write_frozen_parameters(parameters, development_dir=development)
    write_freeze_manifest(manifest, parameters_path=parameters, development_dir=development)
    with pytest.raises(Stage9FreezeError, match="refusing to overwrite"):
        write_frozen_parameters(parameters, development_dir=development)
    with pytest.raises(Stage9FreezeError, match="refusing to overwrite"):
        write_freeze_manifest(manifest, parameters_path=parameters, development_dir=development)


def test_a_rejected_build_writes_nothing(tmp_path: Path) -> None:
    development = _copy_reports(tmp_path)
    _rewrite(development / "alerts.json", lambda r: r["gate_decision"].__setitem__("evidence_count", 1))
    parameters = tmp_path / "frozen" / "parameters.json"
    with pytest.raises(Stage9FreezeError):
        write_frozen_parameters(parameters, development_dir=development)
    assert not parameters.parent.exists()


# --- source and runtime guards: never a holdout, a gold loader, a database or the network -------
def test_freeze_module_imports_no_data_or_io_dependency() -> None:
    tree = ast.parse(Path(stage9_freeze.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) and node.module else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {"__future__", "argparse", "json", "os", "collections", "dataclasses",
                        "pathlib", "typing", "services"}
    source = Path(stage9_freeze.__file__).read_text(encoding="utf-8")
    for forbidden in ("_gold", "load_corpus", "load_split", "sqlalchemy", "httpx", "requests.",
                      "datetime.", "random.", "subprocess"):
        assert forbidden not in source


def test_building_both_artifacts_opens_no_gold_or_holdout_path() -> None:
    _OPENED.clear()
    _RECORDING["on"] = True
    try:
        build_frozen_parameters()
        build_freeze_manifest()
    finally:
        _RECORDING["on"] = False
    assert _OPENED  # the protocol, the reports and the parameters were read
    for path in _OPENED:
        assert "evaluation/gold" not in path
        assert "holdout" not in path
