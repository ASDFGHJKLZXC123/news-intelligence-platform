"""Stage 9 entity-linking accept/adjudicate calibration (`stage9-validation.v1` §4.2).

Every test is offline and holdout-safe: the synthetic re-banding/metric/selection tests build
``ScoredMention`` snapshots directly, and the real-corpus tests load only ``load_tuning_corpus``
(train + development). No test opens, counts, or parses the sealed evaluation split, and a C-level
audit run proves it. No database, network, spaCy, or LLM is used anywhere.
"""

from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from services.entities.news_linking import ACCEPT_THRESHOLD, ADJUDICATE_THRESHOLD, LinkBand
from services.evaluation import entity_linking_calibration as elc
from services.evaluation.calibration import CalibrationStatus, canonical_json_bytes, sha256_file
from services.evaluation.entity_linking_calibration import (
    GRID_STEP,
    INITIAL_ACCEPT,
    INITIAL_ADJUDICATE,
    EntityLinkingCalibrationError,
    PolicyMetrics,
    ScoredMention,
    SplitStats,
    build_entity_linking_report,
    build_split_stats,
    calibrate_entity_linking,
    compute_policy_metrics,
    entity_linking_grid_points,
    entity_linking_policy_grid,
    reband_snapshot,
    score_corpus,
    select_policy,
    validate_policy,
    write_development_report,
)
from services.evaluation.entity_linking_gold import (
    CATALOG_FILE,
    GOLD_ROOT,
    MANIFEST_FILE,
    SPLIT_DEVELOPMENT,
    SPLIT_FILES,
    SPLIT_TRAIN,
    load_tuning_corpus,
)

ACCEPT = Decimal("0.850")
ADJUDICATE = Decimal("0.500")


# --- snapshot builders ---------------------------------------------------------------------
def make_snapshot(
    mention_id: str = "m",
    *,
    is_link: bool = True,
    score: str | None = None,
    correct: bool = True,
    expected_in_candidates: bool = True,
    tie: bool = False,
    alias_type: str = "legal_name",
    supported: bool = True,
    tags: tuple[str, ...] = ("legal_name",),
    split: str = SPLIT_DEVELOPMENT,
) -> ScoredMention:
    """A synthetic snapshot with exactly the fields a policy re-bands on."""
    top_id = uuid.uuid4() if score is not None else None
    expected = None
    if is_link:
        expected = top_id if (correct and top_id is not None) else uuid.uuid4()
    candidates: list[uuid.UUID] = []
    if top_id is not None:
        candidates.append(top_id)
        if tie:
            candidates.append(uuid.uuid4())
        # A no-candidate mention keeps an empty list (a candidate-recall miss); only a mention
        # that already has a top candidate can also carry the expected target among its candidates.
        if expected is not None and expected_in_candidates and expected not in candidates:
            candidates.append(expected)
    return ScoredMention(
        mention_id=mention_id,
        split=split,
        case_tags=tags,
        is_link=is_link,
        expected_target=expected,
        candidate_ids=tuple(candidates),
        top_id=top_id,
        top_score=Decimal(score) if score is not None else None,
        top_tie=tie,
        top_alias_type=alias_type if top_id is not None else None,
        top_supported=supported if top_id is not None else False,
    )


def pm(**overrides: int) -> PolicyMetrics:
    """A PolicyMetrics with zeroed counts except those overridden (for selection-key tests)."""
    fields = {
        "total": 100,
        "links": 0,
        "nils": 0,
        "candidate_hits": 0,
        "top_hits": 0,
        "accepted_total": 0,
        "accepted_correct": 0,
        "accepted_wrong_target": 0,
        "accepted_nil": 0,
        "adjudications": 0,
        "nil_decisions": 0,
        "nil_recalled": 0,
        "routed_hits": 0,
    }
    fields.update(overrides)
    return PolicyMetrics(**fields)  # type: ignore[arg-type]


