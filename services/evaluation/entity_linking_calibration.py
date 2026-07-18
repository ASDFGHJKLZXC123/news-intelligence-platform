"""Entity-linking accept/adjudicate band evaluator for ``stage9-validation.v1``.

``docs/evaluation/stage9-validation-protocol.md`` §4.2 fixes the *written* entity-linking
procedure: with ADR 0005's stage-2 signal weights held fixed, sweep the two band thresholds over
``inclusive_decimal_grid("0.000", "1.000", "0.005")`` (constrained to ``0 <= adjudicate < accept
<= 1`` -- 20,100 ordered policies), enumerate on **train** under the eligibility gate, then select
**exactly one** policy on **development** with the predeclared lexicographic objective. This module
implements exactly that, built on the shared primitives in :mod:`services.evaluation.calibration`
and the tuning slice loaded by :mod:`services.evaluation.entity_linking_gold`.

It scores the real production linker (:class:`services.entities.news_linking.NewsEntityLinker`)
**once per mention** against a private, in-memory identity store built from the gold catalog's
``profile_row()`` / ``alias_rows()`` -- no database, no network, no spaCy, no LLM, and nothing is
persisted (``persist_run=False``). Each mention becomes an immutable *scored snapshot* (its ordered
candidate ids, top score, exact-top tie, top alias type, supporting-signal flag). Every candidate
policy then **re-bands the same snapshots** in memory -- it never rescores -- preserving the
production inclusive band boundaries, the accept-threshold **tie gate**, and the **brand/product
gate**. A no-candidate mention stays NIL for every policy.

It applies **no production value**: it reads only the train + development tuning slice, never opens
the sealed evaluation split, and every report records ``production_applied = false``. Applying the
selected bands is a later, separately-verified step -- the §6 freeze in
:mod:`services.evaluation.stage9_freeze`, which is what records the application. Determinism: gold ids arrive
sorted, the grid is an exact ``Decimal`` sweep, selection is a total order (accept then adjudicate
break every remaining tie), and the report is canonical JSON with no clock, randomness, or git
revision.
"""

from __future__ import annotations

import argparse
import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

from db.models import EntityAlias, EntityProfile
from services.entities.news_linking import (
    SIGNAL_WEIGHTS,
    LinkBand,
    MentionLinkResult,
    NewsEntityLinker,
)
from services.evaluation.calibration import (
    METRIC_PRECISION,
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    inclusive_decimal_grid,
    sha256_file,
)
from services.evaluation.entity_linking_gold import (
    CASE_TAGS,
    CATALOG_FILE,
    DATASET_ID,
    GOLD_ROOT,
    MANIFEST_FILE,
    SCHEMA_VERSION,
    SPLIT_DEVELOPMENT,
    SPLIT_FILES,
    SPLIT_TRAIN,
    EntityLinkingGoldCorpus,
    GoldMention,
    TargetCatalog,
    fixture_target_uuid,
    load_tuning_corpus,
)

#: Protocol this evaluator obeys, and the version stamped on the report's policy metadata.
PROTOCOL_ID = "stage9-validation.v1"
DOMAIN = "entity_linking"
#: The split the report is written against; train is the eligibility filter, not the reported split.
REPORT_SPLIT = "development"
#: ADR 0005's fixed initial weight set -- swept over band thresholds only, never re-weighted here.
LINKER_WEIGHT_SET_ID = "adr0005-stage2-initial.v1"

#: §4.2 band-threshold grid (decimal strings -- never floats -- for a reproducible sweep).
GRID_START = "0.000"
GRID_STOP = "1.000"
GRID_STEP = "0.005"
#: The alias type the brand/product gate holds back from an unsupported auto-accept.
_BRAND_PRODUCT_ALIAS_TYPE = "brand_product"

#: The ADR 0005 initial bands -- production at selection time, and the point every policy's tie-break
#: distance is measured from. They stay 0.850/0.500 here even though the freeze later applied the
#: selected 0.070/0.000 to production: this module's baseline is the pre-selection state, not the
#: live constants.
INITIAL_ACCEPT = Decimal("0.850")
INITIAL_ADJUDICATE = Decimal("0.500")

