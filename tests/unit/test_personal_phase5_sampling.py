"""Synthetic acceptance checks for trial accounting, never actual reading evidence."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
from collections import Counter
from uuid import UUID

import pytest

from services.personal.trial_sampling import (
    TrialValidationError,
    assess_reliability,
    choose_samples,
)


def uid(number):
    return str(UUID(int=number))


def trial():
    return {
        "schema": "personal-trial.v1",
        "trial_id": uid(100_000),
        "timezone": "America/Los_Angeles",
        "sampling_method": "sha256-round-robin-v1",
    }


def group(number):
    article_id = f"article-{number}"
    return {
        "event_id": uid(number),
        "article_ids": [article_id],
        "source_inputs": [
            {
                "article_id": article_id,
                "title": "Synthetic fixture",
                "rss_summary": "Synthetic retained input",
                "revision_id": uid(400_000 + number),
                "source_id": uid(500_000 + number),
                "content_hash": "0" * 64,
                "url": f"https://example.invalid/article-{number}",
            }
        ],
        "observation_status": "incomplete_pre_snapshot",
    }


def summary(number, day, *, version=1, report=None, published=None):
    return {
        "report_id": report or uid(200_000 + number),
        "version": version,
        "snapshot_id": uid(300_000 + number),
        "event_id": uid(number),
        "published_at": published or f"2026-10-{day:02}T18:30:00-07:00",
        "heading": "Synthetic heading",
        "rendered_summary": "Synthetic event summary",
        "source_inputs": group(number)["source_inputs"],
        "claims": [{"text": "Synthetic claim", "citations": ["fixture-citation"]}],
        "report_introduction_claims": [{"text": "Synthetic introduction claim"}],
    }


def session(day, groups=None, summaries=None, **changes):
    result = {
        "session_id": f"session-{day}",
        "processing_date": f"2026-10-{day:02}",
        "run": {"run_id": uid(600_000 + day), "local_date": f"2026-10-{day:02}"},
        "started_at": f"2026-10-{day:02}T18:00:00-07:00",
        "closed_at": f"2026-10-{day:02}T19:00:00-07:00",
        "actual_reading_session": True,
        "session_status": "closed",
        "workflow_completed": True,
        "developer_intervention": False,
        "groups": groups or [],
        "summaries": summaries or [],
    }
    result.update(changes)
    return result


def populated(days=7):
    return [
        session(
            day,
            [group(day * 100 + j) for j in range(5)],
            [summary(day * 100 + j, day) for j in range(2)],
        )
        for day in range(1, days + 1)
    ]


def test_exact_hash_and_oldest_first_round_robin_from_uncapped_today_population():
    sessions = populated()
    result = choose_samples(trial(), list(reversed(sessions)))
    queues = [
        [
            item["event_id"]
            for item in sorted(
                s["groups"],
                key=lambda item: (
                    hashlib.sha256(
                        f"{trial()['trial_id']}|group|{item['event_id']}".encode()
                    ).hexdigest(),
                    item["event_id"],
                ),
            )
        ]
        for s in sessions
    ]
    expected = [queue[round_number] for round_number in range(5) for queue in queues][:30]
    assert result["selection_order"]["groups"] == expected
    assert Counter(item["session_id"] for item in result["group_sample"]) == {
        "session-1": 5,
        "session-2": 5,
        **{f"session-{day}": 4 for day in range(3, 8)},
    }
    assert len(result["group_population"]) == 35
    assert result["counts"] == {"actual_sessions": 7, "groups": 30, "summaries": 10}
    assert result["state"] == "frozen"
    assert result["minimums_met"]


def test_summary_hash_is_full_identity_and_separate_day_balanced_population():
    result = choose_samples(trial(), populated())
    for unit in result["summary_sample"]:
        value = f"{trial()['trial_id']}|summary|{unit['report_id']}|{unit['version']}|{unit['event_id']}"
        assert unit["selection_hash"] == hashlib.sha256(value.encode()).hexdigest()
        assert unit["unit_id"] == f"{unit['report_id']}|{unit['version']}|{unit['event_id']}"
        assert unit["material"]["report_introduction_claims"]
    assert [item["session_id"] for item in result["summary_sample"][:7]] == [
        f"session-{day}" for day in range(1, 8)
    ]
    assert len({item["event_id"] for item in result["summary_sample"]}) == 10


def test_population_selection_does_not_inspect_wording_or_human_quality_labels():
    sessions = populated()
    first = choose_samples(trial(), sessions)
    for s in sessions:
        s["groups"].reverse()
        for item in s["groups"]:
            item["title"] = "poor and inconvenient"
            item["relevance"] = "not relevant"
            item["coherence"] = "incorrect merge"
        for item in s["summaries"]:
            item["rendered_summary"] = "difficult unsupported material"
            item["factual_support"] = "contradicted"
    second = choose_samples(trial(), sessions)
    assert first["selection_order"] == second["selection_order"]


def test_empty_days_skipped_without_synthetic_groups_and_large_day_not_brief_capped():
    sessions = [session(1, [group(i) for i in range(1, 71)])]
    sessions += [session(day) for day in range(2, 8)]
    result = choose_samples(trial(), sessions)
    assert len(result["group_population"]) == 70
    assert len(result["group_sample"]) == 30
    assert result["summary_sample"] == []
    assert not result["minimums_met"]


def test_same_group_assigned_earliest_actual_session_with_its_observed_membership():
    early = group(1)
    late = group(1)
    late["article_ids"] = ["new-member"]
    late["source_inputs"] = [{"article_id": "new-member", "title": "Later regrouping"}]
    result = choose_samples(trial(), [session(2, [late]), session(1, [early])])
    assert result["group_sample"][0]["material"] == early
    assert result["group_sample"][0]["session_id"] == "session-1"
    assert any(item["reason"] == "event_seen_earlier" for item in result["exclusions"])


def test_earliest_publication_then_numeric_version_then_report_uuid():
    summaries = [
        summary(1, 1, version=9, report=uid(12), published="2026-10-01T18:10:00-07:00"),
        summary(1, 1, version=2, report=uid(13), published="2026-10-01T18:10:00-07:00"),
        summary(1, 1, version=2, report=uid(11), published="2026-10-01T18:10:00-07:00"),
        summary(1, 1, version=1, report=uid(10), published="2026-10-01T18:20:00-07:00"),
    ]
    result = choose_samples(trial(), [session(1, [group(1)], summaries)])
    assert len(result["summary_population"]) == 1
    assert result["summary_sample"][0]["report_id"] == uid(11)
    assert result["summary_sample"][0]["version"] == 2
    assert len(result["summary_candidates"]) == 4


def test_duplicate_summary_occurrence_keeps_earliest_availability_session():
    unit = summary(1, 1)
    result = choose_samples(trial(), [session(2, summaries=[unit]), session(1, summaries=[unit])])
    assert len(result["summary_candidates"]) == 1
    assert result["summary_sample"][0]["session_id"] == "session-1"


def test_missing_publication_chronology_preserves_candidates_and_blocks_substitution():
    sessions = populated()
    sessions[0]["summaries"][0]["published_at"] = None
    result = choose_samples(trial(), sessions)
    assert result["summary_selection_blocked"]
    assert not result["summary_population_complete"]
    assert len(result["summary_candidates"]) == 14
    assert result["summary_sample"] == []
    assert len(result["group_sample"]) == 30
    assert any(
        item["finding"] == "publication_timestamp_unverifiable"
        for item in result["evidence_findings"]
    )
    assert not result["minimums_met"]


def test_missing_retained_sources_are_selected_unverifiable_and_never_replaced():
    sessions = populated()
    initial = choose_samples(trial(), sessions)
    event_id = initial["group_sample"][0]["event_id"]
    for item in sessions[0]["groups"]:
        if item["event_id"] == event_id:
            del item["source_inputs"]
    sessions[0]["summaries"][0].pop("rendered_summary")
    result = choose_samples(trial(), sessions)
    assert result["selection_order"] == initial["selection_order"]
    assert result["group_sample"][0]["evidence_status"] == "unverifiable"
    assert result["group_sample"][0]["article_ids"]
    assert any(
        item["finding"] == "rendered_summary_missing" for item in result["evidence_findings"]
    )


def test_structural_exclusions_leave_raw_quiet_failed_and_nonmatching_outside_population():
    groups = [
        {"kind": "raw_article", "grouped": False},
        {"event_id": uid(2), "qualifying": False},
        {"in_trial_scope": False},
    ]
    summaries = [
        {"kind": "quiet_message"},
        {"publication_status": "failed"},
        {"kind": "report_heading"},
        {"report_type": "legacy_daily_brief"},
    ]
    result = choose_samples(trial(), [session(1, groups, summaries)])
    assert result["group_population"] == result["summary_population"] == []
    assert len(result["exclusions"]) == 7


def test_provisional_sample_recomputes_until_seven_actual_processing_dates():
    sessions = populated(6)
    first = choose_samples(trial(), sessions)
    assert first["state"] == "provisional"
    result = choose_samples(trial(), populated(), previous=first)
    assert result == choose_samples(trial(), populated())


def test_extension_freezes_identities_material_and_order_while_filling_both_minimums():
    sessions = [session(day, [group(day)], [summary(day, day)]) for day in range(1, 8)]
    first = choose_samples(trial(), sessions)
    new_session = session(
        8, [group(i) for i in range(8, 50)], [summary(i, 8) for i in range(8, 14)]
    )
    extended = choose_samples(trial(), sessions + [new_session], previous=first)
    assert extended["group_sample"][:7] == first["group_sample"]
    assert extended["summary_sample"][:7] == first["summary_sample"]
    assert extended["selection_batches"] == [
        [f"session-{day}" for day in range(1, 8)],
        ["session-8"],
    ]
    assert extended["minimums_met"]
    assert extended == choose_samples(trial(), sessions + [new_session], previous=extended)


def test_frozen_extension_after_new_missing_publication_retains_proven_old_summary_selection():
    sessions = populated()
    first = choose_samples(trial(), sessions)
    missing = summary(801, 8)
    missing["published_at"] = None
    result = choose_samples(trial(), sessions + [session(8, summaries=[missing])], previous=first)
    assert result["summary_sample"] == first["summary_sample"]
    assert result["summary_selection_blocked"]
    assert not result["minimums_met"]


@pytest.mark.parametrize(
    "change",
    [
        "membership",
        "drop_population",
        "add_population",
        "remove_session",
        "reorder_selected",
        "change_summary",
        "change_trial",
    ],
)
def test_frozen_history_cannot_be_mutated_to_produce_easier_sample(change):
    sessions = populated()
    first = choose_samples(trial(), sessions)
    config = trial()
    if change == "membership":
        sessions[0]["groups"][0]["article_ids"] = ["changed"]
        sessions[0]["groups"][0]["source_inputs"] = [{"article_id": "changed", "title": "Changed"}]
    elif change == "drop_population":
        sessions[0]["groups"].pop()
    elif change == "add_population":
        sessions[0]["groups"].append(group(999))
    elif change == "remove_session":
        sessions.pop(0)
    elif change == "reorder_selected":
        first["group_sample"].reverse()
    elif change == "change_summary":
        sessions[0]["summaries"][0]["rendered_summary"] = "Changed after scoring"
    else:
        config["trial_id"] = uid(999)
    with pytest.raises(TrialValidationError):
        choose_samples(config, sessions, previous=first)


def test_input_and_output_payloads_are_independent_copies():
    sessions = populated()
    untouched = copy.deepcopy(sessions)
    result = choose_samples(trial(), sessions)
    assert sessions == untouched
    result["group_sample"][0]["material"]["article_ids"].append("changed")
    assert sessions == untouched
    assert all(
        "changed" not in unit["material"]["article_ids"] for unit in result["group_population"]
    )


def test_misses_and_background_runs_are_retained_but_not_actual_sessions():
    missed = session(
        8,
        actual_reading_session=False,
        session_status="missed",
        started_at=None,
        closed_at=None,
        workflow_completed=None,
        developer_intervention=None,
    )
    background = session(9, groups=[group(999)], actual_reading_session=False)
    result = choose_samples(trial(), populated(6) + [missed, background])
    assert result["counts"]["actual_sessions"] == 6
    assert result["state"] == "provisional"
    assert len(result["exclusions"]) == 2
    assert not any(unit["event_id"] == uid(999) for unit in result["group_population"])


def test_retry_across_midnight_keeps_original_date_and_counts_once():
    across = session(
        1,
        started_at="2026-10-02T00:01:00-07:00",
        closed_at="2026-10-02T00:20:00-07:00",
        ordinary_retry_count=1,
    )
    result = assess_reliability([across] + [session(day) for day in range(2, 8)])
    assert result["status"] == "pass"
    assert result["actual_session_count"] == 7
    assert result["original_seven"]["completed_without_developer_intervention"] == 7


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "not-a-uuid"),
        ("article_ids", []),
        ("article_ids", ["a", "a"]),
        ("article_ids", [None]),
        ("source_inputs", "text"),
        ("source_inputs", [{"article_id": "outsider", "title": "outside frozen members"}]),
    ],
)
def test_malformed_group_identity_or_membership_fails_closed(field, value):
    material = group(1)
    material[field] = value
    with pytest.raises(TrialValidationError):
        choose_samples(trial(), [session(1, [material])])


@pytest.mark.parametrize(
    "field,value",
    [
        ("report_id", "wrong"),
        ("snapshot_id", "wrong"),
        ("version", True),
        ("version", 0),
        ("published_at", "2026-10-01T19:01:00-07:00"),
        ("published_at", "2026-10-01T18:30:00"),
    ],
)
def test_malformed_summary_identity_or_unavailable_publication_fails_closed(field, value):
    material = summary(1, 1)
    material[field] = value
    with pytest.raises(TrialValidationError):
        choose_samples(trial(), [session(1, [group(1)], [material])])


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_date",
        "duplicate_id_on_miss",
        "naive_start",
        "end_before_start",
        "wrong_timezone",
        "missed_actual",
        "non_boolean_user_field",
    ],
)
def test_invalid_sessions_fail_closed_before_filtering(change):
    sessions = [session(1), session(2)]
    if change == "duplicate_date":
        sessions[1]["processing_date"] = sessions[0]["processing_date"]
        sessions[1]["run"]["local_date"] = sessions[0]["processing_date"]
    elif change == "duplicate_id_on_miss":
        sessions[1].update(
            session_id="session-1", actual_reading_session=False, session_status="missed"
        )
    elif change == "naive_start":
        sessions[0]["started_at"] = "2026-10-01T18:00:00"
    elif change == "end_before_start":
        sessions[0]["closed_at"] = "2026-10-01T17:00:00-07:00"
    elif change == "wrong_timezone":
        sessions[0]["timezone"] = "America/New_York"
    elif change == "missed_actual":
        sessions[0]["session_status"] = "missed"
    else:
        sessions[0]["developer_intervention"] = "no"
    with pytest.raises(TrialValidationError):
        choose_samples(trial(), sessions)


def test_immutable_summary_material_conflict_and_duplicate_group_fail_closed():
    unit = summary(1, 1)
    changed = copy.deepcopy(unit)
    changed["rendered_summary"] = "Mutable current report"
    with pytest.raises(TrialValidationError):
        choose_samples(trial(), [session(1, summaries=[unit]), session(2, summaries=[changed])])
    with pytest.raises(TrialValidationError):
        choose_samples(trial(), [session(1, [group(1), group(1)])])


def test_reliability_original_seven_never_improves_from_successful_extension():
    sessions = populated()
    for item in sessions[:2]:
        item["workflow_completed"] = False
    result = assess_reliability(sessions + [session(day) for day in range(8, 15)])
    assert result["status"] == "needs_correction"
    assert result["original_seven"]["completed_without_developer_intervention"] == 5
    assert result["extension"]["completed_without_developer_intervention"] == 7
    assert result["extension"]["required_successes"] is None
    assert result["extension"]["status"] == "reported"


def test_six_ordinary_completions_pass_even_with_one_known_failed_session():
    sessions = populated()
    sessions[0]["workflow_completed"] = False
    result = assess_reliability(sessions)
    assert result["status"] == "pass"
    assert result["original_seven"]["not_completed"] == 1
    sessions[1]["developer_intervention"] = True
    result = assess_reliability(sessions)
    assert result["status"] == "needs_correction"
    assert result["original_seven"]["completed_with_developer_intervention"] == 1


@pytest.mark.parametrize("field", ["workflow_completed", "developer_intervention"])
def test_missing_human_fields_remain_unproven_even_with_six_proven_successes(field):
    sessions = populated()
    sessions[0].pop(field)
    result = assess_reliability(sessions)
    assert result["status"] == "insufficient_evidence"
    assert result["original_seven"]["unproven_count"] == 1
    assert result["original_seven"]["unproven_session_ids"] == ["session-1"]
    assert result["original_seven"]["completed_without_developer_intervention"] == 6


def test_extension_unknown_fields_are_separate_and_do_not_rewrite_original_success():
    result = assess_reliability(populated() + [session(8, developer_intervention=None)])
    assert result["original_seven"]["status"] == "pass"
    assert result["extension"]["status"] == "insufficient_evidence"
    assert result["extension"]["unproven_count"] == 1


def test_no_actual_sessions_and_fewer_than_seven_cannot_pass_reliability():
    assert assess_reliability([])["status"] == "insufficient_evidence"
    result = assess_reliability(populated(6))
    assert result["status"] == "insufficient_evidence"
    assert result["original_seven"]["session_count"] == 6
    assert result["extension"]["completion_without_intervention_rate"] is None


def test_missing_run_ownership_evidence_cannot_pass_reliability_but_does_not_change_samples():
    sessions = populated()
    initial = choose_samples(trial(), sessions)
    sessions[0].pop("run")
    assert choose_samples(trial(), sessions)["selection_order"] == initial["selection_order"]
    result = assess_reliability(sessions)
    assert result["status"] == "insufficient_evidence"
    assert result["original_seven"]["identity_unverifiable_session_ids"] == ["session-1"]


def test_same_run_across_midnight_cannot_supply_second_processing_date():
    sessions = [session(1), session(2)]
    sessions[1]["run"]["run_id"] = sessions[0]["run"]["run_id"]
    with pytest.raises(TrialValidationError, match="logical run"):
        assess_reliability(sessions)


@pytest.mark.parametrize("change", ["timezone", "calendar_date", "run_date", "conflicting_run_id"])
def test_capture_identity_must_match_session_metadata(change):
    item = session(1)
    if change == "timezone":
        item["calendar"] = {"persisted_timezone_at_capture": "America/New_York"}
    elif change == "calendar_date":
        item["calendar"] = {"processing_date": "2026-10-02"}
    elif change == "run_date":
        item["run"]["local_date"] = "2026-10-02"
    else:
        item["run_id"] = uid(800_001)
    with pytest.raises(TrialValidationError):
        choose_samples(trial(), [item])


def test_title_only_source_retained_but_unverifiable_provenance_is_not_called_review_ready():
    material = group(1)
    material["source_inputs"] = [{"article_id": "article-1", "title": "Retained title"}]
    result = choose_samples(trial(), [session(1, [material])])
    assert result["group_sample"][0]["evidence_status"] == "unverifiable"
    assert any(item["finding"].endswith(":content_hash") for item in result["evidence_findings"])


def test_frozen_population_input_order_can_change_without_changing_frozen_material():
    sessions = populated()
    original = choose_samples(trial(), sessions)
    for item in sessions:
        item["groups"].reverse()
        item["summaries"].reverse()
    result = choose_samples(trial(), sessions, previous=original)
    assert result["group_sample"] == original["group_sample"]
    assert result["summary_sample"] == original["summary_sample"]


def test_publication_instants_compare_offsets_not_timestamp_lexical_order():
    unit = summary(1, 1, published="2026-10-02T01:30:00+00:00")
    result = choose_samples(trial(), [session(1, summaries=[unit])])
    assert result["summary_sample"][0]["published_at"] == unit["published_at"]
    assert dt.datetime.fromisoformat(unit["published_at"]).hour == 1


def test_missing_published_material_blocks_selection_even_when_other_days_fill_the_target():
    sessions = populated()
    sessions[0].update(summaries=[], summary_population_complete=False)
    result = choose_samples(trial(), sessions)
    assert len(result["summary_candidates"]) == 12
    assert result["summary_sample"] == []
    assert result["summary_selection_blocked"]
    assert not result["summary_population_complete"]
    assert not result["minimums_met"]
    assert len(result["group_sample"]) == 30
    assert {
        "kind": "session",
        "session_id": "session-1",
        "finding": "summary_population_incomplete",
    } in result["evidence_findings"]


def test_incomplete_extension_retains_frozen_summary_sample_without_adding_replacements():
    sessions = [session(day, [group(day)], [summary(day, day)]) for day in range(1, 8)]
    first = choose_samples(trial(), sessions)
    extension = session(
        8,
        [group(8)],
        [summary(number, 8) for number in range(8, 14)],
        summary_population_complete=False,
    )
    result = choose_samples(trial(), sessions + [extension], previous=first)
    assert result["summary_sample"] == first["summary_sample"]
    assert result["group_sample"][:7] == first["group_sample"]
    assert len(result["summary_candidates"]) == 13
    assert not result["minimums_met"]


def test_incomplete_population_flag_cannot_be_coerced_from_ambiguous_text():
    with pytest.raises(TrialValidationError, match="summary_population_complete"):
        choose_samples(trial(), [session(1, summary_population_complete="false")])
