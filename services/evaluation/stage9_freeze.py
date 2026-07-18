"""Freeze the four Stage 9 development dispositions and record the one applied change.

``docs/evaluation/stage9-validation-protocol.md`` (``stage9-validation.v1``) runs each domain as a
train / development / freeze / final sequence (§1) and separates selection from the final holdout by
a **freeze** (§6). This module is that freeze. It strict-loads the four canonical development reports
(clustering, entity linking, analogy, alerts) -- rejecting duplicate JSON keys, non-canonical bytes, a
report pinned to different protocol bytes, or an off-disposition domain/status/split -- and writes two
artifacts:

* ``evaluation/stage9/frozen/parameters.json`` -- what each domain's parameters *are* after the
  freeze, one record per domain, keys sorted;
* ``evaluation/stage9/frozen/manifest.json`` -- the exact SHA-256 of the protocol document, of the
  frozen parameters, and of all four development reports, with repo-relative paths and statuses.

Exactly one disposition is *applied* to production: entity linking. Its verified development report
selected **accept 0.070 / adjudicate 0.000** from the ADR 0005 initial **0.850 / 0.500** with the
weight set unchanged, and the freeze both records that (``production_applied = true``) and checks the
live ``services.entities.news_linking`` constants and policy id now equal it. The other three are
recorded unchanged and unapplied -- clustering stays live **0.80** (roadmap 0.82 informational),
analogy stays **0.60**, alerts stay **0.50** with an empty composite gate and a released single-signal
carve-out. The development reports themselves are untouched: each stays its pre-application selection
artifact with ``production_applied = false``; it is the *frozen parameters* that record the
application.

Both writers are canonical, write-once, and atomic. No gold set is loaded, no holdout is opened, and
no database, network, LLM, clock, randomness, or git revision is read. The import direction is
one-way (freeze -> domain evaluators): this module is deliberately not re-exported from
``services.evaluation.__init__``.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.entities.news_linking import (
    ACCEPT_THRESHOLD,
    ADJUDICATE_THRESHOLD,
    ENTITY_LINKING_POLICY_VERSION,
)
from services.evaluation.alert_validation import DECISION_BOUNDARY
from services.evaluation.analogy_validation import ANALOGY_FLOOR
from services.evaluation.calibration import (
    CalibrationError,
    canonical_json_bytes,
    sha256_file,
)
from services.evaluation.clustering_calibration import (
    LIVE_CLUSTERING_THRESHOLD,
    ROADMAP_CLUSTERING_THRESHOLD,
)
from services.evaluation.entity_linking_calibration import (
    INITIAL_ACCEPT,
    INITIAL_ADJUDICATE,
    LINKER_WEIGHT_SET_ID,
)

#: Protocol the freeze is anchored to, and the policy version stamped on both artifacts.
PROTOCOL_ID = "stage9-validation.v1"
POLICY_VERSION = "stage9-validation.v1"
#: Schema ids for the two artifacts, so a later reader can tell what shape it is holding.
PARAMETERS_SCHEMA = "stage9-frozen-parameters.v1"
MANIFEST_SCHEMA = "stage9-frozen-manifest.v1"

#: The entity development report's selected bands -- and, after this freeze, the production bands.
SELECTED_ACCEPT = "0.070"
SELECTED_ADJUDICATE = "0.000"

_REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DOC_PATH = _REPO_ROOT / "docs" / "evaluation" / "stage9-validation-protocol.md"
DEVELOPMENT_DIR = _REPO_ROOT / "evaluation" / "stage9" / "development"
FROZEN_DIR = _REPO_ROOT / "evaluation" / "stage9" / "frozen"
FROZEN_PARAMETERS_PATH = FROZEN_DIR / "parameters.json"
FROZEN_MANIFEST_PATH = FROZEN_DIR / "manifest.json"


class Stage9FreezeError(CalibrationError):
    """A freeze input is missing, non-canonical, stale, tampered with, or off its disposition."""


# --- per-domain expected disposition -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _DomainSpec:
    """One domain's expected development-report disposition and its frozen-record builder."""

    domain: str
    filename: str
    status: str
    split: str
    build_record: Callable[[dict[str, Any]], dict[str, Any]]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Stage9FreezeError(message)


def _fmt(value: Any) -> str:
    return format(value, ".3f")