#: Train eligibility gates (§4.2): at least one auto-accept, precision >= 0.95, NIL recall >= 0.80.
MIN_AUTO_ACCEPTS = 1
MIN_AUTO_ACCEPT_PRECISION = 0.95
MIN_NIL_RECALL = 0.80

_REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DOC_PATH = _REPO_ROOT / "docs" / "evaluation" / "stage9-validation-protocol.md"
DEFAULT_OUTPUT_PATH = _REPO_ROOT / "evaluation" / "stage9" / "development" / "entity-linking.json"

_NEGATIVE_INFINITY = float("-inf")


class EntityLinkingCalibrationError(CalibrationError):
    """An entity-linking calibration input is invalid. Raised before any policy is scored."""


# --- deterministic candidate grids ---------------------------------------------------------
def entity_linking_grid_points() -> tuple[Decimal, ...]:
    """The §4.2 threshold axis: ``0.000..1.000`` step ``0.005`` -- 201 inclusive Decimal points."""
    return inclusive_decimal_grid(GRID_START, GRID_STOP, GRID_STEP)


def entity_linking_policy_grid() -> tuple[tuple[Decimal, Decimal], ...]:
    """Every ``(accept, adjudicate)`` policy with ``0 <= adjudicate < accept <= 1`` -- 20,100 pairs.

    Ordered accept-ascending, then adjudicate-ascending: a stable enumeration, though selection is a
    total order and does not depend on it.
    """
    points = entity_linking_grid_points()
    return tuple(
        (accept, adjudicate) for index, accept in enumerate(points) for adjudicate in points[:index]
    )


def _grid_decimal(value: Decimal | int | str, name: str) -> Decimal:
    """Coerce a threshold to a finite ``Decimal`` in ``[0, 1]`` on the 0.005 grid. Floats refused."""
    if isinstance(value, bool):
        raise EntityLinkingCalibrationError(f"{name} must not be a bool, got {value!r}")
    if isinstance(value, float):
        raise EntityLinkingCalibrationError(
            f"{name} must be a Decimal, int or decimal string, not a float ({value!r})"
        )
    if not isinstance(value, Decimal | int | str):
        raise EntityLinkingCalibrationError(
            f"{name} must be a Decimal, int or str, got {type(value).__name__}"
        )
    try:
        result = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise EntityLinkingCalibrationError(f"{name} is not a valid decimal: {value!r}") from exc
    if not result.is_finite():
        raise EntityLinkingCalibrationError(f"{name} must be finite, got {value!r}")
    if not Decimal(0) <= result <= Decimal(1):
        raise EntityLinkingCalibrationError(f"{name} must lie in [0, 1], got {value!r}")
    _, remainder = divmod(result, Decimal(GRID_STEP))
    if remainder != 0:
        raise EntityLinkingCalibrationError(
            f"{name} {value!r} is not on the {GRID_STEP} grid"
        )
    return result


def validate_policy(
    accept: Decimal | int | str, adjudicate: Decimal | int | str
) -> tuple[Decimal, Decimal]:
    """Validate a candidate policy: finite grid Decimals with ``0 <= adjudicate < accept <= 1``."""
    resolved_accept = _grid_decimal(accept, "accept")
    resolved_adjudicate = _grid_decimal(adjudicate, "adjudicate")
    if not resolved_adjudicate < resolved_accept:
        raise EntityLinkingCalibrationError(
            f"adjudicate {resolved_adjudicate} must be strictly below accept {resolved_accept}"
        )
    return resolved_accept, resolved_adjudicate


