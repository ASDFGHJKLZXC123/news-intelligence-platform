"""The one-time entity-linking final-holdout command (``stage9-validation.v1`` §1, §5, §6).

This is the **only** code path permitted to pass the entity gold loader's ``allow_holdout=True``
(protocol §2, §6). It reports a number; it selects nothing. The sealed 50-mention final holdout is
scored exactly once, at the **frozen** bands (accept ``0.070`` / adjudicate ``0.000``), with the real
:class:`~services.entities.news_linking.NewsEntityLinker` and the in-memory identity store the
development run used -- no sweep, no candidate grid, no comparison of policies, and no production
constant is touched by the result. §1's rule is absolute: an unsatisfactory final number is a
finding, not a do-over; a retry needs a new protocol version and a new, unseen holdout.

The command is deliberately unforgiving, in this order:

1. **No acknowledgement, no run.** Without the operator's explicit flag it refuses before reading a
   single file.
2. **Never twice.** If the canonical report *or* the canonical unseal receipt already exists it
   refuses before any holdout access.
3. **Preflight before unseal.** The whole §6 precondition set -- the freeze verified end to end,
   every manifest hash re-checked against the bytes on disk, the frozen entity disposition and the
   development report strict-loaded, every tuning-visible input hash recomputed, the live production
   constants/policy/weights confirmed, the gold manifest and target catalog validated, and the final
   split's declaration read from *manifest metadata only* -- runs to completion before the holdout is
   opened. Anything missing, stale, non-canonical, tampered with, or drifted refuses.
4. **The receipt is written first.** Only after every precondition passes, a durable receipt is
   created with a true exclusive create (``open(..., "xb")``), fsynced, and only *then* is the loader
   called. If anything afterwards fails, the receipt remains and every rerun is refused: seeing the
   holdout is irrevocable, so a crash mid-run must not license a second look.

Determinism: no clock, no randomness, no git revision, no database, no network, no LLM. Both
artifacts are canonical JSON, exclusive-create and fsynced. The import direction is one-way, so this
module is deliberately **not** re-exported from :mod:`services.evaluation`.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from services.entities.news_linking import (
    ACCEPT_THRESHOLD,
    ADJUDICATE_THRESHOLD,
    ENTITY_LINKING_POLICY_VERSION,
    SIGNAL_WEIGHTS,
)
from services.evaluation.calibration import (
    METRIC_PRECISION,
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    canonical_json_hash,
    sha256_file,
    verify_artifact_hash,
)
from services.evaluation.entity_linking_calibration import (
    LINKER_WEIGHT_SET_ID,
    PolicyMetrics,
    SplitStats,
    build_split_stats,
    compute_policy_metrics,
    score_mentions,
)
from services.evaluation.entity_linking_gold import (
    CASE_TAGS,
    CATALOG_FILE,
    GOLD_ROOT,
    MANIFEST_FILE,
    SPLIT_COUNTS,
    SPLIT_DEVELOPMENT,
    SPLIT_FILES,
    SPLIT_FINAL_HOLDOUT,
    SPLIT_TRAIN,
    GoldMention,
    TargetCatalog,
    load_manifest,
    load_split,
    load_target_catalog,
)
from services.evaluation.stage9_freeze import (
    DEVELOPMENT_DIR,
    FROZEN_MANIFEST_PATH,
    FROZEN_PARAMETERS_PATH,
    PROTOCOL_DOC_PATH,
    PROTOCOL_ID,
    SELECTED_ACCEPT,
    SELECTED_ADJUDICATE,
    load_canonical_json,
    verify_frozen_artifacts,
)

DOMAIN = "entity_linking"
#: The split this report is written against -- the vocabulary of §5, not the loader's split name.
REPORT_SPLIT = "final"
#: Beyond §5's ``status``: this run is the once-only final report, and can never be repeated.
EVALUATION_STATUS = "final_reported_once"
REPORT_SCHEMA = "stage9-entity-linking-final.v1"
RECEIPT_SCHEMA = "stage9-entity-linking-unseal-receipt.v1"
RECEIPT_STATE = "unsealed_once"
#: The development report's filename inside the freeze's development directory.
DEVELOPMENT_REPORT_FILE = "entity-linking.json"
DEVELOPMENT_SPLIT_NAME = "development"
#: Every domain the freeze manifest must account for before the holdout may be opened.
EXPECTED_FREEZE_DOMAINS = frozenset({"alerts", "analogy", "clustering", DOMAIN})
#: Exactly the tuning-visible inputs the development report may name -- the holdout is not one.
TUNING_INPUT_FILES = (
    MANIFEST_FILE,
    CATALOG_FILE,
    SPLIT_FILES[SPLIT_TRAIN],
    SPLIT_FILES[SPLIT_DEVELOPMENT],
)
FINAL_SPLIT_FILE = SPLIT_FILES[SPLIT_FINAL_HOLDOUT]

_REPO_ROOT = Path(__file__).resolve().parents[2]
FINAL_DIR = _REPO_ROOT / "evaluation" / "stage9" / "final"
#: The one canonical output, and the durable receipt that sits beside it. Neither is configurable.
FINAL_REPORT_PATH = FINAL_DIR / "entity-linking.json"
UNSEAL_RECEIPT_PATH = FINAL_DIR / "entity-linking.unseal-receipt.json"

#: The frozen bands this run reports at. Read from the freeze, never chosen here.
FROZEN_ACCEPT = Decimal(SELECTED_ACCEPT)
FROZEN_ADJUDICATE = Decimal(SELECTED_ADJUDICATE)

LIMITATIONS = (
    "labels are synthetic/automated original-synthetic English ORG/PRODUCT positives only, with no "
    "human adjudication and no coreference; this is a measurement against automated labels, not a "
    "human-validated ground truth",
    "the stage-3 LLM adjudication is not exercised: routed-link recall credits an adjudication only "
    "when the expected target is among the deterministic candidates, and does not model the "
    "adjudicator's choice",
    "the bands were frozen before this split was opened: the holdout was scored once at the frozen "
    "0.070 / 0.000, no policy was swept, compared, or selected here, and no production value was "
    "changed by this result",
    "this split is now spent: a further attempt requires a new protocol version and a new, unseen "
    "holdout (protocol section 1), never a rerun of this command",
)


class EntityLinkingFinalError(CalibrationError):
    """A §6 precondition failed, or the one-time artifacts already exist. The holdout stays sealed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EntityLinkingFinalError(message)