# --- grid and policy enumeration -----------------------------------------------------------
def test_the_threshold_axis_is_201_inclusive_points() -> None:
    points = entity_linking_grid_points()
    assert len(points) == 201
    assert points[0] == Decimal("0.000")
    assert points[-1] == Decimal("1.000")


def test_the_policy_grid_is_exactly_20100_ordered_pairs() -> None:
    grid = entity_linking_policy_grid()
    assert len(grid) == 20100
    step = Decimal(GRID_STEP)
    for accept, adjudicate in grid:
        assert adjudicate < accept
        assert Decimal(0) <= adjudicate and accept <= Decimal(1)
        assert divmod(accept, step)[1] == 0 and divmod(adjudicate, step)[1] == 0
    assert len(set(grid)) == 20100


# --- re-banding: boundaries and gates ------------------------------------------------------
@pytest.mark.parametrize(
    ("score", "band"),
    [
        ("1.000", LinkBand.ACCEPT),
        ("0.850", LinkBand.ACCEPT),  # exactly accept accepts (inclusive)
        ("0.845", LinkBand.ADJUDICATE),  # just below accept
        ("0.500", LinkBand.ADJUDICATE),  # exactly adjudicate adjudicates (inclusive)
        ("0.495", LinkBand.NIL),  # just below adjudicate
        ("0.000", LinkBand.NIL),
    ],
)
def test_reband_boundaries_are_inclusive(score: str, band: LinkBand) -> None:
    snapshot = make_snapshot(score=score)
    resolved, chosen = reband_snapshot(snapshot, ACCEPT, ADJUDICATE)
    assert resolved is band
    assert (chosen == snapshot.top_id) is (band is LinkBand.ACCEPT)


def test_a_no_candidate_mention_is_nil_for_every_policy() -> None:
    snapshot = make_snapshot(score=None)
    for accept, adjudicate in ((ACCEPT, ADJUDICATE), (Decimal("0.005"), Decimal("0.000"))):
        assert reband_snapshot(snapshot, accept, adjudicate) == (LinkBand.NIL, None)


def test_an_exact_top_score_tie_is_routed_to_adjudication_not_accepted() -> None:
    tied = make_snapshot(score="0.900", tie=True)
    separable = make_snapshot(score="0.900", tie=False)
    assert reband_snapshot(tied, ACCEPT, ADJUDICATE) == (LinkBand.ADJUDICATE, None)
    assert reband_snapshot(separable, ACCEPT, ADJUDICATE)[0] is LinkBand.ACCEPT


def test_an_unsupported_brand_product_top_is_routed_to_adjudication() -> None:
    unsupported = make_snapshot(score="0.900", alias_type="brand_product", supported=False)
    supported = make_snapshot(score="0.900", alias_type="brand_product", supported=True)
    other = make_snapshot(score="0.900", alias_type="colloquial", supported=False)
    assert reband_snapshot(unsupported, ACCEPT, ADJUDICATE) == (LinkBand.ADJUDICATE, None)
    assert reband_snapshot(supported, ACCEPT, ADJUDICATE)[0] is LinkBand.ACCEPT
    assert reband_snapshot(other, ACCEPT, ADJUDICATE)[0] is LinkBand.ACCEPT  # gate is brand-only


# --- metrics -------------------------------------------------------------------------------
def _mixed_split() -> SplitStats:
    return build_split_stats(
        [
            make_snapshot("s1", score="0.900", correct=True),  # accept correct
            make_snapshot("s2", score="0.900", correct=False),  # accept wrong target
            make_snapshot("s3", is_link=False, score="0.900"),  # accept a NIL
            make_snapshot("s4", score="0.600", correct=True),  # adjudicate, expected in cands
            make_snapshot("s5", score="0.400", correct=True),  # below adjudicate -> NIL
            make_snapshot("s6", score=None, correct=True),  # link with no candidate -> NIL
            make_snapshot("s7", is_link=False, score=None),  # NIL, no candidate -> NIL
            make_snapshot("s8", is_link=False, score="0.600"),  # NIL adjudicated
        ]
    )