# --- immutable scored snapshot -------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScoredMention:
    """One mention scored once by the real linker, captured independently of any band.

    Everything a candidate policy needs to re-band the mention without rescoring it: the ordered
    candidate entity ids, the top candidate's 4-decimal score (as an exact ``Decimal``), whether the
    top score is exactly tied by the runner-up, the top candidate's alias type and whether any signal
    supported it, and the gold answer (``expected_target`` UUID, or ``None`` for a NIL mention).
    """

    mention_id: str
    split: str
    case_tags: tuple[str, ...]
    is_link: bool
    expected_target: uuid.UUID | None
    candidate_ids: tuple[uuid.UUID, ...]
    top_id: uuid.UUID | None
    top_score: Decimal | None
    top_tie: bool
    top_alias_type: str | None
    top_supported: bool

    def __post_init__(self) -> None:
        if not isinstance(self.mention_id, str) or not self.mention_id.strip():
            raise EntityLinkingCalibrationError(
                f"mention_id must be a non-empty string, got {self.mention_id!r}"
            )
        if self.top_score is not None:
            if not isinstance(self.top_score, Decimal) or not self.top_score.is_finite():
                raise EntityLinkingCalibrationError(
                    f"{self.mention_id}: top_score must be a finite Decimal, got {self.top_score!r}"
                )
            if not Decimal(0) <= self.top_score <= Decimal(1):
                raise EntityLinkingCalibrationError(
                    f"{self.mention_id}: top_score must lie in [0, 1], got {self.top_score}"
                )
        if not all(isinstance(entity_id, uuid.UUID) for entity_id in self.candidate_ids):
            raise EntityLinkingCalibrationError(f"{self.mention_id}: candidate ids must be UUIDs")
        has_candidate = bool(self.candidate_ids)
        if has_candidate != (self.top_id is not None) or has_candidate != (self.top_score is not None):
            raise EntityLinkingCalibrationError(
                f"{self.mention_id}: a candidate list, a top id, and a top score must agree"
            )
        if self.is_link and not isinstance(self.expected_target, uuid.UUID):
            raise EntityLinkingCalibrationError(
                f"{self.mention_id}: a link mention needs an expected_target UUID"
            )
        if not self.is_link and self.expected_target is not None:
            raise EntityLinkingCalibrationError(
                f"{self.mention_id}: a NIL mention must have no expected_target"
            )


def _snapshot(mention: GoldMention, result: MentionLinkResult, cap: int) -> ScoredMention:
    """Freeze one linked mention into a band-independent snapshot."""
    candidates = result.candidates
    if len(candidates) >= cap:
        raise EntityLinkingCalibrationError(
            f"{mention.mention_id}: candidate list hit the cap {cap}; raise it to keep the catalog"
        )
    if candidates:
        top = candidates[0]
        top_score: Decimal | None = Decimal(str(top.score))
        top_id: uuid.UUID | None = top.entity_id
        top_alias_type: str | None = top.evidence.alias_type
        top_supported = top.has_supporting_signal
        top_tie = len(candidates) > 1 and candidates[1].score == top.score
        candidate_ids = tuple(candidate.entity_id for candidate in candidates)
    else:
        top_score = top_id = top_alias_type = None
        top_supported = top_tie = False
        candidate_ids = ()
    expected = fixture_target_uuid(mention.expected_target_id) if mention.is_link else None
    return ScoredMention(
        mention_id=mention.mention_id,
        split=mention.split,
        case_tags=mention.case_tags,
        is_link=mention.is_link,
        expected_target=expected,
        candidate_ids=candidate_ids,
        top_id=top_id,
        top_score=top_score,
        top_tie=top_tie,
        top_alias_type=top_alias_type,
        top_supported=top_supported,
    )


def reband_snapshot(
    snapshot: ScoredMention, accept: Decimal, adjudicate: Decimal
) -> tuple[LinkBand, uuid.UUID | None]:
    """Re-band one snapshot under a policy, preserving the production tie and brand/product gates.

    Inclusive boundaries (``score >= accept`` accepts, ``score >= adjudicate`` adjudicates). When the
    top clears accept but the top score is exactly tied, or the top is an unsupported brand/product
    alias, the mention routes to adjudication instead of attaching -- exactly as
    ``services.entities.news_linking.decide_band`` does at the live thresholds. A no-candidate
    mention is NIL for every policy.
    """
    score = snapshot.top_score
    if score is None:
        return LinkBand.NIL, None
    if score >= accept:
        gated = snapshot.top_tie or (
            snapshot.top_alias_type == _BRAND_PRODUCT_ALIAS_TYPE and not snapshot.top_supported
        )
        return (LinkBand.ADJUDICATE, None) if gated else (LinkBand.ACCEPT, snapshot.top_id)
    if score >= adjudicate:
        return LinkBand.ADJUDICATE, None
    return LinkBand.NIL, None


