"""Transaction-boundary tests for the durable LLM runtime repository."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from db.models.core import Job, LLMRun
from services.llm.repository import SQLAlchemyLLMRuntimeRepository


def test_sqlalchemy_repository_commits_writes_by_default() -> None:
    session = MagicMock()
    repository = SQLAlchemyLLMRuntimeRepository(session)
    run = MagicMock(spec=LLMRun)
    job = MagicMock(spec=Job)

    repository.save_llm_run(run)
    repository.save_job(job)

    session.add.assert_called_once_with(run)
    session.merge.assert_called_once_with(job)
    assert session.commit.call_count == 2
    session.flush.assert_not_called()


def test_sqlalchemy_repository_rolls_back_a_failed_write() -> None:
    session = MagicMock()
    session.commit.side_effect = RuntimeError("database unavailable")
    repository = SQLAlchemyLLMRuntimeRepository(session)

    with pytest.raises(RuntimeError, match="database unavailable"):
        repository.save_llm_run(MagicMock(spec=LLMRun))

    session.rollback.assert_called_once_with()


def test_sqlalchemy_repository_can_join_a_caller_owned_transaction() -> None:
    session = MagicMock()
    repository = SQLAlchemyLLMRuntimeRepository(session, commit_on_write=False)

    repository.save_job(MagicMock(spec=Job))

    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_sqlalchemy_repository_returns_database_monthly_sum() -> None:
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = 1.234567
    repository = SQLAlchemyLLMRuntimeRepository(session)

    assert repository.monthly_spend_usd() == pytest.approx(1.234567)
    session.execute.assert_called_once()