def _mapping(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise EntityLinkingFinalError(f"field {key!r} is missing or not an object")
    return value


def _rel(path: Path) -> str:
    """The path relative to the repository root (POSIX), so the artifacts are machine-independent."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _fmt(value: Decimal | float) -> str:
    return format(value, ".3f")


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, METRIC_PRECISION)


# --- canonical, non-configurable paths ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FinalPaths:
    """Every path the command touches. The defaults are the canonical ones; the CLI exposes none.

    Tests override these with temp directories and synthetic fixtures; the CLI cannot, so an operator
    can neither redirect the one-time report nor point the run at a different freeze or gold set.
    """

    protocol: Path = PROTOCOL_DOC_PATH
    freeze_manifest: Path = FROZEN_MANIFEST_PATH
    frozen_parameters: Path = FROZEN_PARAMETERS_PATH
    development_dir: Path = DEVELOPMENT_DIR
    gold_root: Path = GOLD_ROOT
    output: Path = FINAL_REPORT_PATH
    receipt: Path = UNSEAL_RECEIPT_PATH

    @property
    def development_report(self) -> Path:
        return self.development_dir / DEVELOPMENT_REPORT_FILE


CANONICAL_PATHS = FinalPaths()

#: The loader seam. Exactly one call, with ``allow_holdout=True``, happens through this signature.
HoldoutLoader = Callable[..., Sequence[GoldMention]]
Scorer = Callable[[Sequence[GoldMention], TargetCatalog], SplitStats]


# --- preflight ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Preflight:
    """Everything §6 verified, carried forward so nothing is loaded or re-hashed twice."""

    protocol_sha256: str
    freeze_manifest_sha256: str
    frozen_parameters_sha256: str
    development_report_sha256: str
    development_report: dict[str, Any] = field(repr=False)
    catalog: TargetCatalog = field(repr=False)
    tuning_input_hashes: dict[str, str] = field(repr=False)
    dataset: dict[str, str] = field(repr=False)
    final_count: int = 0


def preflight(paths: FinalPaths = CANONICAL_PATHS) -> Preflight:
    """Run every §6 precondition. Returns the verified inputs, or raises. Opens no holdout.

    Reads, in order: the freeze end to end; the manifest's exact hashes for the protocol, the frozen
    parameters and all four development reports; the frozen entity disposition; the entity
    development report; every tuning-visible input hash that report names; the live production
    constants, band policy and signal weights; and the gold manifest plus target catalog. The final
    split is confirmed to exist by its **manifest declaration only** -- its file is never opened,
    stat-ed, or hashed here.
    """
    manifest = verify_frozen_artifacts(
        parameters_path=paths.frozen_parameters,
        manifest_path=paths.freeze_manifest,
        development_dir=paths.development_dir,
        protocol_path=paths.protocol,
    )
    protocol_block = _mapping(manifest, "protocol")
    _require(protocol_block.get("id") == PROTOCOL_ID, f"the freeze does not name {PROTOCOL_ID}")
    protocol_sha = verify_artifact_hash(paths.protocol, protocol_block.get("sha256", ""))
    parameters_sha = verify_artifact_hash(
        paths.frozen_parameters, _mapping(manifest, "frozen_parameters").get("sha256", "")
    )

    reports = _mapping(manifest, "development_reports")
    _require(
        set(reports) == set(EXPECTED_FREEZE_DOMAINS),
        f"the freeze manifest must record exactly {sorted(EXPECTED_FREEZE_DOMAINS)}, "
        f"got {sorted(reports)}",
    )
    report_hashes = {
        domain: verify_artifact_hash(
            paths.development_dir / Path(str(_mapping(reports, domain).get("path"))).name,
            _mapping(reports, domain).get("sha256", ""),
        )
        for domain in sorted(reports)
    }

    _check_frozen_disposition(paths, protocol_sha)
    development = _check_development_report(paths, protocol_sha)
    tuning_hashes = _check_tuning_inputs(paths, development)
    _check_live_production(development)
    catalog, final_count = _check_gold_inputs(paths, development)

    return Preflight(
        protocol_sha256=protocol_sha,
        freeze_manifest_sha256=sha256_file(paths.freeze_manifest),
        frozen_parameters_sha256=parameters_sha,
        development_report_sha256=report_hashes[DOMAIN],
        development_report=development,
        catalog=catalog,
        tuning_input_hashes=tuning_hashes,
        dataset=dict(_mapping(development, "dataset")),
        final_count=final_count,
    )


def _check_frozen_disposition(paths: FinalPaths, protocol_sha: str) -> None:
    """§6.2: the frozen entity parameters are the calibrated, applied 0.070 / 0.000 disposition."""
    parameters, _ = load_canonical_json(paths.frozen_parameters)
    _require(
        _mapping(parameters, "protocol").get("sha256") == protocol_sha,
        "the frozen parameters are pinned to different protocol bytes: they are stale",
    )
    entity = _mapping(_mapping(parameters, "domains"), DOMAIN)
    _require(
        entity.get("status") == CalibrationStatus.CALIBRATED.value,
        f"the frozen entity disposition is {entity.get('status')!r}, not calibrated: "
        "there is nothing to report a held-out number against",
    )
    for block in ("selected", "effective"):
        bands = _mapping(entity, block)
        _require(
            (bands.get("accept"), bands.get("adjudicate")) == (SELECTED_ACCEPT, SELECTED_ADJUDICATE),
            f"the frozen entity {block} bands are not {SELECTED_ACCEPT} / {SELECTED_ADJUDICATE}",
        )
    _require(entity.get("production_applied") is True, "the frozen entity bands were never applied")
    _require(
        entity.get("production_policy_version") == ENTITY_LINKING_POLICY_VERSION,
        f"the frozen band policy {entity.get('production_policy_version')!r} does not equal the live "
        f"{ENTITY_LINKING_POLICY_VERSION!r}",
    )
    _require(
        entity.get("weight_set") == LINKER_WEIGHT_SET_ID
        and entity.get("weight_set_changed") is False,
        f"the frozen entity weight set must be the unchanged {LINKER_WEIGHT_SET_ID}",
    )


def _check_development_report(paths: FinalPaths, protocol_sha: str) -> dict[str, Any]:
    """§6.3: the selection happened first, and its report is still the pre-application artifact."""
    development, _ = load_canonical_json(paths.development_report)
    _require(development.get("domain") == DOMAIN, "the development report is not the entity domain")
    _require(
        development.get("split") == DEVELOPMENT_SPLIT_NAME,
        f"the development report's split is {development.get('split')!r}, not the selection split",
    )
    _require(
        development.get("status") == CalibrationStatus.CALIBRATED.value,
        f"the development report is {development.get('status')!r}, not calibrated",
    )
    protocol_block = _mapping(development, "protocol")
    _require(
        protocol_block.get("id") == PROTOCOL_ID and protocol_block.get("sha256") == protocol_sha,
        "the development report is pinned to different protocol bytes: it is stale",
    )
    selected = _mapping(development, "selected_parameters")
    _require(
        (selected.get("accept"), selected.get("adjudicate")) == (SELECTED_ACCEPT, SELECTED_ADJUDICATE),
        f"the development report did not select {SELECTED_ACCEPT} / {SELECTED_ADJUDICATE}",
    )
    _require(
        selected.get("changed_from_initial") is True,
        "the development report does not record a change from the ADR 0005 initial bands",
    )
    _require(
        selected.get("production_applied") is False,
        "the development report must stay the selection-phase artifact (production_applied false); "
        "the frozen parameters record the application, not the report",
    )
    _require(
        _mapping(development, "model_versions").get("linker_weight_set") == LINKER_WEIGHT_SET_ID,
        f"the development report weight set is not the unchanged {LINKER_WEIGHT_SET_ID}",
    )
    return development


def _check_tuning_inputs(paths: FinalPaths, development: dict[str, Any]) -> dict[str, str]:
    """§6.4: every tuning-visible input the development report named still hashes to the same bytes.

    The name set must be exactly the four tuning-visible files, which is also what stops a tampered
    report from smuggling the holdout in as a "tuning" input and getting it hashed early.
    """
    declared = _mapping(development, "input_hashes")
    _require(
        set(declared) == set(TUNING_INPUT_FILES),
        f"the development report must hash exactly {sorted(TUNING_INPUT_FILES)}, "
        f"got {sorted(declared)}",
    )
    return {
        name: verify_artifact_hash(paths.gold_root / name, str(declared[name]))
        for name in sorted(declared)
    }


def _check_live_production(development: dict[str, Any]) -> None:
    """§6.4: the live linker constants, band policy and signal weights are the pinned model."""
    _require(
        (_fmt(ACCEPT_THRESHOLD), _fmt(ADJUDICATE_THRESHOLD))
        == (SELECTED_ACCEPT, SELECTED_ADJUDICATE),
        f"the live bands {ACCEPT_THRESHOLD} / {ADJUDICATE_THRESHOLD} are not the frozen "
        f"{SELECTED_ACCEPT} / {SELECTED_ADJUDICATE}: production drifted after the freeze",
    )
    _require(
        ENTITY_LINKING_POLICY_VERSION and isinstance(ENTITY_LINKING_POLICY_VERSION, str),
        "the live band policy version is not set",
    )
    live_weights = {signal.value: weight for signal, weight in SIGNAL_WEIGHTS.items()}
    _require(
        _mapping(development, "model_versions").get("signal_weights") == live_weights,
        "the live signal weights differ from the weight set the selection was made under: "
        "the model the holdout would be scored with is not the model that was calibrated",
    )


def _check_gold_inputs(
    paths: FinalPaths, development: dict[str, Any]
) -> tuple[TargetCatalog, int]:
    """§6.4: the gold manifest and catalog validate, and the final split is *declared*. No open."""
    gold_manifest = load_manifest(paths.gold_root)
    catalog = load_target_catalog(paths.gold_root)
    dataset = _mapping(development, "dataset")
    _require(
        (dataset.get("dataset_id"), dataset.get("schema_version"))
        == (gold_manifest.dataset_id, gold_manifest.schema_version),
        f"the development report names dataset {dataset.get('dataset_id')!r}/"
        f"{dataset.get('schema_version')!r}, not the loaded gold set",
    )
    declared = {name: (file, count) for name, file, count in gold_manifest.splits}
    _require(
        SPLIT_FINAL_HOLDOUT in declared,
        f"the gold manifest does not declare a {SPLIT_FINAL_HOLDOUT} split",
    )
    final_file, final_count = declared[SPLIT_FINAL_HOLDOUT]
    _require(
        (final_file, final_count) == (FINAL_SPLIT_FILE, SPLIT_COUNTS[SPLIT_FINAL_HOLDOUT]),
        f"the declared final split is {final_file!r}/{final_count}, not {FINAL_SPLIT_FILE!r}/"
        f"{SPLIT_COUNTS[SPLIT_FINAL_HOLDOUT]}",
    )
    return catalog, final_count


# --- durable, exclusive writers ---------------------------------------------------------------
def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fsync
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - same
        pass
    finally:
        os.close(fd)


def _write_exclusive(path: Path, data: bytes, description: str) -> bytes:
    """Create ``path`` with ``data``, exclusively and durably. Never overwrites; never truncates.

    ``open(..., "xb")`` is the whole point: the create-if-absent is atomic in the kernel, so two
    concurrent runs cannot both believe they are the first, and a pre-existing artifact refuses
    rather than being clobbered. The bytes are fsynced, then the directory entry is fsynced, so a
    crash after this returns still leaves the artifact -- which is what makes a rerun refuse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise EntityLinkingFinalError(
            f"refusing to overwrite the existing {description} at {path}"
        ) from exc
    except OSError as exc:
        raise EntityLinkingFinalError(f"cannot write the {description} to {path}: {exc}") from exc
    _fsync_dir(path.parent)
    return data


# --- the two canonical artifacts ---------------------------------------------------------------
def build_unseal_receipt(pre: Preflight, paths: FinalPaths = CANONICAL_PATHS) -> dict[str, Any]:
    """The durable record that this holdout was unsealed, written *before* the loader is called.

    Carries no clock, no git revision and no result -- only what was verified, what will be reported
    at, and where the one report goes. Its existence alone is enough to refuse every later run.
    """
    return {
        "schema": RECEIPT_SCHEMA,
        "protocol": {"id": PROTOCOL_ID, "sha256": pre.protocol_sha256},
        "domain": DOMAIN,
        "split": SPLIT_FINAL_HOLDOUT,
        "state": RECEIPT_STATE,
        "freeze": {
            "manifest": {
                "path": _rel(paths.freeze_manifest),
                "sha256": pre.freeze_manifest_sha256,
            },
            "frozen_parameters": {
                "path": _rel(paths.frozen_parameters),
                "sha256": pre.frozen_parameters_sha256,
            },
            "development_report": {
                "path": _rel(paths.development_report),
                "sha256": pre.development_report_sha256,
            },
        },
        "selected_parameters": {"accept": SELECTED_ACCEPT, "adjudicate": SELECTED_ADJUDICATE},
        "model_versions": {
            "band_policy_version": ENTITY_LINKING_POLICY_VERSION,
            "linker_weight_set": LINKER_WEIGHT_SET_ID,
            "signal_weights": {signal.value: weight for signal, weight in SIGNAL_WEIGHTS.items()},
        },
        "output_path": _rel(paths.output),
    }


def _by_case_tag(stats: SplitStats, accept: Decimal, adjudicate: Decimal) -> dict[str, Any]:
    """Per-case-tag behaviour of the frozen policy on the final split, in fixed CASE_TAGS order."""
    breakdown: dict[str, Any] = {}
    for tag in CASE_TAGS:
        tagged = tuple(snapshot for snapshot in stats.snapshots if tag in snapshot.case_tags)
        if not tagged:
            continue
        metrics = compute_policy_metrics(build_split_stats(tagged), accept, adjudicate)
        breakdown[tag] = {
            "mentions": metrics.total,
            "links": metrics.links,
            "nils": metrics.nils,
            "accepted_total": metrics.accepted_total,
            "accepted_correct": metrics.accepted_correct,
            "accepted_wrong_target": metrics.accepted_wrong_target,
            "accepted_nil": metrics.accepted_nil,
            "adjudications": metrics.adjudications,
            "nil_decisions": metrics.nil_decisions,
            "candidate_recall": _round(metrics.candidate_recall),
            "top_target_recall": _round(metrics.top_target_recall),
            "nil_recall": _round(metrics.nil_recall),
        }
    return breakdown


def build_final_report(
    pre: Preflight,
    stats: SplitStats,
    metrics: PolicyMetrics,
    *,
    final_input_sha256: str,
    receipt_sha256: str,
    paths: FinalPaths = CANONICAL_PATHS,
) -> dict[str, Any]:
    """The canonical §5 final report: one split, one policy, one set of numbers, and the caveats.

    It repeats no train/development selection metric -- those live in the development report, whose
    path and exact hash are recorded here instead -- and it states ``no_retuning`` outright, because
    the only thing this document licenses is being read.
    """
    return {
        "schema": REPORT_SCHEMA,
        "protocol": {"id": PROTOCOL_ID, "sha256": pre.protocol_sha256},
        "domain": DOMAIN,
        "split": REPORT_SPLIT,
        "status": CalibrationStatus.CALIBRATED.value,
        "evaluation_status": EVALUATION_STATUS,
        "no_retuning": True,
        "freeze": {
            "manifest": {
                "path": _rel(paths.freeze_manifest),
                "sha256": pre.freeze_manifest_sha256,
            },
            "frozen_parameters": {
                "path": _rel(paths.frozen_parameters),
                "sha256": pre.frozen_parameters_sha256,
            },
            "development_report": {
                "path": _rel(paths.development_report),
                "sha256": pre.development_report_sha256,
            },
            "unseal_receipt": {"path": _rel(paths.receipt), "sha256": receipt_sha256},
        },
        "dataset": dict(pre.dataset),
        "model_versions": {
            "band_policy_version": ENTITY_LINKING_POLICY_VERSION,
            "linker_weight_set": LINKER_WEIGHT_SET_ID,
            "policy_version": PROTOCOL_ID,
            "signal_weights": {signal.value: weight for signal, weight in SIGNAL_WEIGHTS.items()},
        },
        "selected_parameters": {
            "accept": SELECTED_ACCEPT,
            "adjudicate": SELECTED_ADJUDICATE,
            "source": "frozen",
            "production_applied": True,
            "changed_by_this_run": False,
        },
        "input_hashes": {**pre.tuning_input_hashes, FINAL_SPLIT_FILE: final_input_sha256},
        "counts": {REPORT_SPLIT: metrics.total},
        "metrics": {REPORT_SPLIT: metrics.as_dict()},
        "diagnostics": {
            "candidate_recall": _round(metrics.candidate_recall),
            "top_target_recall": _round(metrics.top_target_recall),
            "final_by_case_tag": _by_case_tag(stats, FROZEN_ACCEPT, FROZEN_ADJUDICATE),
        },
        "limitations": list(LIMITATIONS),
    }


# --- the one-time command -----------------------------------------------------------------------
def run_final_holdout(
    *,
    acknowledge_final_holdout: bool = False,
    paths: FinalPaths = CANONICAL_PATHS,
    loader: HoldoutLoader = load_split,
    scorer: Scorer = score_mentions,
) -> bytes:
    """Unseal, score once, and write the one-time final report. Returns its exact bytes.

    The ordering below *is* the safety property, and it is not negotiable: acknowledgement, then the
    two existence refusals, then the whole of :func:`preflight`, then the durable receipt, and only
    then the single gated loader call. ``loader`` and ``scorer`` are seams for tests, which inject
    synthetic fixtures; the defaults are the real gated loader and the real linker-backed scorer.
    """
    if not acknowledge_final_holdout:
        raise EntityLinkingFinalError(
            "refusing to unseal the entity-linking final holdout without the explicit operator "
            "acknowledgement (--acknowledge-final-holdout); no file has been read"
        )
    if paths.output.exists():
        raise EntityLinkingFinalError(
            f"the one-time final report already exists at {paths.output}: the holdout has been "
            "reported once and a second report is not a rerun, it is a new protocol version"
        )
    if paths.receipt.exists():
        raise EntityLinkingFinalError(
            f"an unseal receipt already exists at {paths.receipt}: this holdout has been seen and is "
            "irrevocably spent, whether or not the report was written"
        )

    pre = preflight(paths)

    # Receipt first: after this line the holdout counts as seen, whatever happens next.
    receipt = build_unseal_receipt(pre, paths)
    _write_exclusive(paths.receipt, canonical_json_bytes(receipt), "unseal receipt")

    mentions = loader(
        paths.gold_root, SPLIT_FINAL_HOLDOUT, catalog=pre.catalog, allow_holdout=True
    )
    _require(
        len(mentions) == pre.final_count,
        f"the final split holds {len(mentions)} mentions, not the declared {pre.final_count}",
    )
    stats = scorer(mentions, pre.catalog)
    metrics = compute_policy_metrics(stats, FROZEN_ACCEPT, FROZEN_ADJUDICATE)

    # The consumed split is hashed by a second byte read. Hashing the bytes ourselves first and
    # handing the parse a buffer would save the read, but only by going around the gated loader --
    # and one gated call with the full validation is worth more than one avoided read. Loading and
    # scoring still happen exactly once.
    report = build_final_report(
        pre,
        stats,
        metrics,
        final_input_sha256=sha256_file(paths.gold_root / FINAL_SPLIT_FILE),
        receipt_sha256=canonical_json_hash(receipt),
        paths=paths,
    )
    return _write_exclusive(paths.output, canonical_json_bytes(report), "final report")


def build_parser() -> argparse.ArgumentParser:
    """The CLI: one acknowledgement flag and nothing else -- no output path, no root, no bands."""
    parser = argparse.ArgumentParser(
        description=(
            "Stage 9 entity-linking FINAL HOLDOUT report. Runs exactly once, at the frozen "
            "0.070 / 0.000 bands, and writes a durable unseal receipt before opening the split."
        )
    )
    parser.add_argument(
        "--acknowledge-final-holdout",
        action="store_true",
        help=(
            "Acknowledge that this irrevocably spends the sealed final holdout: it may never be "
            "scored again, and a disappointing result does not license retuning."
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point (no clock, no randomness, no git revision, no configurable paths)."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    data = run_final_holdout(acknowledge_final_holdout=args.acknowledge_final_holdout)
    print(f"wrote {len(data)} bytes to {FINAL_REPORT_PATH}")
    print(f"unseal receipt at {UNSEAL_RECEIPT_PATH}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DOMAIN",
    "EVALUATION_STATUS",
    "FINAL_DIR",
    "FINAL_REPORT_PATH",
    "FROZEN_ACCEPT",
    "FROZEN_ADJUDICATE",
    "RECEIPT_SCHEMA",
    "RECEIPT_STATE",
    "REPORT_SCHEMA",
    "REPORT_SPLIT",
    "UNSEAL_RECEIPT_PATH",
    "CANONICAL_PATHS",
    "EntityLinkingFinalError",
    "FinalPaths",
    "Preflight",
    "build_final_report",
    "build_parser",
    "build_unseal_receipt",
    "main",
    "preflight",
    "run_final_holdout",
]