# --- per-split precomputation and per-policy metrics ---------------------------------------
@dataclass(frozen=True, slots=True)
class SplitStats:
    """One split's snapshots plus the policy-independent recall denominators/hits, precomputed once."""

    snapshots: tuple[ScoredMention, ...]
    links: int
    nils: int
    candidate_hits: int  # link mentions whose candidate list contains the expected target
    top_hits: int  # link mentions whose top candidate is the expected target


def build_split_stats(snapshots: Sequence[ScoredMention]) -> SplitStats:
    """Validate a split's snapshots (unique ids) and precompute its candidate/top-target hits."""
    frozen = tuple(snapshots)
    seen: set[str] = set()
    for snapshot in frozen:
        if not isinstance(snapshot, ScoredMention):
            raise EntityLinkingCalibrationError("every snapshot must be a ScoredMention")
        if snapshot.mention_id in seen:
            raise EntityLinkingCalibrationError(f"duplicate mention id {snapshot.mention_id!r}")
        seen.add(snapshot.mention_id)
    links = sum(1 for snapshot in frozen if snapshot.is_link)
    return SplitStats(
        snapshots=frozen,
        links=links,
        nils=len(frozen) - links,
        candidate_hits=sum(
            1 for s in frozen if s.is_link and s.expected_target in s.candidate_ids
        ),
        top_hits=sum(1 for s in frozen if s.is_link and s.top_id == s.expected_target),
    )


@dataclass(frozen=True, slots=True)
class PolicyMetrics:
    """One policy's confusion counts over a split, and the §4.2 rates derived from them.

    Counts are exact; every rate is ``None`` where its denominator is zero -- notably
    :attr:`auto_accept_precision` is ``None`` (infeasible), never ``1.0``, when nothing was
    auto-accepted. Rates are rounded only when serialized (:meth:`as_dict`); selection reads them raw.
    """

    total: int
    links: int
    nils: int
    candidate_hits: int
    top_hits: int
    accepted_total: int
    accepted_correct: int
    accepted_wrong_target: int
    accepted_nil: int
    adjudications: int
    nil_decisions: int
    nil_recalled: int
    routed_hits: int

    @property
    def auto_accept_precision(self) -> float | None:
        return None if self.accepted_total == 0 else self.accepted_correct / self.accepted_total

    @property
    def correct_auto_link_recall(self) -> float | None:
        return None if self.links == 0 else self.accepted_correct / self.links

    @property
    def routed_link_recall(self) -> float | None:
        return None if self.links == 0 else self.routed_hits / self.links

    @property
    def nil_recall(self) -> float | None:
        return None if self.nils == 0 else self.nil_recalled / self.nils

    @property
    def candidate_recall(self) -> float | None:
        return None if self.links == 0 else self.candidate_hits / self.links

    @property
    def top_target_recall(self) -> float | None:
        return None if self.links == 0 else self.top_hits / self.links

    @property
    def adjudication_rate(self) -> float:
        return 0.0 if self.total == 0 else self.adjudications / self.total

    @property
    def is_train_eligible(self) -> bool:
        """§4.2 gate: at least one auto-accept, precision >= 0.95, and NIL recall >= 0.80."""
        precision = self.auto_accept_precision
        nil_recall = self.nil_recall
        return (
            self.accepted_total >= MIN_AUTO_ACCEPTS
            and precision is not None
            and precision >= MIN_AUTO_ACCEPT_PRECISION
            and nil_recall is not None
            and nil_recall >= MIN_NIL_RECALL
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "links": self.links,
            "nils": self.nils,
            "accepted_total": self.accepted_total,
            "accepted_correct": self.accepted_correct,
            "accepted_wrong_target": self.accepted_wrong_target,
            "accepted_nil": self.accepted_nil,
            "adjudications": self.adjudications,
            "nil_decisions": self.nil_decisions,
            "auto_accept_precision": _round(self.auto_accept_precision),
            "correct_auto_link_recall": _round(self.correct_auto_link_recall),
            "routed_link_recall": _round(self.routed_link_recall),
            "nil_recall": _round(self.nil_recall),
            "candidate_recall": _round(self.candidate_recall),
            "top_target_recall": _round(self.top_target_recall),
            "adjudication_rate": _round(self.adjudication_rate),
        }