def test_metrics_count_accepts_adjudications_and_nils_exactly() -> None:
    metrics = compute_policy_metrics(_mixed_split(), ACCEPT, ADJUDICATE)
    assert (metrics.links, metrics.nils) == (5, 3)
    assert metrics.accepted_total == 3
    assert metrics.accepted_correct == 1
    assert metrics.accepted_wrong_target == 1
    assert metrics.accepted_nil == 1
    assert metrics.adjudications == 2  # s4, s8
    assert metrics.nil_decisions == 3  # s5, s6, s7
    assert metrics.auto_accept_precision == pytest.approx(1 / 3)
    assert metrics.correct_auto_link_recall == pytest.approx(0.2)
    assert metrics.routed_link_recall == pytest.approx(0.4)  # s1 accept + s4 adjudicate-in-cands
    assert metrics.nil_recall == pytest.approx(1 / 3)  # only s7 NIL'd
    assert metrics.candidate_recall == pytest.approx(0.8)  # s1,s2,s4,s5
    assert metrics.top_target_recall == pytest.approx(0.6)  # s1,s4,s5
    assert metrics.adjudication_rate == pytest.approx(0.25)


def test_zero_accept_precision_is_none_never_one() -> None:
    metrics = compute_policy_metrics(_mixed_split(), Decimal("1.000"), ADJUDICATE)
    assert metrics.accepted_total == 0
    assert metrics.auto_accept_precision is None
    assert metrics.as_dict()["auto_accept_precision"] is None


def test_routed_link_recall_credits_adjudication_reaching_the_expected_target() -> None:
    reached = build_split_stats([make_snapshot("r", score="0.600", correct=True)])
    missed = build_split_stats(
        [make_snapshot("m", score="0.600", correct=False, expected_in_candidates=False)]
    )
    assert compute_policy_metrics(reached, ACCEPT, ADJUDICATE).routed_link_recall == 1.0
    assert compute_policy_metrics(missed, ACCEPT, ADJUDICATE).routed_link_recall == 0.0


def test_train_eligibility_gate_boundaries() -> None:
    # >= 1 accept, precision >= 0.95, NIL recall >= 0.80
    assert pm(accepted_total=20, accepted_correct=19, nils=10, nil_recalled=8).is_train_eligible
    assert not pm(accepted_total=0, nils=10, nil_recalled=10).is_train_eligible  # no accept
    assert not pm(
        accepted_total=100, accepted_correct=94, nils=10, nil_recalled=10
    ).is_train_eligible  # precision 0.94
    assert not pm(
        accepted_total=20, accepted_correct=20, nils=10, nil_recalled=7
    ).is_train_eligible  # NIL recall 0.70


# --- selection tie-breaks (each isolated on the selection key) -----------------------------
def _key_pick(a: tuple, b: tuple) -> tuple:
    return max([a, b], key=elc._selection_sort_key)


def test_tiebreak_1_precision_none_ranks_below_every_number() -> None:
    none_precision = (Decimal("0.600"), Decimal("0.200"), pm(accepted_total=0))
    real_precision = (Decimal("0.500"), Decimal("0.100"), pm(accepted_total=2, accepted_correct=1))
    assert _key_pick(none_precision, real_precision) is real_precision


def test_tiebreak_2_correct_auto_link_recall() -> None:
    low = (Decimal("0.500"), Decimal("0.100"), pm(accepted_total=2, accepted_correct=2, links=10))
    high = (Decimal("0.500"), Decimal("0.200"), pm(accepted_total=4, accepted_correct=4, links=10))
    assert _key_pick(low, high) is high  # both precision 1.0, higher link recall wins


def test_tiebreak_3_routed_link_recall() -> None:
    base = {"accepted_total": 2, "accepted_correct": 2, "links": 10}
    low = (Decimal("0.500"), Decimal("0.100"), pm(**base, routed_hits=2))
    high = (Decimal("0.500"), Decimal("0.200"), pm(**base, routed_hits=5))
    assert _key_pick(low, high) is high


