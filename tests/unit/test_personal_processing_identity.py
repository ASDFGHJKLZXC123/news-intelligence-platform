"""Process recovery must preserve identity when executable paths contain spaces."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from services.personal import processing


def test_fixed_child_scan_recognizes_unquoted_spaced_executable(monkeypatch):
    entry = {
        "run_id": "run-id",
        "delivery_token": "delivery-id",
        "generation": 4,
        "executable": sys.executable,
    }
    output = f"123 {sys.executable} -m services.personal.child run-id delivery-id 4\n"
    monkeypatch.setattr(
        processing.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    assert processing._matching_child_argv_exists(entry)


def test_failed_run_retry_waits_for_exact_child_exit_confirmation():
    import datetime as dt
    import uuid

    from db.models import PersonalRun, PersonalWriterMode
    from services.personal.runs import retry_status

    run = PersonalRun(id=uuid.uuid4(), state="failed", attempt=1, max_attempts=3)
    control = PersonalWriterMode(
        mode="personal",
        active_run_id=None,
        unconfirmed_child=True,
        child_history={"owned-child": {"exited": False}},
    )
    assert retry_status(run, now=dt.datetime.now(dt.UTC), control=control) == (
        False,
        "awaiting confirmed child exit or explicit reconciliation",
    )


def test_configured_personal_mode_blocks_real_legacy_session_before_database_or_provider(
    monkeypatch,
):
    import pytest
    from sqlalchemy.orm import Session

    from packages.config import settings
    from services.writer_mode import LegacyWriterModeConflict, require_legacy_writer_mode

    monkeypatch.setattr(
        settings, "get_settings", lambda: SimpleNamespace(personal_processing_mode="personal")
    )
    with Session() as session:
        with pytest.raises(LegacyWriterModeConflict, match="configured personal"):
            require_legacy_writer_mode(session)


def test_seed_and_deadline_imports_do_not_construct_personal_repository():
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "import db.seed.episode_seed; import services.personal.deadlines"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_queue_intent_validates_fixed_transport_before_database():
    import datetime as dt
    import uuid

    import pytest
    from sqlalchemy.orm import Session

    from db.models import PersonalWorkspace
    from services.personal.runs import create_daily_run, retry_run

    workspace = PersonalWorkspace(id=uuid.uuid4())
    now = dt.datetime.now(dt.UTC)
    with Session() as session:
        with pytest.raises(ValueError, match="transport"):
            create_daily_run(session, workspace, now=now, transport="unsupported")
        with pytest.raises(ValueError, match="transport"):
            retry_run(session, workspace, uuid.uuid4(), now=now, transport="unsupported")
