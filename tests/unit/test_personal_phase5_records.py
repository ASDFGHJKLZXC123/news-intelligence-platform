"""Trial bookkeeping cannot turn placeholders or exports into actual use."""

from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import uuid
from pathlib import Path

import pytest

from services.personal import trial_records
from services.personal.trial_records import (
    BASELINE_FIELDS,
    baseline_rows,
    bind_runtime,
    initialize_trial,
    read_json,
    record_session,
    sample_history,
    write_new_json,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def synthetic_clock(monkeypatch):
    monkeypatch.setattr(
        trial_records, "current_time", lambda: dt.datetime(2026, 10, 4, 16, 30, tzinfo=dt.UTC)
    )


def baseline(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=BASELINE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def rows(date="2026-10-01"):
    return [
        {
            "session_id": f"b-{number}",
            "date": date,
            "reading_minutes": "10",
            "useful_story_count": "2",
            "sources_opened_count": "2",
            "duplicate_coverage_count": "0",
            "interest_area": "Technology/business",
        }
        for number in range(3)
    ]


def observation(trial):
    return {
        "actual_reading_session": True,
        "recorded_by": "user",
        "started_at": "2026-10-04T10:00:00-07:00",
        "closed_at": "2026-10-04T10:15:00-07:00",
        "workflow_completed": True,
        "developer_intervention": False,
        "ordinary_retry_count": 0,
        "reading_minutes": 10,
        "maintenance_minutes": 0,
        "useful_stories": [],
        "sources_opened": [],
        "failures": [],
        "recovery_actions": [],
        "attempt_observations": [],
        "source_inspection": "not_exercised",
        "saving_reopening": "not_exercised",
        "brief_reading": "unavailable",
        "export": "unavailable",
        "application_revision": trial["application_revision"],
        "runtime_revision_basis": "synthetic test fixture; no actual session",
    }


def evidence(trial):
    return {
        "schema": "personal-session.v1",
        "trial_id": trial["trial_id"],
        "session_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "session_status": "draft",
        "processing_date": "2026-10-04",
        "calendar": {"persisted_timezone_at_capture": "America/Los_Angeles"},
        "run": {"local_date": "2026-10-04"},
        "captured_at": "2026-10-04T10:12:00-07:00",
        "profile": {"ai_enabled": False, "profile_revision_id": trial["test_profile"]},
        "observed_migration_head": "0023_personal_processing_control",
        "exporter_source_identity": trial["application_revision"],
        "groups": [],
        "summaries": [],
    }


def prepared_trial(folder):
    trial = initialize_trial(folder, ROOT)
    profile_id = str(uuid.uuid4())
    binding = read_json(folder / "runtime-binding-template.json")
    binding.update(
        recorded_at="2026-10-04T09:00:00-07:00",
        profile_revision_id=profile_id,
        migration_head="0023_personal_processing_control",
        runtime_revision_basis="synthetic unit fixture",
    )
    bind_runtime(folder, binding)
    trial["test_profile"] = profile_id
    return trial


def test_new_trial_has_zero_actual_evidence_and_cannot_replace_existing(tmp_path):
    folder = tmp_path / "trial"
    initialize_trial(folder, ROOT)
    assert baseline_rows(folder / "baseline.csv") == []
    assessment = read_json(folder / "release-assessment.json")
    assert assessment["actual_sessions"] == 0
    assert assessment["decision"] == "insufficient_evidence"
    assert set(assessment["acceptance"].values()) == {"pending"}
    assert not list((folder / "sessions").iterdir())
    with pytest.raises(FileExistsError):
        initialize_trial(folder, ROOT)


def test_repeated_baseline_observation_is_rejected(tmp_path):
    path = tmp_path / "baseline.csv"
    baseline(path, [rows()[0]] * 3)
    with pytest.raises(ValueError, match="distinct"):
        baseline_rows(path)


@pytest.mark.parametrize(
    "change",
    [
        {"started_at": None},
        {"started_at": "2026-10-04T10:00:00"},
        {"started_at": "2026-10-04T10:13:00-07:00"},
        {"actual_reading_session": False},
        {"developer_intervention": None},
        {"runtime_revision_basis": None},
        {"started_at": "2026-10-04T09:15:00-07:00"},
    ],
)
def test_invalid_actual_use_is_refused_without_new_session(tmp_path, change):
    folder = tmp_path / "trial"
    trial = prepared_trial(folder)
    baseline(folder / "baseline.csv", rows())
    with pytest.raises(ValueError):
        record_session(folder, evidence(trial), {**observation(trial), **change})
    assert not list((folder / "sessions").iterdir())


def test_baseline_after_trial_is_refused(tmp_path):
    folder = tmp_path / "trial"
    trial = prepared_trial(folder)
    baseline(folder / "baseline.csv", rows("2026-10-05"))
    with pytest.raises(ValueError, match="precede"):
        record_session(folder, evidence(trial), observation(trial))


def test_runtime_binding_refuses_ai_and_cannot_replace_original(tmp_path):
    folder = tmp_path / "trial"
    initialize_trial(folder, ROOT)
    binding = read_json(folder / "runtime-binding-template.json")
    binding.update(
        recorded_at="2026-10-04T09:00:00-07:00",
        profile_revision_id=str(uuid.uuid4()),
        migration_head="0023_personal_processing_control",
        runtime_revision_basis="synthetic unit fixture",
    )
    with pytest.raises(ValueError, match="spending disabled"):
        bind_runtime(folder, {**binding, "ai_enabled": True})
    assert not (folder / "runtime-binding.json").exists()
    bind_runtime(folder, binding)
    with pytest.raises(FileExistsError):
        bind_runtime(folder, binding)


def test_recorded_date_cannot_be_repeated_and_original_record_survives(tmp_path):
    folder = tmp_path / "trial"
    trial = prepared_trial(folder)
    baseline(folder / "baseline.csv", rows())
    first = record_session(folder, evidence(trial), observation(trial))
    original = (folder / "sessions" / f"{first['session_id']}.json").read_bytes()
    with pytest.raises(ValueError, match="distinct"):
        record_session(folder, evidence(trial), observation(trial))
    assert len(list((folder / "sessions").iterdir())) == 1
    assert (folder / "sessions" / f"{first['session_id']}.json").read_bytes() == original


def test_private_record_creation_preserves_previous_bytes(tmp_path):
    path = tmp_path / "retained.json"
    write_new_json(path, {"original": True})
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_new_json(path, {"original": False})
    assert path.read_bytes() == original


def test_sample_history_rejects_modified_predecessor(tmp_path):
    import hashlib

    folder = tmp_path / "trial"
    (folder / "samples").mkdir(parents=True)
    first = folder / "samples" / "first.json"
    second = folder / "samples" / "second.json"
    write_new_json(first, {"history_sequence": 1, "previous_manifest_sha256": None})
    write_new_json(
        second,
        {
            "history_sequence": 2,
            "previous_manifest_sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
        },
    )
    assert sample_history(folder) == [first, second]
    first.write_text('{"history_sequence":1,"previous_manifest_sha256":null}\n')
    with pytest.raises(ValueError, match="changed"):
        sample_history(folder)


def test_cli_capture_never_connects_without_explicit_database(monkeypatch, capsys, tmp_path):
    spec = importlib.util.spec_from_file_location("trial_cli", ROOT / "scripts/personal-trial.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: pytest.fail("connected"))
    assert module.main(["capture", str(tmp_path), "--run-id", str(uuid.uuid4())]) == 1
    assert "Capture failed" in capsys.readouterr().err