def test_tiebreak_4_nil_recall() -> None:
    base = {"accepted_total": 2, "accepted_correct": 2, "links": 10, "routed_hits": 2}
    low = (Decimal("0.500"), Decimal("0.100"), pm(**base, nils=10, nil_recalled=3))
    high = (Decimal("0.500"), Decimal("0.200"), pm(**base, nils=10, nil_recalled=6))
    assert _key_pick(low, high) is high


def test_tiebreak_5_lower_adjudication_rate() -> None:
    base = {"accepted_total": 2, "accepted_correct": 2, "links": 10, "routed_hits": 2, "nils": 10, "nil_recalled": 6}
    fewer = (Decimal("0.500"), Decimal("0.100"), pm(**base, adjudications=5, total=100))
    more = (Decimal("0.500"), Decimal("0.200"), pm(**base, adjudications=10, total=100))
    assert _key_pick(fewer, more) is fewer


def test_tiebreak_6_closer_to_the_initial_point() -> None:
    metrics = pm(accepted_total=2, accepted_correct=2, links=10)
    near = (Decimal("0.850"), Decimal("0.500"), metrics)  # distance 0
    far = (Decimal("0.900"), Decimal("0.450"), metrics)  # distance 0.10
    assert _key_pick(near, far) is near  # dominates the higher-accept preference


def test_tiebreak_7_higher_accept_then_8_higher_adjudicate() -> None:
    metrics = pm(accepted_total=2, accepted_correct=2, links=10)
    higher_accept = (Decimal("0.900"), Decimal("0.500"), metrics)  # distance 0.05
    lower_accept = (Decimal("0.800"), Decimal("0.500"), metrics)  # distance 0.05
    assert _key_pick(higher_accept, lower_accept) is higher_accept

    higher_adj = (Decimal("0.850"), Decimal("0.550"), metrics)  # distance 0.05
    lower_adj = (Decimal("0.850"), Decimal("0.450"), metrics)  # distance 0.05
    assert _key_pick(higher_adj, lower_adj) is higher_adj


# --- selection outcomes (calibrated / retained) --------------------------------------------
def test_no_eligible_policy_retains_the_initial_bands() -> None:
    train = build_split_stats([make_snapshot("t", is_link=False, score="1.000")])  # NIL never NIL'd
    development = build_split_stats([make_snapshot("d", score="0.900", correct=True)])
    result = select_policy(train, development)
    assert result.status is CalibrationStatus.RETAINED_INSUFFICIENT_EVIDENCE
    assert (result.selected_accept, result.selected_adjudicate) == (INITIAL_ACCEPT, INITIAL_ADJUDICATE)
    assert result.train_eligible_count == 0


def test_eligible_but_no_defined_development_precision_retains() -> None:
    train = build_split_stats(
        [make_snapshot("t0", score="1.000", correct=True)]
        + [make_snapshot(f"tn{i}", is_link=False, score=None) for i in range(4)]
    )
    development = build_split_stats(
        [make_snapshot("d0", score="0.000", correct=True)]  # a link that never auto-accepts
        + [make_snapshot(f"dn{i}", is_link=False, score=None) for i in range(2)]
    )
    result = select_policy(train, development)
    assert result.train_eligible_count > 0
    assert result.eligible_with_defined_precision == 0
    assert result.status is CalibrationStatus.RETAINED_INSUFFICIENT_EVIDENCE
    assert (result.selected_accept, result.selected_adjudicate) == (INITIAL_ACCEPT, INITIAL_ADJUDICATE)


# --- policy and snapshot validation adversaries --------------------------------------------
@pytest.mark.parametrize(
    ("accept", "adjudicate"),
    [
        (0.85, 0.5),  # floats refused
        ("0.850", "0.900"),  # adjudicate above accept
        ("0.500", "0.500"),  # adjudicate equal to accept
        ("1.500", "0.100"),  # out of range
        ("0.852", "0.100"),  # off the 0.005 grid
    ],
)
def test_validate_policy_rejects_bad_policies(accept: object, adjudicate: object) -> None:
    with pytest.raises(EntityLinkingCalibrationError):
        validate_policy(accept, adjudicate)  # type: ignore[arg-type]


