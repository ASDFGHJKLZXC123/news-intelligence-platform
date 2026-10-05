"""A paid dispatch permission uses the same control transaction as business ownership."""

from __future__ import annotations

import pytest

from services.personal.spending import PaidWorkBlocked
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_spending import reserve, seed

pytestmark = pytest.mark.integration


def test_paid_reservation_rejects_a_run_without_global_personal_ownership():
    # Possessing a run token is insufficient authority in the other durable mode.
    # Author the mismatched legacy control explicitly after creating a valid owned run.
    with migrated_disposable_engine() as engine:
        factory, ledgers = seed(engine, dates=1)
        from db.models import PersonalWriterMode

        with factory() as session:
            session.get(PersonalWriterMode, True).mode = "legacy"
            session.commit()
        with pytest.raises(PaidWorkBlocked):
            reserve(ledgers[0])


def test_durable_dispatch_permission_needs_full_call_budget():
    import datetime as dt

    from sqlalchemy import func, select

    from db.models import PersonalRun
    from db.models.personal_spending import PersonalPaidRequest
    from tests.integration.test_personal_phase4a_faults import _paid_owner

    with migrated_disposable_engine() as engine:
        factory, ledger = _paid_owner(engine)
        with factory() as session:
            run = session.get(PersonalRun, ledger.run_id)
            too_late = run.graceful_deadline_at - dt.timedelta(seconds=29)
        ledger.clock = lambda: too_late
        with pytest.raises(PaidWorkBlocked, match="deadline"):
            reserve(ledger)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0
