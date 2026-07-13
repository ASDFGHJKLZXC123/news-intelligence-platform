"""Postgres integration coverage for Stage 2 durable LLM runtime state."""

from __future__ import annotations

import datetime
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text

from db.base import SessionLocal, engine
from db.models.core import Job, LLMRun
from services.llm.repository import SQLAlchemyLLMRuntimeRepository

pytestmark = pytest.mark.integration


def _run(*, cost_usd: float, started_at: datetime.datetime) -> LLMRun:
    return LLMRun(
        prompt_name="integration",
        prompt_version="v1",
        provider="fake",
        model="fake-model",
        status="succeeded",
        attempt=1,
        cost_usd=cost_usd,
        started_at=started_at,
        completed_at=started_at,
    )


@pytest.fixture
def clean_llm_runtime_tables(require_postgres: None):
    command.upgrade(Config("alembic.ini"), "head")
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE llm_runs, jobs RESTART IDENTITY CASCADE"))
    try:
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE llm_runs, jobs RESTART IDENTITY CASCADE"))


def test_repository_durably_persists_runs_jobs_and_reads_monthly_spend(
    clean_llm_runtime_tables: None,
) -> None:
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.UTC)
    job_id = uuid.uuid4()

    with SessionLocal() as session:
        repository = SQLAlchemyLLMRuntimeRepository(session)
        repository.save_llm_run(_run(cost_usd=0.001003, started_at=now))
        repository.save_llm_run(
            _run(cost_usd=9.0, started_at=datetime.datetime(2026, 6, 30, tzinfo=datetime.UTC))
        )
        repository.save_job(
            Job(
                id=job_id,
                job_key="llm-runtime-integration",
                job_type="llm",
                state="llm_validation_failed",
                attempt=2,
                max_attempts=3,
                error={"code": "schema_validation"},
                safe_to_rerun=True,
                created_at=now,
                updated_at=now,
            )
        )
        assert repository.monthly_spend_usd(as_of=now) == pytest.approx(0.001003)

    # A new Session proves that commit-on-write made both records durable.
    with SessionLocal() as session:
        persisted_job = session.get(Job, job_id)
        assert persisted_job is not None
        assert persisted_job.state == "llm_validation_failed"
        assert len(session.scalars(select(LLMRun)).all()) == 2


def test_event_analogy_constraint_is_the_zero_to_one_hundred_contract(
    require_postgres: None,
) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    with engine.connect() as connection:
        checks = {
            item["name"]: item["sqltext"]
            for item in inspect(connection).get_check_constraints("event_analogies")
        }

    constraint = checks["ck_event_analogies_similarity_score"]
    assert "100" in constraint