def test_validate_policy_accepts_a_grid_policy() -> None:
    assert validate_policy("0.850", "0.500") == (Decimal("0.850"), Decimal("0.500"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mention_id": "  "},  # blank id
        {"top_score": Decimal("1.5")},  # out of [0, 1]
        {"is_link": True, "expected_target": None},  # link without expected target
        {"is_link": False, "expected_target": uuid.uuid4()},  # NIL with expected target
    ],
)
def test_scored_mention_rejects_inconsistent_fields(kwargs: dict) -> None:
    base = dict(
        mention_id="m",
        split=SPLIT_DEVELOPMENT,
        case_tags=("legal_name",),
        is_link=True,
        expected_target=uuid.uuid4(),
        candidate_ids=(),
        top_id=None,
        top_score=None,
        top_tie=False,
        top_alias_type=None,
        top_supported=False,
    )
    # keep the link's expected target unless the case overrides it
    base.update(kwargs)
    with pytest.raises(EntityLinkingCalibrationError):
        ScoredMention(**base)  # type: ignore[arg-type]


def test_scored_mention_rejects_a_candidate_top_and_score_that_disagree() -> None:
    with pytest.raises(EntityLinkingCalibrationError):
        ScoredMention(
            mention_id="m",
            split=SPLIT_DEVELOPMENT,
            case_tags=(),
            is_link=True,
            expected_target=uuid.uuid4(),
            candidate_ids=(uuid.uuid4(),),  # a candidate ...
            top_id=None,  # ... but no top id
            top_score=None,
            top_tie=False,
            top_alias_type=None,
            top_supported=False,
        )


def test_build_split_stats_rejects_duplicate_mention_ids() -> None:
    with pytest.raises(EntityLinkingCalibrationError):
        build_split_stats([make_snapshot("dup"), make_snapshot("dup")])


# --- the real tuning corpus ----------------------------------------------------------------
def test_scoring_the_real_tuning_corpus_is_deterministic_with_the_expected_counts() -> None:
    corpus = load_tuning_corpus()
    first = score_corpus(corpus)
    second = score_corpus(load_tuning_corpus())

    assert len(first[SPLIT_TRAIN].snapshots) == 100
    assert len(first[SPLIT_DEVELOPMENT].snapshots) == 50
    for split in (SPLIT_TRAIN, SPLIT_DEVELOPMENT):
        a, b = first[split], second[split]
        assert [s.mention_id for s in a.snapshots] == [s.mention_id for s in b.snapshots]
        assert [s.top_score for s in a.snapshots] == [s.top_score for s in b.snapshots]
    # Every link's correct target is generated as a candidate and ranked first (candidate/top recall).
    for split in (SPLIT_TRAIN, SPLIT_DEVELOPMENT):
        stats = first[split]
        assert stats.candidate_hits == stats.links
        assert stats.top_hits == stats.links


def test_calibrating_the_real_corpus_selects_one_grid_policy_deterministically() -> None:
    result = calibrate_entity_linking(load_tuning_corpus())
    assert result.status is CalibrationStatus.CALIBRATED
    # The deterministic argmax of §4.2's objective over the frozen tuning slice.
    assert (result.selected_accept, result.selected_adjudicate) == (Decimal("0.070"), Decimal("0.000"))
    assert result.selected_development.auto_accept_precision == 1.0
    # A selected policy is a real grid policy. Selection is measured from the ADR 0005 initial
    # bands, not from the live constants -- the freeze (§6) is what applied the selection.
    assert (result.selected_accept, result.selected_adjudicate) in set(entity_linking_policy_grid())
    assert (INITIAL_ACCEPT, INITIAL_ADJUDICATE) == (Decimal("0.850"), Decimal("0.500"))


# --- the development report -----------------------------------------------------------------
def test_the_development_report_is_deterministic_and_canonical() -> None:
    first = build_entity_linking_report()
    second = build_entity_linking_report()
    payload = canonical_json_bytes(first)
    assert payload == canonical_json_bytes(second)
    # canonical JSON round-trips and carries no NaN/Infinity.
    assert json.loads(payload.decode("utf-8")) == first