def compute_policy_metrics(
    stats: SplitStats, accept: Decimal, adjudicate: Decimal
) -> PolicyMetrics:
    """Re-band every snapshot in ``stats`` under one policy and tally the §4.2 metrics."""
    accepted_total = accepted_correct = accepted_wrong = accepted_nil = 0
    adjudications = nil_decisions = nil_recalled = routed_hits = 0
    for snapshot in stats.snapshots:
        band, chosen = reband_snapshot(snapshot, accept, adjudicate)
        if band is LinkBand.ACCEPT:
            accepted_total += 1
            if snapshot.is_link and chosen == snapshot.expected_target:
                accepted_correct += 1
                routed_hits += 1
            elif snapshot.is_link:
                accepted_wrong += 1
            else:
                accepted_nil += 1
        elif band is LinkBand.ADJUDICATE:
            adjudications += 1
            if snapshot.is_link and snapshot.expected_target in snapshot.candidate_ids:
                routed_hits += 1
        else:
            nil_decisions += 1
            if not snapshot.is_link:
                nil_recalled += 1
    return PolicyMetrics(
        total=len(stats.snapshots),
        links=stats.links,
        nils=stats.nils,
        candidate_hits=stats.candidate_hits,
        top_hits=stats.top_hits,
        accepted_total=accepted_total,
        accepted_correct=accepted_correct,
        accepted_wrong_target=accepted_wrong,
        accepted_nil=accepted_nil,
        adjudications=adjudications,
        nil_decisions=nil_decisions,
        nil_recalled=nil_recalled,
        routed_hits=routed_hits,
    )


# --- the in-memory identity store and one-pass scoring -------------------------------------
class _IdentitySource:
    """A read-only, dict-backed identity store the real linker reads through its ``all_of`` seam.

    Holds only detached ``EntityProfile`` / ``EntityAlias`` rows built from the gold catalog; it has
    no session, opens no database, and is never persisted. ``all_of`` is the one hook
    ``NewsLinkingRepository`` uses when it is present, so every read stays in memory.
    """

    __slots__ = ("_rows",)

    def __init__(
        self, profiles: Sequence[EntityProfile], aliases: Sequence[EntityAlias]
    ) -> None:
        self._rows: dict[type[Any], tuple[Any, ...]] = {
            EntityProfile: tuple(profiles),
            EntityAlias: tuple(aliases),
        }

    def all_of(self, model: type[Any]) -> tuple[Any, ...]:
        return self._rows.get(model, ())


def build_identity_source(catalog: TargetCatalog) -> _IdentitySource:
    """Build the in-memory identity store from the gold catalog's ORM-shaped fixture rows."""
    profiles = [EntityProfile(**target.profile_row()) for target in catalog.targets]
    aliases = [
        EntityAlias(**row) for target in catalog.targets for row in target.alias_rows()
    ]
    return _IdentitySource(profiles, aliases)


def score_mentions(
    mentions: Sequence[GoldMention], catalog: TargetCatalog
) -> SplitStats:
    """Score a sequence of gold mentions once with the real linker, in the order given.

    The linker runs against the in-memory identity store with ``persist_run=False`` and a candidate
    cap above the catalog size, so the full ordered candidate list is retained without changing any
    evidence. Each mention becomes one immutable snapshot; :func:`build_split_stats` then rejects a
    duplicate id and precomputes the policy-independent candidate/top-target hits. Pure and
    deterministic: no database, network, spaCy, or LLM, and nothing is persisted.

    This is the single scoring seam. :func:`score_corpus` (train + development) and the §6 final
    command both go through it, so a mention is scored exactly the same way whichever split it is in.
    """
    linker = NewsEntityLinker(build_identity_source(catalog))
    cap = len(catalog.targets) + 1
    snapshots: list[ScoredMention] = []
    for mention in mentions:
        stage2_mention, context = mention.to_stage2_inputs(catalog)
        result = linker.link(
            stage2_mention, replace(context, max_candidates=cap), persist_run=False
        )
        snapshots.append(_snapshot(mention, result, cap))
    return build_split_stats(snapshots)


