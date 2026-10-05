"""Pure, offline Phase 5 sampling and reliability accounting.

The caller supplies complete, frozen Today populations and published-summary
observations. This module never reads current event rows, calls a provider, or
judges wording. Missing review evidence stays visible on the chosen unit. A
missing publication timestamp blocks new summary selection, since guessing the
earliest version would change the prescribed population.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from collections import deque
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SAMPLING_METHOD = "sha256-round-robin-v1"
SAMPLE_SCHEMA = "personal-samples.v1"
GROUP_TARGET = 30
SUMMARY_TARGET = 10
_DEFAULT_TIMEZONE = "America/Los_Angeles"


class TrialValidationError(ValueError):
    """An ambiguous identity or changed frozen population cannot be sampled."""


def _uuid(value: Any, label: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise TrialValidationError(f"{label} must be a canonical UUID string") from exc
    return value


def _timestamp(value: Any, label: str) -> dt.datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise TrialValidationError(f"{label} must be an ISO timestamp with offset") from exc
    return result


def _zone(value: Any) -> str:
    try:
        if not isinstance(value, str) or not value:
            raise ValueError
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TrialValidationError("timezone must be a persisted IANA timezone") from exc
    return value


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TrialValidationError(f"{label} must contain finite JSON values") from exc


def _sessions(sessions: Any, timezone: str) -> tuple[list[dict], list[dict]]:
    if not isinstance(sessions, list):
        raise TrialValidationError("sessions must be a list")
    actual, excluded = [], []
    seen_ids, actual_dates, actual_runs = set(), set(), set()
    for raw in sessions:
        if not isinstance(raw, dict):
            raise TrialValidationError("each session must be an object")
        item = _json_copy(raw, "session")
        session_id = item.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip() or session_id in seen_ids:
            raise TrialValidationError("session IDs must be nonempty and unique, including misses")
        seen_ids.add(session_id)
        value = item.get("processing_date")
        try:
            if not isinstance(value, str) or dt.date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise TrialValidationError("processing_date must be an ISO calendar date") from exc
        if "timezone" in item and item["timezone"] != timezone:
            raise TrialValidationError("session timezone differs from the persisted trial timezone")
        calendar = item.get("calendar", {})
        if not isinstance(calendar, dict):
            raise TrialValidationError("captured calendar must be an object")
        if calendar.get("persisted_timezone_at_capture", timezone) != timezone:
            raise TrialValidationError("captured timezone differs from the trial timezone")
        if calendar.get("processing_date", value) != value:
            raise TrialValidationError(
                "captured calendar date differs from the session processing date"
            )
        run = item.get("run", {})
        if not isinstance(run, dict):
            raise TrialValidationError("captured run must be an object")
        if run.get("local_date", value) != value or item.get("run_processing_date", value) != value:
            raise TrialValidationError("run date differs from the session processing date")
        run_id = item.get("run_id", run.get("run_id"))
        if "run_id" in run and run["run_id"] != run_id:
            raise TrialValidationError("top-level and captured logical run IDs disagree")
        if run_id is not None:
            _uuid(run_id, "run_id")
        if type(item.get("actual_reading_session")) is not bool:
            raise TrialValidationError("actual_reading_session must be explicitly true or false")
        if item.get("session_status") not in {"closed", "missed"}:
            raise TrialValidationError("only closed or missed sessions can enter a manifest")
        if item["session_status"] == "missed" and item["actual_reading_session"]:
            raise TrialValidationError("a missed session cannot be an actual reading session")
        for key in ("workflow_completed", "developer_intervention"):
            if item.get(key) is not None and type(item[key]) is not bool:
                raise TrialValidationError(f"{key} must be a boolean or null")
        if item.get("started_at") is not None or item.get("closed_at") is not None:
            start = _timestamp(item.get("started_at"), "started_at")
            close = _timestamp(item.get("closed_at"), "closed_at")
            if close < start:
                raise TrialValidationError("closed_at precedes started_at")
        elif item["actual_reading_session"]:
            raise TrialValidationError("actual sessions require start and close timestamps")
        # Retry/reading across midnight retains the original processing date.
        if item["actual_reading_session"]:
            if value in actual_dates:
                raise TrialValidationError("actual sessions must have distinct processing dates")
            actual_dates.add(value)
            if run_id is not None:
                if run_id in actual_runs:
                    raise TrialValidationError("one logical run cannot supply two actual sessions")
                actual_runs.add(run_id)
            actual.append(item)
        else:
            excluded.append(
                {
                    "session_id": session_id,
                    "processing_date": value,
                    "reason": "missed_session"
                    if item["session_status"] == "missed"
                    else "not_actual_reading_session",
                }
            )
    actual.sort(key=lambda item: (item["processing_date"], item["session_id"]))
    excluded.sort(key=lambda item: (item["processing_date"], item["session_id"]))
    return actual, excluded


def _article_ids(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise TrialValidationError(f"{label} must be a nonempty frozen article-ID list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TrialValidationError(f"{label} contains an invalid article ID")
    if len(value) != len(set(value)):
        raise TrialValidationError(f"{label} contains duplicate article IDs")
    return list(value)


def _retained_sources(material: dict, members: list[str] | None) -> list[str]:
    """Inspect structure/presence only; never evaluate source prose or quality."""
    sources = material.get("source_inputs")
    if sources is None:
        return ["retained_source_inputs_missing"]
    if not isinstance(sources, list):
        raise TrialValidationError("source_inputs must be a list")
    if not sources:
        return ["retained_source_inputs_missing"]
    observed, missing = set(), []
    for source in sources:
        if not isinstance(source, dict):
            raise TrialValidationError("each source input must be an object")
        article_id = source.get("article_id")
        if not isinstance(article_id, str) or not article_id.strip() or article_id in observed:
            raise TrialValidationError("source-input article IDs must be nonempty and unique")
        if members is not None and article_id not in members:
            raise TrialValidationError("source input is outside the frozen group membership")
        observed.add(article_id)
        for key in ("source_id", "url", "revision_id", "content_hash"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                missing.append(f"source_provenance_missing:{article_id}:{key}")
        if source.get("revision_id") is not None:
            _uuid(source["revision_id"], "source revision_id")
        content_hash = source.get("content_hash")
        if content_hash is not None and (
            not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(char not in "0123456789abcdef" for char in content_hash)
        ):
            raise TrialValidationError("source content_hash must be lowercase SHA-256 hexadecimal")
        retained = False
        for key in ("title", "rss_summary", "retained_title", "retained_summary", "text"):
            content = source.get(key)
            if content is not None and not isinstance(content, str):
                raise TrialValidationError("retained source content must be text or null")
            retained = retained or bool(content)
        if not retained:
            missing.append(f"retained_source_text_missing:{article_id}")
    if members is not None and observed != set(members):
        missing.append("frozen_member_sources_missing")
    return missing


def _structural_exclusion(material: dict, kind: str) -> str | None:
    # Only explicit structural fields can exclude candidates. Titles, clicks,
    # relevance scores, coherence judgments and summary wording are ignored.
    for key in ("qualifying", "grouped", "in_trial_scope"):
        if key in material and type(material[key]) is not bool:
            raise TrialValidationError(f"{key} must be boolean when supplied")
    if material.get("in_trial_scope") is False:
        return "outside_trial_scope"
    if material.get("qualifying") is False:
        return "nonmatching_today_event"
    if material.get("grouped") is False or material.get("kind") == "raw_article":
        return "raw_ungrouped_article"
    if kind == "summary":
        if material.get("publication_status", "published") != "published":
            return "not_successfully_published"
        if material.get("report_type", "personal_daily_brief") != "personal_daily_brief":
            return "not_personal_brief"
        if material.get("kind") in {"quiet_message", "report_heading", "failed_attempt"}:
            return "not_event_summary"
    return None


def _populations(trial_id: str, actual: list[dict]) -> tuple[list, list, list, list]:
    groups, candidates, exclusions, findings = [], [], [], []
    seen_groups, seen_units = {}, {}
    for session in actual:
        for kind, entries in (
            ("group", session.get("groups", [])),
            ("summary", session.get("summaries", [])),
        ):
            if not isinstance(entries, list):
                raise TrialValidationError(f"{kind} population must be a list")
            for raw in entries:
                if not isinstance(raw, dict):
                    raise TrialValidationError("each population unit must be an object")
                material = _json_copy(raw, f"{kind} material")
                reason = _structural_exclusion(material, kind)
                if reason:
                    exclusions.append(
                        {
                            "session_id": session["session_id"],
                            "kind": kind,
                            "identity": material.get("event_id"),
                            "reason": reason,
                        }
                    )
                    continue
                event_id = _uuid(material.get("event_id"), "event_id")
                unit = {
                    "session_id": session["session_id"],
                    "processing_date": session["processing_date"],
                    "event_id": event_id,
                    "material": material,
                }
                if kind == "group":
                    members = _article_ids(material.get("article_ids"), "article_ids")
                    unit.update(unit_id=event_id, article_ids=members)
                    unit["selection_hash"] = hashlib.sha256(
                        f"{trial_id}|group|{event_id}".encode()
                    ).hexdigest()
                    problems = _retained_sources(material, members)
                    if event_id in seen_groups:
                        if seen_groups[event_id]["session_id"] == session["session_id"]:
                            raise TrialValidationError(
                                "a group appears twice in one full population"
                            )
                        exclusions.append(
                            {
                                "session_id": session["session_id"],
                                "kind": kind,
                                "identity": event_id,
                                "reason": "event_seen_earlier",
                                "assigned_session_id": seen_groups[event_id]["session_id"],
                            }
                        )
                        continue
                    seen_groups[event_id] = unit
                    groups.append(unit)
                else:
                    report_id = _uuid(material.get("report_id"), "report_id")
                    snapshot_id = _uuid(material.get("snapshot_id"), "snapshot_id")
                    version = material.get("version")
                    if type(version) is not int or version < 1:
                        raise TrialValidationError("report version must be a positive integer")
                    unit_id = f"{report_id}|{version}|{event_id}"
                    published = material.get("published_at")
                    if published is not None:
                        if _timestamp(published, "published_at") > _timestamp(
                            session["closed_at"], "closed_at"
                        ):
                            raise TrialValidationError(
                                "a summary was published after session close"
                            )
                    unit.update(
                        unit_id=unit_id,
                        report_id=report_id,
                        snapshot_id=snapshot_id,
                        version=version,
                        published_at=published,
                    )
                    unit["selection_hash"] = hashlib.sha256(
                        f"{trial_id}|summary|{report_id}|{version}|{event_id}".encode()
                    ).hexdigest()
                    members = None
                    if "article_ids" in material:
                        members = _article_ids(material["article_ids"], "summary article_ids")
                    problems = _retained_sources(material, members)
                    for key in ("heading", "rendered_summary"):
                        if not isinstance(material.get(key), str) or not material[key].strip():
                            problems.append(f"{key}_missing")
                    if published is None:
                        problems.append("publication_timestamp_unverifiable")
                    if unit_id in seen_units:
                        if seen_units[unit_id]["material"] != material:
                            raise TrialValidationError(
                                "a published summary identity changed material"
                            )
                        exclusions.append(
                            {
                                "session_id": session["session_id"],
                                "kind": kind,
                                "identity": unit_id,
                                "reason": "unit_seen_earlier",
                                "assigned_session_id": seen_units[unit_id]["session_id"],
                            }
                        )
                        continue
                    seen_units[unit_id] = unit
                    candidates.append(unit)
                unit["evidence_status"] = "unverifiable" if problems else "retained"
                for problem in problems:
                    findings.append(
                        {
                            "kind": kind,
                            "unit_id": unit["unit_id"],
                            "session_id": unit["session_id"],
                            "finding": problem,
                        }
                    )
    positions = {session["session_id"]: index for index, session in enumerate(actual)}
    groups.sort(key=lambda unit: (positions[unit["session_id"]], unit["unit_id"]))
    candidates.sort(key=lambda unit: (positions[unit["session_id"]], unit["unit_id"]))
    for session in actual:
        if session.get("run_id", session.get("run", {}).get("run_id")) is None:
            findings.append(
                {
                    "kind": "session",
                    "session_id": session["session_id"],
                    "finding": "logical_run_identity_unverifiable",
                }
            )
    return groups, candidates, exclusions, findings


def _summary_population(candidates: list[dict], exclusions: list[dict]) -> list[dict]:
    earliest = {}
    for unit in sorted(
        candidates,
        key=lambda item: (
            _timestamp(item["published_at"], "published_at"),
            item["version"],
            item["report_id"],
        ),
    ):
        if unit["event_id"] in earliest:
            exclusions.append(
                {
                    "session_id": unit["session_id"],
                    "kind": "summary",
                    "identity": unit["unit_id"],
                    "reason": "event_summary_seen_earlier",
                    "selected_population_unit_id": earliest[unit["event_id"]]["unit_id"],
                }
            )
        else:
            earliest[unit["event_id"]] = unit
    return list(earliest.values())


def _select(population: list[dict], batches: list[list[str]], limit: int) -> list[dict]:
    selected = []
    for batch in batches:
        queues = [
            deque(
                sorted(
                    (unit for unit in population if unit["session_id"] == session_id),
                    key=lambda unit: (unit["selection_hash"], unit["unit_id"]),
                )
            )
            for session_id in batch
        ]
        while any(queues) and len(selected) < limit:
            for queue in queues:
                if queue and len(selected) < limit:
                    selected.append(queue.popleft())
    return copy.deepcopy(selected)


def choose_samples(trial: dict, sessions: list[dict], previous: dict | None = None) -> dict:
    """Return inspectable samples without inspecting titles or quality judgments.

    Persist the result and pass it back as ``previous`` on extension. Before the
    seventh actual processing date a sample is provisional. Afterward historical
    population material and selections are append-only; changes require explicit
    correction records, rather than a fresh convenient sample.
    """
    if not isinstance(trial, dict) or trial.get("schema") != "personal-trial.v1":
        raise TrialValidationError("trial schema must be personal-trial.v1")
    trial_id = _uuid(trial.get("trial_id"), "trial_id")
    timezone = _zone(trial.get("timezone"))
    if trial.get("sampling_method") != SAMPLING_METHOD:
        raise TrialValidationError(f"sampling_method must be {SAMPLING_METHOD}")
    actual, session_exclusions = _sessions(sessions, timezone)
    groups, candidates, exclusions, findings = _populations(trial_id, actual)
    incomplete_sessions = []
    for session in actual:
        if "summary_population_complete" in session:
            if type(session["summary_population_complete"]) is not bool:
                raise TrialValidationError("summary_population_complete must be explicitly boolean")
            if not session["summary_population_complete"]:
                incomplete_sessions.append(session["session_id"])
                findings.append(
                    {
                        "kind": "session",
                        "session_id": session["session_id"],
                        "finding": "summary_population_incomplete",
                    }
                )
    blocked = bool(incomplete_sessions) or any(unit["published_at"] is None for unit in candidates)
    summaries = [] if blocked else _summary_population(candidates, exclusions)
    ids = [item["session_id"] for item in actual]
    batches = [ids[:7]] if ids else []
    if len(ids) > 7:
        batches.append(ids[7:])
    previous_summaries = []
    if previous is not None:
        if not isinstance(previous, dict) or any(
            previous.get(key) != value
            for key, value in (
                ("schema", SAMPLE_SCHEMA),
                ("trial_id", trial_id),
                ("timezone", timezone),
                ("sampling_method", SAMPLING_METHOD),
            )
        ):
            raise TrialValidationError("previous manifest belongs to a different trial or schema")
        if previous.get("state") not in {"provisional", "frozen"}:
            raise TrialValidationError("previous sample state is invalid")
        if previous["state"] == "frozen":
            old_ids = previous.get("observed_actual_session_ids")
            if not isinstance(old_ids, list) or len(old_ids) < 7 or ids[: len(old_ids)] != old_ids:
                raise TrialValidationError(
                    "frozen actual-session history is not an unchanged prefix"
                )
            for key, population in (
                ("group_population", groups),
                ("summary_candidates", candidates),
            ):
                prior_population = [unit for unit in population if unit["session_id"] in old_ids]
                if previous.get(key) != prior_population:
                    raise TrialValidationError(
                        f"frozen {key} changed; record an explicit correction"
                    )
            batches = previous.get("selection_batches")
            if (
                not isinstance(batches, list)
                or not all(isinstance(batch, list) for batch in batches)
                or not batches
                or batches[0] != old_ids[:7]
                or [session_id for batch in batches for session_id in batch] != old_ids
            ):
                raise TrialValidationError("frozen selection batches are malformed")
            batches = copy.deepcopy(batches)
            expected_groups = _select(previous["group_population"], batches, GROUP_TARGET)
            if previous.get("group_sample") != expected_groups:
                raise TrialValidationError("frozen group selection does not reproduce")
            previous_summaries = copy.deepcopy(previous.get("summary_sample", []))
            if previous.get("summary_selection_blocked"):
                if previous_summaries:
                    # A blocked extension may retain a previously proven selection.
                    by_id = {item["unit_id"]: item for item in previous["summary_candidates"]}
                    if any(by_id.get(item["unit_id"]) != item for item in previous_summaries):
                        raise TrialValidationError(
                            "frozen summary evidence is not in its population"
                        )
            elif previous_summaries != _select(
                previous["summary_population"], batches, SUMMARY_TARGET
            ):
                raise TrialValidationError("frozen summary selection does not reproduce")
            if ids[len(old_ids) :]:
                batches.append(ids[len(old_ids) :])
    selected_groups = _select(groups, batches, GROUP_TARGET)
    selected_summaries = (
        previous_summaries if blocked else _select(summaries, batches, SUMMARY_TARGET)
    )
    if previous is not None and previous.get("state") == "frozen":
        for key, selected in (
            ("group_sample", selected_groups),
            ("summary_sample", selected_summaries),
        ):
            old = previous.get(key, [])
            if selected[: len(old)] != old:
                raise TrialValidationError(f"extension would replace frozen {key}")
    return {
        "schema": SAMPLE_SCHEMA,
        "trial_id": trial_id,
        "timezone": timezone,
        "sampling_method": SAMPLING_METHOD,
        "state": "frozen" if len(actual) >= 7 else "provisional",
        "original_session_ids": ids[:7],
        "extension_session_ids": ids[7:],
        "observed_actual_session_ids": ids,
        "selection_batches": batches,
        "group_population": groups,
        "summary_candidates": candidates,
        "summary_population": candidates if blocked else summaries,
        "summary_selection_blocked": blocked,
        "summary_population_complete": not blocked,
        "group_sample": selected_groups,
        "summary_sample": selected_summaries,
        "selection_order": {
            "groups": [item["unit_id"] for item in selected_groups],
            "summaries": [item["unit_id"] for item in selected_summaries],
        },
        "exclusions": session_exclusions + exclusions,
        "evidence_findings": findings,
        "counts": {
            "actual_sessions": len(actual),
            "groups": len(selected_groups),
            "summaries": len(selected_summaries),
        },
        "minimums_met": len(actual) >= 7
        and len(selected_groups) == GROUP_TARGET
        and len(selected_summaries) == SUMMARY_TARGET
        and not blocked,
    }


def _reliability_window(sessions: list[dict], *, original: bool) -> dict:
    unproven = [
        item["session_id"]
        for item in sessions
        if item.get("workflow_completed") is None or item.get("developer_intervention") is None
    ]
    successful = [
        item["session_id"]
        for item in sessions
        if item.get("workflow_completed") is True and item.get("developer_intervention") is False
    ]
    missing_runs = [
        item["session_id"]
        for item in sessions
        if item.get("run_id", item.get("run", {}).get("run_id")) is None
    ]
    if (original and len(sessions) < 7) or unproven or missing_runs or not sessions:
        status = "insufficient_evidence"
    elif original:
        status = "pass" if len(successful) >= 6 else "needs_correction"
    else:
        status = "reported"
    return {
        "status": status,
        "session_ids": [item["session_id"] for item in sessions],
        "session_count": len(sessions),
        "completed_without_developer_intervention": len(successful),
        "completed_without_developer_intervention_ids": successful,
        "completed_with_developer_intervention": sum(
            item.get("workflow_completed") is True and item.get("developer_intervention") is True
            for item in sessions
        ),
        "not_completed": sum(item.get("workflow_completed") is False for item in sessions),
        "developer_intervention_count": sum(
            item.get("developer_intervention") is True for item in sessions
        ),
        "unproven_count": len(unproven),
        "unproven_session_ids": unproven,
        "identity_unverifiable_session_ids": missing_runs,
        "identity_evidence_complete": not missing_runs,
        "completion_without_intervention_rate": len(successful) / len(sessions)
        if sessions
        else None,
        "required_successes": 6 if original else None,
    }


def assess_reliability(sessions: list[dict]) -> dict:
    """Count the original seven and extension independently; silence is unproven.

    This measures the recorded completion/intervention fields only. It does not
    attest that the reading occurred, verify recovery, or check spending/data
    controls. Those remain separate release-assessment evidence.
    """
    if not isinstance(sessions, list):
        raise TrialValidationError("sessions must be a list")
    timezone = _DEFAULT_TIMEZONE
    supplied_zones = {
        _zone(item["timezone"])
        for item in sessions
        if isinstance(item, dict) and item.get("timezone") is not None
    }
    if len(supplied_zones) > 1:
        raise TrialValidationError("reliability sessions disagree on their persisted timezone")
    if supplied_zones:
        timezone = _zone(next(iter(supplied_zones)))
    actual, exclusions = _sessions(sessions, timezone)
    original = _reliability_window(actual[:7], original=True)
    extension = _reliability_window(actual[7:], original=False)
    return {
        "schema": "personal-trial-reliability.v1",
        "timezone": timezone,
        "status": original["status"],
        "original_seven": original,
        "extension": extension,
        "excluded_sessions": exclusions,
        "actual_session_count": len(actual),
    }