# --- frozen per-domain records ---------------------------------------------------------------
def _entity_record(report: dict[str, Any]) -> dict[str, Any]:
    """Entity linking: the one applied change. Initial 0.850/0.500, effective 0.070/0.000.

    Cross-checks the report against the ADR 0005 initial point, the selected bands and the unchanged
    weight set, confirms the report is still the pre-application artifact
    (``production_applied = false`` *on the report*), and verifies the live ``news_linking``
    constants and policy id now equal the selected policy -- which is what lets the frozen record say
    ``production_applied = true`` honestly.
    """
    initial = _mapping(report, "initial_parameters")
    selected = _mapping(report, "selected_parameters")
    model_versions = _mapping(report, "model_versions")
    _require(
        (initial.get("accept"), initial.get("adjudicate"))
        == (_fmt(INITIAL_ACCEPT), _fmt(INITIAL_ADJUDICATE)),
        "entity report initial bands are not the ADR 0005 initial 0.850 / 0.500",
    )
    _require(
        (selected.get("accept"), selected.get("adjudicate"))
        == (SELECTED_ACCEPT, SELECTED_ADJUDICATE),
        f"entity report selected bands are not {SELECTED_ACCEPT} / {SELECTED_ADJUDICATE}",
    )
    _require(selected.get("changed_from_initial") is True, "entity report is not changed_from_initial")
    _require(
        selected.get("production_applied") is False,
        "the entity development report must stay the pre-application artifact "
        "(production_applied false); the frozen parameters record the application, not the report",
    )
    _require(
        model_versions.get("linker_weight_set") == LINKER_WEIGHT_SET_ID,
        f"entity report weight set is not the unchanged {LINKER_WEIGHT_SET_ID}: only the two band "
        "thresholds were calibrated",
    )
    return {
        "domain": "entity_linking",
        "status": "calibrated",
        "initial": {"accept": initial["accept"], "adjudicate": initial["adjudicate"]},
        "selected": {"accept": selected["accept"], "adjudicate": selected["adjudicate"]},
        "effective": {"accept": selected["accept"], "adjudicate": selected["adjudicate"]},
        "changed": True,
        "production_applied": True,
        "production_policy_version": ENTITY_LINKING_POLICY_VERSION,
        "weight_set": LINKER_WEIGHT_SET_ID,
        "weight_set_changed": False,
    }


def _clustering_record(report: dict[str, Any]) -> dict[str, Any]:
    """Clustering: not verifiable, live 0.80 retained, roadmap 0.82 informational, nothing applied."""
    effective = _mapping(report, "effective_parameters")
    _require(
        effective.get("clustering_threshold") == LIVE_CLUSTERING_THRESHOLD,
        "clustering report threshold is not the live 0.80",
    )
    _require(effective.get("changed") is False, "clustering report claims a change")
    _require(
        effective.get("roadmap_threshold") == ROADMAP_CLUSTERING_THRESHOLD,
        "clustering report roadmap threshold is not 0.82",
    )
    _require(
        effective.get("roadmap_threshold_informational_only") is True,
        "clustering report does not mark the roadmap threshold informational only",
    )
    _require(report.get("selected_parameters") is None, "clustering report selected a parameter")
    _require(report.get("metrics") is None, "clustering report carries metrics it cannot have run")
    return {
        "domain": "clustering",
        "status": "not_verifiable",
        "effective": {"clustering_threshold": effective["clustering_threshold"]},
        "roadmap_threshold": effective["roadmap_threshold"],
        "roadmap_threshold_informational_only": True,
        "changed": False,
        "production_applied": False,
    }


def _analogy_record(report: dict[str, Any]) -> dict[str, Any]:
    """Analogy: evaluation-only, the 0.60 floor retained, nothing selected, nothing applied."""
    selected = _mapping(report, "selected_parameters")
    _require(selected.get("selection") == "none", "analogy report selected a floor")
    _require(selected.get("floor") == ANALOGY_FLOOR, "analogy report floor is not the live 0.60")
    _require(selected.get("floor_changed") is False, "analogy report claims a floor change")
    _require(selected.get("production_applied") is False, "analogy report claims it was applied")
    return {
        "domain": "analogy",
        "status": "evaluation_only",
        "effective": {"floor": selected["floor"]},
        "changed": False,
        "production_applied": False,
    }