def score_corpus(corpus: EntityLinkingGoldCorpus) -> dict[str, SplitStats]:
    """Score every tuning mention once with the real linker, returning per-split precomputed stats.

    Buckets the tuning slice by split -- rejecting an unexpected split and any id repeated across
    the slice -- and delegates the scoring itself to :func:`score_mentions`.
    """
    catalog = corpus.catalog
    buckets: dict[str, list[GoldMention]] = {SPLIT_TRAIN: [], SPLIT_DEVELOPMENT: []}
    seen: set[str] = set()
    for mention in corpus.mentions:
        if mention.split not in buckets:
            raise EntityLinkingCalibrationError(f"unexpected split {mention.split!r}")
        if mention.mention_id in seen:
            raise EntityLinkingCalibrationError(f"duplicate mention id {mention.mention_id!r}")
        seen.add(mention.mention_id)
        buckets[mention.split].append(mention)
    return {split: score_mentions(mentions, catalog) for split, mentions in buckets.items()}


# --- sweep and selection -------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EntityLinkingCalibration:
    """The frozen outcome of one train-enumerate / development-select run."""

    status: CalibrationStatus
    selected_accept: Decimal
    selected_adjudicate: Decimal
    train_eligible_count: int
    eligible_with_defined_precision: int
    initial_train: PolicyMetrics
    initial_development: PolicyMetrics
    selected_train: PolicyMetrics
    selected_development: PolicyMetrics
    train_stats: SplitStats
    development_stats: SplitStats


def _distance_from_initial(accept: Decimal, adjudicate: Decimal) -> Decimal:
    """Total absolute distance from the initial 0.85 / 0.50 point (tie-break 6)."""
    return abs(accept - INITIAL_ACCEPT) + abs(adjudicate - INITIAL_ADJUDICATE)


def _selection_sort_key(
    entry: tuple[Decimal, Decimal, PolicyMetrics],
) -> tuple[float, float, float, float, float, Decimal, Decimal, Decimal]:
    """The §4.2 development objective as a max-key. A ``None`` rate sorts to ``-inf``, never 1.0.

    Maximize (1) auto-accept precision, (2) correct auto-link recall, (3) routed-link recall,
    (4) NIL recall; then minimize (5) adjudication rate and (6) distance from 0.85/0.50 (both
    negated); then prefer (7) higher accept and (8) higher adjudicate. ``(accept, adjudicate)`` is
    unique per policy, so the argmax is unique.
    """
    accept, adjudicate, metrics = entry
    return (
        _or_neg_inf(metrics.auto_accept_precision),
        _or_neg_inf(metrics.correct_auto_link_recall),
        _or_neg_inf(metrics.routed_link_recall),
        _or_neg_inf(metrics.nil_recall),
        -metrics.adjudication_rate,
        -_distance_from_initial(accept, adjudicate),
        accept,
        adjudicate,
    )


