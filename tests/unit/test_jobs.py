"""Stage 1 job contract tests."""

from __future__ import annotations

import datetime

import pytest

from packages.jobs import JobState, Stage1Job, stable_job_key


def test_stable_job_key_is_deterministic_and_order_independent() -> None:
    a = stable_job_key("noop", {"b": 2, "a": 1})
    b = stable_job_key("noop", {"a": 1, "b": 2})
    assert a == b
    assert a.startswith("noop:")


def test_stage1_job_create_is_idempotent_for_same_identity() -> None:
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    first = Stage1Job.create("noop", {"payload": {"hello": "world"}}, now=now)
    second = Stage1Job.create("noop", {"payload": {"hello": "world"}}, now=now)
    assert first.job_key == second.job_key
    assert first.job_id == second.job_id
    assert first.state == JobState.QUEUED
    assert first.idempotency_key == first.job_key
    assert first.safe_to_rerun is True
    assert first.created_at.tzinfo is datetime.UTC
    assert first.updated_at.tzinfo is datetime.UTC
    assert first.can_retry is False


def test_stage1_job_state_transitions_and_retry_contract() -> None:
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    job = Stage1Job.create("noop", {"x": 1}, now=now, max_attempts=3)
    running = job.mark_running(now=now)
    assert running.state == JobState.RUNNING
    assert running.can_retry is False
    failed = running.mark_failed("boom", "failed once", details={"attempt": 1}, now=now)
    assert failed.state == JobState.FAILED
    assert failed.error is not None
    assert failed.error.details["attempt"] == 1
    with pytest.raises(TypeError):
        failed.error.details["attempt"] = 2  # type: ignore[index]
    assert failed.can_retry is True
    retrying = failed.next_attempt(now=now)
    assert retrying.state == JobState.RETRYING
    assert retrying.attempt == 2
    assert retrying.mark_succeeded(now=now).state == JobState.SUCCEEDED


def test_stage1_job_partially_failed_state_is_available() -> None:
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    job = Stage1Job.create("pipeline", {"id": "daily"}, now=now)
    partial = job.mark_running(now=now).mark_partially_failed(
        "partial", "one branch failed", details={"failed_ids": ["event-1"]}, now=now
    )
    assert partial.state == JobState.PARTIALLY_FAILED
    assert partial.error is not None
    assert partial.error.details["failed_ids"] == ("event-1",)


def test_stage1_job_retry_respects_retryable_safe_rerun_and_state() -> None:
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    failed = Stage1Job.create("noop", {"x": 1}, now=now, max_attempts=2).mark_failed(
        "boom", "retryable", now=now
    )
    assert failed.can_retry is True
    assert failed.next_attempt(now=now).can_retry is False

    non_retryable = failed.mark_failed("fatal", "not retryable", retryable=False, now=now)
    assert non_retryable.can_retry is False
    with pytest.raises(ValueError):
        non_retryable.next_attempt(now=now)

    unsafe = Stage1Job(
        job_type=failed.job_type,
        job_key=failed.job_key,
        job_id=failed.job_id,
        state=JobState.FAILED,
        created_at=failed.created_at,
        updated_at=failed.updated_at,
        attempt=failed.attempt,
        max_attempts=failed.max_attempts,
        error=failed.error,
        safe_to_rerun=False,
    )
    assert unsafe.can_retry is False


def test_stage1_job_rejects_non_canonical_identity_values() -> None:
    with pytest.raises(TypeError):
        Stage1Job.create("noop", {"ids": {"a", "b"}})
    with pytest.raises(TypeError):
        stable_job_key("noop", {"custom": object()})


def test_stage1_job_rejects_non_string_identity_mapping_keys() -> None:
    with pytest.raises(TypeError):
        stable_job_key("noop", {1: "a"})
    with pytest.raises(TypeError):
        Stage1Job.create("noop", {"payload": {1: "a"}})


def test_stage1_job_string_and_int_identity_keys_do_not_collide() -> None:
    string_key = stable_job_key("noop", {"1": "a"})
    assert string_key.startswith("noop:")
    with pytest.raises(TypeError):
        stable_job_key("noop", {1: "a"})


def test_stage1_job_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError):
        Stage1Job.create("noop", now=datetime.datetime(2026, 1, 1))