def _alerts_record(report: dict[str, Any]) -> dict[str, Any]:
    """Alerts: not verifiable, 0.50 boundary retained, empty composite gate, single-signal released."""
    selected = _mapping(report, "selected_parameters")
    gate = _mapping(report, "gate_decision")
    composite = _mapping(gate, "composite")
    single = _mapping(gate, "single_signal")
    _require(
        selected.get("decision_boundary") == DECISION_BOUNDARY,
        "alerts report decision boundary is not the live 0.50",
    )
    _require(
        selected.get("decision_boundary_changed") is False,
        "alerts report claims a decision-boundary change",
    )
    _require(selected.get("production_applied") is False, "alerts report claims it was applied")
    _require(gate.get("evidence_count") == 0, "alerts gate evidence is not empty")
    _require(
        composite.get("experimental") is True and composite.get("released") is False,
        "alerts composite gate is not fail-closed (experimental / not released)",
    )
    _require(
        single.get("released") is True and single.get("experimental") is False,
        "alerts single-signal carve-out is not released",
    )
    return {
        "domain": "alerts",
        "status": "not_verifiable",
        "effective": {"decision_boundary": selected["decision_boundary"]},
        "changed": False,
        "production_applied": False,
        "gate": {
            "evidence_count": 0,
            "composite": {
                "released": False,
                "experimental": True,
                "blocking_windows": list(composite.get("blocking_windows", ())),
            },
            "single_signal": {
                "released": True,
                "experimental": False,
                "blocking_windows": list(single.get("blocking_windows", ())),
            },
        },
    }


_DOMAIN_SPECS: tuple[_DomainSpec, ...] = (
    _DomainSpec("alerts", "alerts.json", "not_verifiable", "development", _alerts_record),
    _DomainSpec("analogy", "analogy.json", "evaluation_only", "unsplit", _analogy_record),
    _DomainSpec("clustering", "clustering.json", "not_verifiable", "development", _clustering_record),
    _DomainSpec(
        "entity_linking", "entity-linking.json", "calibrated", "development", _entity_record
    ),
)


# --- strict, canonical JSON loading ---------------------------------------------------------
def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object hook that refuses a duplicate key rather than silently keeping the last one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Stage9FreezeError(f"duplicate JSON key {key!r} in a freeze input")
        result[key] = value
    return result


def load_canonical_json(path: Path) -> tuple[dict[str, Any], bytes]:
    """Strict-load a JSON object: readable, UTF-8, no duplicate keys, exactly canonical bytes.

    Returns the parsed object and its exact on-disk bytes. Re-serializing the parse must reproduce
    those bytes byte-for-byte, so a reordered, reformatted, or hand-edited artifact is rejected here
    rather than silently re-hashed into the manifest.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Stage9FreezeError(f"missing freeze input {path}: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stage9FreezeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Stage9FreezeError(f"{path} is not a JSON object")
    if canonical_json_bytes(parsed) != raw:
        raise Stage9FreezeError(
            f"{path} is not canonical bytes (reordered, reformatted, or otherwise edited)"
        )
    return parsed, raw


def _mapping(report: dict[str, Any], key: str) -> dict[str, Any]:
    value = report.get(key)
    if not isinstance(value, dict):
        raise Stage9FreezeError(f"freeze input field {key!r} is missing or not an object")
    return value


def _rel(path: Path) -> str:
    """The path relative to the repository root (POSIX), so the artifacts are machine-independent."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _load_report(path: Path, protocol_hash: str, spec: _DomainSpec) -> dict[str, Any]:
    """Strict-load one development report: canonical bytes, pinned protocol, expected disposition."""
    parsed, _ = load_canonical_json(path)
    protocol = _mapping(parsed, "protocol")
    _require(protocol.get("id") == PROTOCOL_ID, f"{path} does not name protocol {PROTOCOL_ID}")
    _require(
        protocol.get("sha256") == protocol_hash,
        f"{path} protocol hash {protocol.get('sha256')!r} does not match the protocol bytes "
        f"{protocol_hash!r}: the report is stale",
    )
    _require(parsed.get("domain") == spec.domain, f"{path} domain is not {spec.domain!r}")
    _require(parsed.get("status") == spec.status, f"{path} status is not {spec.status!r}")
    _require(parsed.get("split") == spec.split, f"{path} split is not {spec.split!r}")
    return parsed