def select_policy(train_stats: SplitStats, development_stats: SplitStats) -> EntityLinkingCalibration:
    """Enumerate on train under the eligibility gate, then select exactly one policy on development.

    Only train-eligible policies are scored on development. If none is eligible, or none of the
    eligible has a defined development accept precision (nothing auto-accepted on development), the
    status is ``retained_insufficient_evidence`` and the initial 0.85/0.50 is kept; otherwise the
    lexicographic winner is selected and the status is ``calibrated``.
    """
    eligible: list[tuple[Decimal, Decimal, PolicyMetrics]] = []
    for accept, adjudicate in entity_linking_policy_grid():
        if compute_policy_metrics(train_stats, accept, adjudicate).is_train_eligible:
            development_metrics = compute_policy_metrics(development_stats, accept, adjudicate)
            eligible.append((accept, adjudicate, development_metrics))

    defined = sum(1 for _, _, metrics in eligible if metrics.auto_accept_precision is not None)
    winner = max(eligible, key=_selection_sort_key) if eligible else None
    if winner is not None and winner[2].auto_accept_precision is not None:
        status = CalibrationStatus.CALIBRATED
        selected_accept, selected_adjudicate = winner[0], winner[1]
    else:
        status = CalibrationStatus.RETAINED_INSUFFICIENT_EVIDENCE
        selected_accept, selected_adjudicate = INITIAL_ACCEPT, INITIAL_ADJUDICATE

    return EntityLinkingCalibration(
        status=status,
        selected_accept=selected_accept,
        selected_adjudicate=selected_adjudicate,
        train_eligible_count=len(eligible),
        eligible_with_defined_precision=defined,
        initial_train=compute_policy_metrics(train_stats, INITIAL_ACCEPT, INITIAL_ADJUDICATE),
        initial_development=compute_policy_metrics(
            development_stats, INITIAL_ACCEPT, INITIAL_ADJUDICATE
        ),
        selected_train=compute_policy_metrics(train_stats, selected_accept, selected_adjudicate),
        selected_development=compute_policy_metrics(
            development_stats, selected_accept, selected_adjudicate
        ),
        train_stats=train_stats,
        development_stats=development_stats,
    )


def calibrate_entity_linking(corpus: EntityLinkingGoldCorpus) -> EntityLinkingCalibration:
    """Score the tuning corpus once and run the train-enumerate / development-select protocol."""
    stats = score_corpus(corpus)
    return select_policy(stats[SPLIT_TRAIN], stats[SPLIT_DEVELOPMENT])


# --- the development report -----------------------------------------------------------------
def build_entity_linking_report(
    *, root: Path = GOLD_ROOT, protocol_path: Path = PROTOCOL_DOC_PATH
) -> dict[str, Any]:
    """Build the canonical §5 development report from the train + development tuning slice.

    Loads only the tuning corpus (the sealed evaluation split is excluded by construction), scores it
    once, runs selection, and records the exact selected thresholds and metrics. The protocol
    document and the four tuning-visible inputs (manifest, catalog, train, development) are hashed by
    their exact bytes. ``production_applied`` is always ``false`` -- this report changes nothing.
    """
    corpus = load_tuning_corpus(root)
    calibration = calibrate_entity_linking(corpus)
    grid_points = entity_linking_grid_points()
    input_files = (
        MANIFEST_FILE,
        CATALOG_FILE,
        SPLIT_FILES[SPLIT_TRAIN],
        SPLIT_FILES[SPLIT_DEVELOPMENT],
    )
    changed = (
        calibration.selected_accept != INITIAL_ACCEPT
        or calibration.selected_adjudicate != INITIAL_ADJUDICATE
    )
    return {
        "protocol": {"id": PROTOCOL_ID, "sha256": sha256_file(protocol_path)},
        "domain": DOMAIN,
        "split": REPORT_SPLIT,
        "status": calibration.status.value,
        "dataset": {"dataset_id": DATASET_ID, "schema_version": SCHEMA_VERSION},
        "input_hashes": {name: sha256_file(root / name) for name in input_files},
        "model_versions": {
            "linker_weight_set": LINKER_WEIGHT_SET_ID,
            "policy_version": PROTOCOL_ID,
            "signal_weights": {signal.value: weight for signal, weight in SIGNAL_WEIGHTS.items()},
        },
        "evaluated_grid": {
            "start": GRID_START,
            "stop": GRID_STOP,
            "step": GRID_STEP,
            "points": len(grid_points),
            "policy_count": len(entity_linking_policy_grid()),
            "constraint": "0 <= adjudicate < accept <= 1",
        },
        "counts": {
            "train": len(calibration.train_stats.snapshots),
            "development": len(calibration.development_stats.snapshots),
        },
        "eligibility": {
            "train_eligible_policies": calibration.train_eligible_count,
            "eligible_with_defined_development_precision": (
                calibration.eligible_with_defined_precision
            ),
        },
        "initial_parameters": {
            "accept": _fmt(INITIAL_ACCEPT),
            "adjudicate": _fmt(INITIAL_ADJUDICATE),
        },
        "selected_parameters": {
            "accept": _fmt(calibration.selected_accept),
            "adjudicate": _fmt(calibration.selected_adjudicate),
            "changed_from_initial": changed,
            "production_applied": False,
        },
        "metrics": {
            "initial": {
                "train": calibration.initial_train.as_dict(),
                "development": calibration.initial_development.as_dict(),
            },
            "selected": {
                "train": calibration.selected_train.as_dict(),
                "development": calibration.selected_development.as_dict(),
            },
        },
        "diagnostics": {
            "candidate_recall": {
                "train": _round(_ratio(calibration.train_stats.candidate_hits, calibration.train_stats.links)),
                "development": _round(_ratio(calibration.development_stats.candidate_hits, calibration.development_stats.links)),
            },
            "top_target_recall": {
                "train": _round(_ratio(calibration.train_stats.top_hits, calibration.train_stats.links)),
                "development": _round(_ratio(calibration.development_stats.top_hits, calibration.development_stats.links)),
            },
            "development_by_case_tag": _by_case_tag(
                calibration.development_stats,
                calibration.selected_accept,
                calibration.selected_adjudicate,
            ),
        },
        "limitations": [
            "labels are synthetic/automated original-synthetic English ORG/PRODUCT positives only, "
            "with no human adjudication and no coreference; any calibrated result is against "
            "automated labels, not human-validated ground truth",
            "the stage-3 LLM adjudication is not exercised: routed-link recall credits an "
            "adjudication only when the expected target is among the deterministic candidates, and "
            "does not model the adjudicator's choice",
            "the sealed evaluation split was not read: selection used train + development only, and "
            "the production accept/adjudicate thresholds are unchanged (production_applied is false)",
        ],
    }


