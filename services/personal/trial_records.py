"""Private, append-only Phase 5 records; these never run the news workflow."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BASELINE_FIELDS = [
    "session_id",
    "date",
    "started_at",
    "reading_minutes",
    "useful_story_count",
    "sources_opened_count",
    "duplicate_coverage_count",
    "interest_area",
    "notes",
]
OBSERVATION_FIELDS = {
    "actual_reading_session",
    "recorded_by",
    "started_at",
    "closed_at",
    "workflow_completed",
    "developer_intervention",
    "ordinary_retry_count",
    "reading_minutes",
    "maintenance_minutes",
    "useful_stories",
    "sources_opened",
    "source_inspection",
    "saving_reopening",
    "brief_reading",
    "export",
    "failures",
    "recovery_actions",
    "attempt_observations",
    "notes",
    "application_revision",
    "runtime_revision_basis",
}


def current_time() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def aware_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"record {field} as an ISO timestamp with a timezone")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"record {field} with a timezone")
    return parsed


def write_new_json(path: Path, value: Any) -> None:
    """Exclusive private file creation: an existing record is never overwritten."""
    encoded = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def application_identity(root: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    paths = []
    for directory in ("apps", "db", "packages", "services", "workers", "frontend/app"):
        paths.extend(path for path in (root / directory).rglob("*") if path.is_file())
    paths.extend(
        root / name
        for name in (
            "scripts/personal-local.py",
            "requirements.txt",
            "requirements-dev.txt",
            "pyproject.toml",
            "infra/docker-compose.personal.yml",
            "frontend/SIGNAL - Intelligence Platform.html",
        )
    )
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
        if "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name != ".DS_Store"
        and not path.is_symlink()
    }
    return {
        "git_head": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "dirty": bool(git("status", "--porcelain")),
        "source_sha256": hashes,
    }


def initialize_trial(destination: Path, root: Path) -> dict[str, Any]:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("captures", "sessions", "samples", "analyses", "reviews"):
        (destination / name).mkdir(mode=0o700)
    trial = {
        "schema": "personal-trial.v1",
        "trial_id": str(uuid.uuid4()),
        "created_at": current_time().isoformat(),
        "timezone": "America/Los_Angeles",
        "sampling_method": "sha256-round-robin-v1",
        "status": "prepared_awaiting_baseline_and_runtime_binding",
        "mode": "raw",
        "application_revision": application_identity(root),
        "expected_migration_head": "0023_personal_processing_control",
        "observed_migration_head": None,
        "profile_revision": None,
        "enabled_features": {"reading": True, "ai": False, "grouping": False, "briefs": False},
        "providers_models": [],
        "price_table_version": None,
        "monthly_allowance_usd": None,
        "run_allowance_usd": None,
        "paid_runtime_enabled": False,
        "selected_preferences_sha256": hashlib.sha256(
            (root / "personal-project-conversion/config/selected-preferences.json").read_bytes()
        ).hexdigest(),
        "selected_preferences_applied": False,
        "population_note": "Raw articles are not grouped-event or brief-summary samples.",
    }
    write_new_json(destination / "trial.json", trial)
    with (destination / "baseline.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=BASELINE_FIELDS)
        writer.writeheader()
        for number in range(1, 4):
            writer.writerow({"session_id": f"baseline-{number}"})
    template = dict.fromkeys(sorted(OBSERVATION_FIELDS))
    template.update(
        actual_reading_session=False,
        recorded_by="user",
        ordinary_retry_count=0,
        useful_stories=[],
        sources_opened=[],
        failures=[],
        recovery_actions=[],
        attempt_observations=[],
    )
    write_new_json(destination / "observation-template.json", template)
    write_new_json(
        destination / "runtime-binding-template.json",
        {
            "schema": "personal-trial-runtime-binding.v1",
            "trial_id": trial["trial_id"],
            "recorded_at": None,
            "profile_revision_id": None,
            "migration_head": None,
            "timezone": trial["timezone"],
            "ai_enabled": False,
            "paid_runtime_enabled": False,
            "application_revision": trial["application_revision"],
            "runtime_revision_basis": None,
        },
    )
    write_new_json(
        destination / "release-assessment.json",
        {
            "trial_id": trial["trial_id"],
            "decision": "insufficient_evidence",
            "actual_sessions": 0,
            "baseline_sessions": 0,
            "group_sample_count": 0,
            "summary_sample_count": 0,
            "user_relevance_judgments": 0,
            "continued_use_judgment": None,
            "acceptance": {f"P5-{number:02}": "pending" for number in range(1, 8)},
        },
    )
    write_new_json(destination / "corrections.json", [])
    write_new_json(destination / "missed-dates.json", [])
    write_new_json(destination / "reviews/relevance.json", [])
    write_new_json(destination / "reviews/coherence.json", [])
    write_new_json(destination / "reviews/claims.json", [])
    write_new_json(
        destination / "reviews/usefulness.json",
        {
            "recorded_by": "user",
            "would_continue": None,
            "concrete_value": None,
            "reading_time_comparison": None,
            "cost_and_upkeep_feedback": None,
        },
    )
    return trial


def baseline_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    complete = []
    seen = set()
    for row in rows:
        if not row.get("date"):
            continue
        dt.date.fromisoformat(row["date"])
        if not row.get("session_id") or row["session_id"] in seen:
            raise ValueError("baseline session identities must be present and distinct")
        seen.add(row["session_id"])
        for key in (
            "reading_minutes",
            "useful_story_count",
            "sources_opened_count",
            "duplicate_coverage_count",
        ):
            value = float(row.get(key) or "nan")
            if not 0 <= value < float("inf"):
                raise ValueError(f"baseline {key} must be a recorded nonnegative number")
            if key != "reading_minutes" and not value.is_integer():
                raise ValueError(f"baseline {key} must be an integer count")
        if not row.get("interest_area"):
            raise ValueError("baseline interest area is required")
        complete.append(row)
    return complete


def closed_sessions(destination: Path) -> list[dict[str, Any]]:
    return [read_json(path) for path in sorted((destination / "sessions").glob("*.json"))]


def sample_history(destination: Path) -> list[Path]:
    records = [(path, read_json(path)) for path in (destination / "samples").glob("*.json")]
    if any(type(record.get("history_sequence")) is not int for _, record in records):
        raise ValueError("sample history is missing its sequence")
    records.sort(key=lambda item: item[1]["history_sequence"])
    parent_hash = None
    for number, (path, record) in enumerate(records, 1):
        if (
            record["history_sequence"] != number
            or record.get("previous_manifest_sha256") != parent_hash
        ):
            raise ValueError("sample history changed or contains a gap")
        parent_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return [path for path, _ in records]


def bind_runtime(destination: Path, record: dict[str, Any]) -> None:
    trial = read_json(destination / "trial.json")
    if (
        record.get("schema") != "personal-trial-runtime-binding.v1"
        or record.get("trial_id") != trial["trial_id"]
    ):
        raise ValueError("runtime binding must identify this trial")
    observed_at = aware_timestamp(record.get("recorded_at"), "runtime recorded_at")
    bound_at = current_time()
    if observed_at > bound_at:
        raise ValueError("runtime observation cannot be future-dated")
    uuid.UUID(record.get("profile_revision_id") or "")
    if (
        record.get("migration_head") != trial["expected_migration_head"]
        or record.get("timezone") != trial["timezone"]
    ):
        raise ValueError("record the supported migration and persisted calendar")
    if record.get("ai_enabled") is not False or record.get("paid_runtime_enabled") is not False:
        raise ValueError("this trial requires raw reading with model spending disabled")
    if record.get("application_revision") != trial["application_revision"] or not record.get(
        "runtime_revision_basis"
    ):
        raise ValueError("record the actual candidate launch/source provenance")
    if closed_sessions(destination):
        raise ValueError("runtime binding must precede the first actual trial session")
    write_new_json(
        destination / "runtime-binding.json", {**record, "bound_at": bound_at.isoformat()}
    )


def record_session(destination: Path, capture: dict, observations: dict) -> dict[str, Any]:
    """Require explicit user observations and keep each run/date in one session."""
    from services.personal.trial_sampling import choose_samples

    trial = read_json(destination / "trial.json")
    if not (destination / "runtime-binding.json").is_file():
        raise ValueError("bind the actual runtime/profile before the first trial session")
    binding = read_json(destination / "runtime-binding.json")
    if capture.get("trial_id") != trial["trial_id"]:
        raise ValueError("capture belongs to a different trial")
    if capture.get("schema") != "personal-session.v1" or capture.get("session_status") != "draft":
        raise ValueError("a session requires an unmodified draft evidence capture")
    if capture.get("calendar", {}).get("persisted_timezone_at_capture") != trial["timezone"]:
        raise ValueError("persisted calendar differs from this trial")
    if capture.get("run", {}).get("local_date") != capture.get("processing_date"):
        raise ValueError("session date must remain the original run's processing date")
    baseline = baseline_rows(destination / "baseline.csv")
    if len(baseline) < 3:
        raise ValueError("record three ordinary-reading baseline sessions before the trial")
    if (
        observations.get("actual_reading_session") is not True
        or observations.get("recorded_by") != "user"
    ):
        raise ValueError("a real reading session requires explicit user observations")
    if set(observations) - OBSERVATION_FIELDS:
        raise ValueError("unsupported observation fields")
    for key in ("source_inspection", "saving_reopening", "brief_reading", "export"):
        if observations.get(key) not in {
            "exercised_successfully",
            "exercised_failed",
            "not_exercised",
            "unavailable",
        }:
            raise ValueError(f"record whether {key} was exercised")
    for key in (
        "useful_stories",
        "sources_opened",
        "failures",
        "recovery_actions",
        "attempt_observations",
    ):
        if not isinstance(observations.get(key), list):
            raise ValueError(f"record {key} as a list, including an empty list if none")
    for key in ("workflow_completed", "developer_intervention"):
        if not isinstance(observations.get(key), bool):
            raise ValueError(f"record {key} explicitly")
    for key in ("reading_minutes", "maintenance_minutes"):
        value = observations.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value < float("inf")
        ):
            raise ValueError(f"record {key} as a nonnegative number")
    retry_count = observations.get("ordinary_retry_count")
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise ValueError("record the ordinary retry count")
    result = {**capture, **observations, "session_status": "closed"}
    # A delayed export cannot stand in for a snapshot taken at session close.
    close = aware_timestamp(result.get("closed_at"), "closed_at")
    captured = aware_timestamp(result.get("captured_at"), "captured_at")
    start = aware_timestamp(result.get("started_at"), "started_at")
    if start < max(
        aware_timestamp(trial.get("created_at"), "trial created_at"),
        aware_timestamp(binding.get("bound_at"), "runtime bound_at"),
    ):
        raise ValueError("actual trial sessions must follow trial creation and runtime binding")
    if aware_timestamp(binding.get("recorded_at"), "runtime recorded_at") > start:
        raise ValueError("runtime binding must precede the actual reading session")
    if (
        any(value.utcoffset() is None for value in (start, captured, close))
        or not start <= captured <= close
    ):
        raise ValueError("capture the evidence during the session, before its recorded close")
    first_start = min(
        [
            start,
            *[
                aware_timestamp(item.get("started_at"), "started_at")
                for item in closed_sessions(destination)
            ],
        ]
    )
    for row in baseline:
        day = dt.date.fromisoformat(row["date"])
        if day > first_start.astimezone(ZoneInfo(trial["timezone"])).date():
            raise ValueError("baseline must precede the application trial")
        if day == first_start.astimezone(ZoneInfo(trial["timezone"])).date():
            baseline_start = aware_timestamp(row.get("started_at"), "baseline started_at")
            if (
                baseline_start.utcoffset() is None
                or baseline_start + dt.timedelta(minutes=float(row["reading_minutes"]))
                > first_start
            ):
                raise ValueError("same-day baseline requires an earlier timed observation")
    identity = observations.get("application_revision")
    if identity != trial["application_revision"]:
        raise ValueError("record the actual runtime revision; changed source needs a new trial")
    if not observations.get("runtime_revision_basis"):
        raise ValueError("record how the running application's revision was established")
    if capture.get("profile", {}).get("profile_revision_id") != binding["profile_revision_id"]:
        raise ValueError("captured profile differs from the pretrial binding")
    if capture.get("exporter_source_identity") != trial["application_revision"]:
        raise ValueError("exporter source changed; prepare a new trial candidate")
    sessions = closed_sessions(destination)
    if sessions and capture.get("profile") != sessions[0].get("profile"):
        raise ValueError("profile changed; start a newly identified trial")
    if capture.get("profile", {}).get("ai_enabled") is not False:
        raise ValueError("this raw-reading trial requires AI disabled")
    if capture.get("observed_migration_head") != trial["expected_migration_head"]:
        raise ValueError("observed migration state does not match this trial")
    choose_samples(trial, [*sessions, result])  # validates date/identity/evidence before writing
    session_id = str(uuid.UUID(result["session_id"]))
    write_new_json(destination / "sessions" / f"{session_id}.json", result)
    return result