# --- the frozen parameters --------------------------------------------------------------------
def build_frozen_parameters(
    *, development_dir: Path = DEVELOPMENT_DIR, protocol_path: Path = PROTOCOL_DOC_PATH
) -> dict[str, Any]:
    """Assemble the canonical §6 frozen parameters from the four development reports.

    Hashes the protocol document once, strict-loads each report against its expected disposition, and
    records one parameter block per domain (keys sorted, so the four domains land in a fixed order):
    entity linking applied at 0.070/0.000, the other three unchanged and unapplied. Verifies the live
    production constants before claiming the entity application. Deterministic and offline.
    """
    protocol_hash = sha256_file(protocol_path)
    domains = {
        spec.domain: spec.build_record(
            _load_report(development_dir / spec.filename, protocol_hash, spec)
        )
        for spec in _DOMAIN_SPECS
    }
    parameters = {
        "schema": PARAMETERS_SCHEMA,
        "protocol": {"id": PROTOCOL_ID, "sha256": protocol_hash},
        "policy_version": POLICY_VERSION,
        "domains": domains,
    }
    verify_production_matches(parameters)
    return parameters


def verify_production_matches(parameters: dict[str, Any]) -> None:
    """Check the live production constants equal the frozen entity-linking parameters.

    The frozen record claims ``production_applied = true`` for entity linking only; this is the check
    behind that claim. It compares the live accept/adjudicate thresholds *and* the live band-policy id
    from :mod:`services.entities.news_linking` against the frozen effective values, and asserts the
    other three domains are recorded as unapplied.
    """
    domains = _mapping(parameters, "domains")
    _require(
        set(domains) == {spec.domain for spec in _DOMAIN_SPECS},
        f"frozen parameters must cover exactly {sorted(spec.domain for spec in _DOMAIN_SPECS)}",
    )
    entity = _mapping(domains, "entity_linking")
    effective = _mapping(entity, "effective")
    _require(entity.get("production_applied") is True, "frozen entity parameters are not applied")
    _require(
        (effective.get("accept"), effective.get("adjudicate"))
        == (_fmt(ACCEPT_THRESHOLD), _fmt(ADJUDICATE_THRESHOLD)),
        f"live entity thresholds {ACCEPT_THRESHOLD} / {ADJUDICATE_THRESHOLD} do not equal the frozen "
        f"{effective.get('accept')} / {effective.get('adjudicate')}: the freeze cannot claim it applied",
    )
    _require(
        entity.get("production_policy_version") == ENTITY_LINKING_POLICY_VERSION,
        f"live band policy {ENTITY_LINKING_POLICY_VERSION!r} does not equal the frozen "
        f"{entity.get('production_policy_version')!r}",
    )
    _require(
        entity.get("weight_set") == LINKER_WEIGHT_SET_ID
        and entity.get("weight_set_changed") is False,
        f"the frozen entity weight set must be the unchanged {LINKER_WEIGHT_SET_ID}",
    )
    for domain, record in domains.items():
        if domain == "entity_linking":
            continue
        _require(
            record.get("production_applied") is False and record.get("changed") is False,
            f"{domain} was recorded as changed or applied; only entity linking is applied",
        )


# --- the frozen manifest -----------------------------------------------------------------------
def build_freeze_manifest(
    *,
    parameters_path: Path = FROZEN_PARAMETERS_PATH,
    development_dir: Path = DEVELOPMENT_DIR,
    protocol_path: Path = PROTOCOL_DOC_PATH,
) -> dict[str, Any]:
    """Assemble the canonical §6 manifest: the exact hash of every artifact the freeze rests on.

    Strict-loads and re-derives the frozen parameters from the four development reports and requires
    the on-disk ``parameters.json`` to be byte-identical to that rebuild -- so a stale, tampered, or
    hand-edited parameter file is rejected instead of hashed. Records repo-relative paths, SHA-256s
    and statuses for the protocol document, the frozen parameters, and all four reports. Carries no
    timestamp and no git revision.
    """
    protocol_hash = sha256_file(protocol_path)
    parsed_parameters, parameters_bytes = load_canonical_json(parameters_path)
    expected = canonical_json_bytes(
        build_frozen_parameters(development_dir=development_dir, protocol_path=protocol_path)
    )
    _require(
        parameters_bytes == expected,
        f"{parameters_path} does not match the parameters rebuilt from the development reports: "
        "it is stale or was edited by hand",
    )
    verify_production_matches(parsed_parameters)

    reports: dict[str, Any] = {}
    for spec in _DOMAIN_SPECS:
        path = development_dir / spec.filename
        _load_report(path, protocol_hash, spec)
        reports[spec.domain] = {
            "path": _rel(path),
            "sha256": sha256_file(path),
            "status": spec.status,
        }

    return {
        "schema": MANIFEST_SCHEMA,
        "protocol": {"id": PROTOCOL_ID, "path": _rel(protocol_path), "sha256": protocol_hash},
        "policy_version": POLICY_VERSION,
        "frozen_parameters": {
            "path": _rel(parameters_path),
            "sha256": sha256_file(parameters_path),
        },
        "development_reports": reports,
    }