def _by_case_tag(
    stats: SplitStats, accept: Decimal, adjudicate: Decimal
) -> dict[str, dict[str, Any]]:
    """Per-case-tag behaviour of the selected policy on development, in fixed CASE_TAGS order."""
    breakdown: dict[str, dict[str, Any]] = {}
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


def write_development_report(
    output_path: Path | str, *, root: Path = GOLD_ROOT, protocol_path: Path = PROTOCOL_DOC_PATH
) -> bytes:
    """Serialize the development report to ``output_path`` as canonical bytes, refusing to overwrite.

    Creates parent directories, writes atomically (temp file then ``os.replace``), and refuses if the
    path already exists -- a one-time report is never silently overwritten. Returns the exact bytes.
    """
    path = Path(output_path)
    if path.exists():
        raise EntityLinkingCalibrationError(f"refusing to overwrite existing report at {path}")
    data = canonical_json_bytes(build_entity_linking_report(root=root, protocol_path=protocol_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


# --- small helpers -------------------------------------------------------------------------
def _round(value: float | None) -> float | None:
    return None if value is None else round(value, METRIC_PRECISION)


def _or_neg_inf(value: float | None) -> float:
    return _NEGATIVE_INFINITY if value is None else value


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _fmt(value: Decimal) -> str:
    return format(value, ".3f")


def main(argv: Iterable[str] | None = None) -> int:
    """CLI: write the canonical development report (no clock, no randomness, no git revision)."""
    parser = argparse.ArgumentParser(description="Stage 9 entity-linking development report.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args(list(argv) if argv is not None else None)
    data = write_development_report(args.output)
    print(f"wrote {len(data)} bytes to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DOMAIN",
    "GRID_START",
    "GRID_STEP",
    "GRID_STOP",
    "INITIAL_ACCEPT",
    "INITIAL_ADJUDICATE",
    "LINKER_WEIGHT_SET_ID",
    "PROTOCOL_ID",
    "EntityLinkingCalibration",
    "EntityLinkingCalibrationError",
    "PolicyMetrics",
    "ScoredMention",
    "SplitStats",
    "build_entity_linking_report",
    "build_identity_source",
    "build_split_stats",
    "calibrate_entity_linking",
    "compute_policy_metrics",
    "entity_linking_grid_points",
    "entity_linking_policy_grid",
    "reband_snapshot",
    "score_corpus",
    "score_mentions",
    "select_policy",
    "validate_policy",
    "write_development_report",
]