def test_the_report_hashes_exactly_the_tuning_visible_inputs_and_protocol() -> None:
    report = build_entity_linking_report()
    assert set(report["input_hashes"]) == {
        MANIFEST_FILE,
        CATALOG_FILE,
        SPLIT_FILES[SPLIT_TRAIN],
        SPLIT_FILES[SPLIT_DEVELOPMENT],
    }
    assert report["input_hashes"][MANIFEST_FILE] == sha256_file(GOLD_ROOT / MANIFEST_FILE)
    assert report["input_hashes"][SPLIT_FILES[SPLIT_TRAIN]] == sha256_file(
        GOLD_ROOT / SPLIT_FILES[SPLIT_TRAIN]
    )
    assert report["protocol"]["sha256"] == sha256_file(elc.PROTOCOL_DOC_PATH)


def test_the_report_records_a_recommendation_that_is_not_yet_applied() -> None:
    report = build_entity_linking_report()
    assert report["status"] == CalibrationStatus.CALIBRATED.value
    assert report["selected_parameters"]["production_applied"] is False
    assert report["selected_parameters"]["accept"] == "0.070"
    assert report["evaluated_grid"]["policy_count"] == 20100
    # The report never names the sealed evaluation split.
    assert b"final_holdout" not in canonical_json_bytes(report)
    # The report is the pre-application selection artifact: it still names the ADR 0005 initial
    # bands as its baseline, whatever production now runs.
    assert report["initial_parameters"] == {"accept": "0.850", "adjudicate": "0.500"}
    assert (ACCEPT_THRESHOLD, ADJUDICATE_THRESHOLD) == (
        float(report["selected_parameters"]["accept"]),
        float(report["selected_parameters"]["adjudicate"]),
    )


def test_write_development_report_writes_once_and_refuses_to_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "entity-linking.json"
    data = write_development_report(output)
    assert output.read_bytes() == data == canonical_json_bytes(build_entity_linking_report())
    with pytest.raises(EntityLinkingCalibrationError, match="refusing to overwrite"):
        write_development_report(output)


# --- provenance guardrails -----------------------------------------------------------------
def test_the_evaluator_source_never_reaches_for_the_holdout() -> None:
    source = Path(elc.__file__).read_text(encoding="utf-8")
    for forbidden in ("allow_holdout", "final_holdout", "load_corpus", "load_split"):
        assert forbidden not in source


# A C-level audit hook installed once, below any monkeypatch, recording only while armed.
_AUDIT_OPENS: list[str] = []
_AUDIT_SOCKETS: list[str] = []
_AUDIT_ARMED = False


def _audit_hook(event: str, args: tuple) -> None:
    if not _AUDIT_ARMED:
        return
    if event == "open":
        _AUDIT_OPENS.append(str(args[0]))
    elif event.startswith("socket."):
        _AUDIT_SOCKETS.append(event)


sys.addaudithook(_audit_hook)


def test_building_the_report_opens_only_allowed_files_and_no_network() -> None:
    global _AUDIT_ARMED
    _AUDIT_OPENS.clear()
    _AUDIT_SOCKETS.clear()
    _AUDIT_ARMED = True
    try:
        build_entity_linking_report()
    finally:
        _AUDIT_ARMED = False

    assert not any("final_holdout" in path for path in _AUDIT_OPENS)
    assert _AUDIT_SOCKETS == []  # no network, no DB-over-socket, no LLM call
    gold_json_opens = {
        Path(path).name
        for path in _AUDIT_OPENS
        if "entity_linking" in path and path.endswith(".json")
    }
    assert gold_json_opens <= {
        MANIFEST_FILE,
        CATALOG_FILE,
        SPLIT_FILES[SPLIT_TRAIN],
        SPLIT_FILES[SPLIT_DEVELOPMENT],
    }
    assert any("stage9-validation-protocol.md" in path for path in _AUDIT_OPENS)
