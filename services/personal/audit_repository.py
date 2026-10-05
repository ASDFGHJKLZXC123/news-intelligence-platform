"""Short, independently fenced audit writes; never retain a transaction across HTTP."""

from __future__ import annotations

from services.llm.repository import SQLAlchemyLLMRuntimeRepository
from services.personal.deadlines import check_budget
from services.personal.runs import lock_owned_run


class PersonalAuditRepository:
    def __init__(self, session_factory, run_id, ownership_token, *, ledger_accounted=False):
        self.session_factory = session_factory
        self.run_id = run_id
        self.ownership_token = ownership_token
        self.ledger_accounted = ledger_accounted

    def save_llm_run(self, run):
        check_budget()
        if self.ledger_accounted:
            run.model_params = {**(run.model_params or {}), "personal_paid_ledger_accounted": True}
        with self.session_factory() as session:
            lock_owned_run(session, self.run_id, self.ownership_token)
            SQLAlchemyLLMRuntimeRepository(session, commit_on_write=False).save_llm_run(run)
            # Composer telemetry uses this exact object after the audit session
            # closes. Detach its flushed values before default commit expiration.
            session.expunge(run)
            session.commit()

    def save_job(self, job):
        check_budget()
        with self.session_factory() as session:
            lock_owned_run(session, self.run_id, self.ownership_token)
            SQLAlchemyLLMRuntimeRepository(session).save_job(job)

    def monthly_spend_usd(self, as_of=None):
        check_budget()
        with self.session_factory() as session:
            return SQLAlchemyLLMRuntimeRepository(session).monthly_spend_usd(as_of)