# --- write-once atomic writers ------------------------------------------------------------------
def _write_once(path: Path, data: bytes, description: str) -> bytes:
    """Write ``data`` to ``path`` once, atomically. Refuses to overwrite; returns the exact bytes."""
    if path.exists():
        raise Stage9FreezeError(f"refusing to overwrite existing {description} at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def write_frozen_parameters(
    output_path: Path | str = FROZEN_PARAMETERS_PATH,
    *,
    development_dir: Path = DEVELOPMENT_DIR,
    protocol_path: Path = PROTOCOL_DOC_PATH,
) -> bytes:
    """Write the canonical frozen parameters, building and validating them before touching disk."""
    data = canonical_json_bytes(
        build_frozen_parameters(development_dir=development_dir, protocol_path=protocol_path)
    )
    return _write_once(Path(output_path), data, "frozen parameters")


def write_freeze_manifest(
    output_path: Path | str = FROZEN_MANIFEST_PATH,
    *,
    parameters_path: Path = FROZEN_PARAMETERS_PATH,
    development_dir: Path = DEVELOPMENT_DIR,
    protocol_path: Path = PROTOCOL_DOC_PATH,
) -> bytes:
    """Write the canonical freeze manifest, building and validating it before touching disk."""
    data = canonical_json_bytes(
        build_freeze_manifest(
            parameters_path=Path(parameters_path),
            development_dir=development_dir,
            protocol_path=protocol_path,
        )
    )
    return _write_once(Path(output_path), data, "freeze manifest")


def verify_frozen_artifacts(
    *,
    parameters_path: Path = FROZEN_PARAMETERS_PATH,
    manifest_path: Path = FROZEN_MANIFEST_PATH,
    development_dir: Path = DEVELOPMENT_DIR,
    protocol_path: Path = PROTOCOL_DOC_PATH,
) -> dict[str, Any]:
    """Re-verify the checked-in freeze end to end, returning the parsed manifest.

    Rebuilds the manifest from the protocol, the parameters and the four reports and requires the
    on-disk ``manifest.json`` to be byte-identical -- which transitively re-checks the parameters, the
    reports, every hash, and the live production constants.
    """
    parsed, raw = load_canonical_json(Path(manifest_path))
    expected = canonical_json_bytes(
        build_freeze_manifest(
            parameters_path=Path(parameters_path),
            development_dir=development_dir,
            protocol_path=protocol_path,
        )
    )
    _require(
        raw == expected,
        f"{manifest_path} does not match the manifest rebuilt from its inputs: it is stale or tampered",
    )
    return parsed


def main(argv: Iterable[str] | None = None) -> int:
    """CLI: write the frozen parameters then the manifest (no clock, randomness, or git revision)."""
    parser = argparse.ArgumentParser(description="Stage 9 freeze-and-apply artifact writer.")
    parser.add_argument("--parameters", default=str(FROZEN_PARAMETERS_PATH))
    parser.add_argument("--manifest", default=str(FROZEN_MANIFEST_PATH))
    args = parser.parse_args(list(argv) if argv is not None else None)
    parameters = write_frozen_parameters(args.parameters)
    manifest = write_freeze_manifest(args.manifest, parameters_path=Path(args.parameters))
    print(f"wrote {len(parameters)} bytes to {args.parameters}")
    print(f"wrote {len(manifest)} bytes to {args.manifest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEVELOPMENT_DIR",
    "FROZEN_DIR",
    "FROZEN_MANIFEST_PATH",
    "FROZEN_PARAMETERS_PATH",
    "MANIFEST_SCHEMA",
    "PARAMETERS_SCHEMA",
    "POLICY_VERSION",
    "PROTOCOL_DOC_PATH",
    "PROTOCOL_ID",
    "SELECTED_ACCEPT",
    "SELECTED_ADJUDICATE",
    "Stage9FreezeError",
    "build_freeze_manifest",
    "build_frozen_parameters",
    "load_canonical_json",
    "verify_frozen_artifacts",
    "verify_production_matches",
    "write_freeze_manifest",
    "write_frozen_parameters",
]
