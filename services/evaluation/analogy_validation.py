"""Analogy retrieval-floor evaluator (evaluation-only) for ``stage9-validation.v1`` §4.3.

``docs/evaluation/stage9-validation-protocol.md`` §4.3 fixes analogy as **evaluation-only**: the
0.60 minimum-similarity floor is frozen, all 40 gold pairs are unsplit and carry no negative /
"no reliable analogy" labels, and there is therefore **no basis on which to select a floor**. This
module builds the canonical §5 report that records exactly that -- and nothing more.

It is a pure validate-and-report step. It loads the tuning-visible corpus and gold via
:func:`services.analogies.corpus.load_corpus_and_gold` (no database, no network, no embedding call,
no clock, no randomness), hashes by their exact bytes the files that load actually consumed (the
numbered episode shards plus ``analogy_gold.json``) and the protocol document, and derives
deterministic coverage/provenance diagnostics from the validated data. It does **not** call the real
retrieval path, does **not** construct similarity scores, and never opens a sealed final holdout.

Because no pinned, outcome-blind embedding/retrieval artifact and no database-backed retrieval run
exist offline, the §4.3 metrics (hit@1/3/5, recall@5, MRR, abstention rate, and the 0.60 threshold
diagnostics) are reported **unavailable/null** rather than fabricated. The report selects no
parameter, changes no production value (the floor stays 0.60, ``production_applied`` is ``false``),
and makes no reranker-quality claim.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from services.analogies.contracts import DEFAULT_MIN_SIMILARITY, DEFAULT_TOP_K
from services.analogies.corpus import (
    CORPUS_DIR,
    CORPUS_GLOB,
    GOLD_PATH,
    SCHEMA_VERSION,
    load_corpus_and_gold,
    quota_report,
)
from services.evaluation.calibration import (
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    sha256_file,
)

#: Protocol this evaluator obeys.
PROTOCOL_ID = "stage9-validation.v1"
DOMAIN = "analogy"
#: Analogy is unsplit and evaluation-only: no train / development / holdout split exists (§2, §4.3).
REPORT_SPLIT = "unsplit"

#: The embedding space analogy retrieval is measured in (ADR 0004); the version is still unpinned.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_MODEL_VERSION = "current"

#: The frozen analogy retrieval floor, reported unchanged; §4.3 selects no floor. An exact
#: fixed-precision decimal string, like every other reported threshold -- the live float stays
#: ``services.analogies.contracts.DEFAULT_MIN_SIMILARITY`` and is asserted equal below.
ANALOGY_FLOOR = "0.60"

_REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DOC_PATH = _REPO_ROOT / "docs" / "evaluation" / "stage9-validation-protocol.md"
DEFAULT_OUTPUT_PATH = _REPO_ROOT / "evaluation" / "stage9" / "development" / "analogy.json"

#: Why every §4.3 metric is null: honesty, not omission.
_UNAVAILABLE_METRICS_REASON = (
    "no pinned, outcome-blind embedding/retrieval artifact exists and no database-backed retrieval "
    "run is available offline, so hit@k, recall@5, MRR, the abstention rate, and the 0.60 threshold "
    "diagnostics cannot be computed here without fabricating similarity scores"
)


class AnalogyValidationError(CalibrationError):
    """An analogy validation input is invalid. Raised before any report is emitted."""


def analogy_input_paths(
    directory: Path = CORPUS_DIR, gold_path: Path = GOLD_PATH
) -> tuple[Path, ...]:
    """The exact files ``load_corpus_and_gold`` consumes: the sorted numbered shards, then gold.

    Replicates :func:`services.analogies.corpus.load_corpus`'s glob and sort so the hashed inventory
    is precisely what was validated -- the unnumbered authoring template is never a corpus input.
    """
    shards = sorted(Path(directory).glob(CORPUS_GLOB))
    if not shards:
        raise AnalogyValidationError(
            f"no corpus shard files matching {CORPUS_GLOB} in {directory}"
        )
    return (*shards, Path(gold_path))


def _assert_floor_unchanged() -> None:
    """Guard the one production constant this report is predicated on: the 0.60 floor is frozen.

    Also ties the reported decimal string to the live float, so the string can never drift away from
    the constant it claims to report.
    """
    if not DEFAULT_MIN_SIMILARITY == float(ANALOGY_FLOOR) == 0.60:
        raise AnalogyValidationError(
            f"the analogy floor DEFAULT_MIN_SIMILARITY is {DEFAULT_MIN_SIMILARITY!r} and the "
            f"reported floor is {ANALOGY_FLOOR!r}, not 0.60; stage9-validation.v1 freezes it and "
            "this evaluation-only report asserts it unchanged"
        )


def _unavailable_metrics() -> dict[str, Any]:
    """The §4.3 metric schema, every value ``null``: measured only when a real run can be pinned."""
    return {
        "available": False,
        "reason": _UNAVAILABLE_METRICS_REASON,
        "hit_at_1": None,
        "hit_at_3": None,
        "hit_at_5": None,
        "recall_at_5": None,
        "mrr": None,
        "abstention_rate": None,
        "threshold_diagnostics": {
            "min_similarity": ANALOGY_FLOOR,
            "correct_above_threshold": None,
            "correct_below_threshold": None,
            "correct_absent": None,
        },
    }


def build_analogy_validation_report(
    *,
    directory: Path = CORPUS_DIR,
    gold_path: Path = GOLD_PATH,
    protocol_path: Path = PROTOCOL_DOC_PATH,
) -> dict[str, Any]:
    """Build the canonical §5 analogy report: validate the data, hash the inputs, report honestly.

    Loads and validates the corpus and gold offline (raising :class:`CorpusValidationError` on bad
    data), hashes the exact bytes of every consumed input and the protocol document, and records
    deterministic coverage/provenance derived from the validated files. No metric is computed and no
    parameter is selected: the 0.60 floor is retained unchanged and ``production_applied`` is
    ``false``.
    """
    _assert_floor_unchanged()
    corpus, gold = load_corpus_and_gold(directory, gold_path)
    paths = analogy_input_paths(directory, gold_path)
    coverage = quota_report(corpus, gold)
    return {
        "protocol": {"id": PROTOCOL_ID, "sha256": sha256_file(protocol_path)},
        "domain": DOMAIN,
        "status": CalibrationStatus.EVALUATION_ONLY.value,
        "split": REPORT_SPLIT,
        "input_hashes": {path.name: sha256_file(path) for path in paths},
        "model_versions": {
            "embedding_model": EMBEDDING_MODEL,
            "embedding_model_version": EMBEDDING_MODEL_VERSION,
            "corpus_schema_version": SCHEMA_VERSION,
            "gold_schema_version": SCHEMA_VERSION,
            "retrieval_top_k": DEFAULT_TOP_K,
        },
        "evaluated_grid": {
            "swept": False,
            "grid": "n/a",
            "description": (
                "analogy runs no threshold sweep (§4.3): the gold pairs are unsplit and carry no "
                "basis to select a floor, so no grid is enumerated"
            ),
        },
        "selected_parameters": {
            "selection": "none",
            "floor": ANALOGY_FLOOR,
            "floor_changed": False,
            "production_applied": False,
            "reason": (
                "the analogy gold is unsplit and evaluation-only with no negative labels; there is "
                "no basis to select a floor, so the 0.60 default is retained unchanged"
            ),
        },
        "coverage": coverage,
        "provenance": {
            "input_file_count": len(paths),
            "corpus_shard_count": len(paths) - 1,
            "gold_pairs": coverage["gold"]["pairs"],
            "human_reviewed_episodes": corpus.quotas.human_reviewed,
            "split": REPORT_SPLIT,
        },
        "metrics": _unavailable_metrics(),
        "blockers": [
            _UNAVAILABLE_METRICS_REASON,
            "the embedding model version is the unpinned string 'current'; retrieval similarities "
            "are only comparable once it is pinned to a concrete version",
        ],
        "limitations": [
            f"all {coverage['gold']['pairs']} gold pairs are unsplit and evaluation-only -- there "
            "is no train/development/holdout split, so nothing can be selected",
            "the gold set carries no negative / 'no reliable analogy' labels, so abstention "
            "correctness cannot be scored",
            "zero human sign-offs: the corpus is llm_drafted_pending_human_review "
            f"(human_reviewed = {corpus.quotas.human_reviewed}), so any measured number would be "
            "against automated labels, not human-validated ground truth",
            "there is no basis on which to select the retrieval floor; the 0.60 default is retained "
            "unchanged and no reranker-quality claim is made",
        ],
    }


def write_analogy_validation_report(
    output_path: Path | str,
    *,
    directory: Path = CORPUS_DIR,
    gold_path: Path = GOLD_PATH,
    protocol_path: Path = PROTOCOL_DOC_PATH,
) -> bytes:
    """Serialize the report to ``output_path`` as canonical bytes, refusing to overwrite.

    Builds the report first (so invalid inputs raise before any file is touched), creates parent
    directories, writes atomically (temp file then ``os.replace``), and refuses if the path already
    exists -- a checked-in report is never silently overwritten. Returns the exact bytes.
    """
    path = Path(output_path)
    if path.exists():
        raise AnalogyValidationError(f"refusing to overwrite existing report at {path}")
    data = canonical_json_bytes(
        build_analogy_validation_report(
            directory=directory, gold_path=gold_path, protocol_path=protocol_path
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def main(argv: list[str] | None = None) -> int:
    """CLI: write the canonical evaluation-only report (no clock, no randomness, no git revision)."""
    parser = argparse.ArgumentParser(
        description="Stage 9 analogy evaluation-only validation report."
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args(argv)
    data = write_analogy_validation_report(args.output)
    print(f"wrote {len(data)} bytes to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ANALOGY_FLOOR",
    "DOMAIN",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_VERSION",
    "PROTOCOL_ID",
    "REPORT_SPLIT",
    "AnalogyValidationError",
    "analogy_input_paths",
    "build_analogy_validation_report",
    "write_analogy_validation_report",
]
