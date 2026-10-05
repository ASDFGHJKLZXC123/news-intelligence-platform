"""On-demand transport bounds and launcher-independent cancellation, no services."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_runner_import_does_not_construct_celery_or_broker():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import services.personal.runner; assert 'workers.celery_app' not in sys.modules; assert 'celery' not in sys.modules",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "APP_ENV": "test"},
    )
    assert result.returncode == 0, result.stderr


def test_budget_uses_original_absolute_and_monotonic_deadlines():
    from services.personal.deadlines import RuntimeBudget, RuntimeCancelled

    current = dt.datetime(2026, 10, 4, tzinfo=dt.UTC)
    ticks = [100.0]
    wall = [current]
    budget = RuntimeBudget(
        current + dt.timedelta(seconds=5),
        current + dt.timedelta(seconds=6),
        wall_clock=lambda: wall[0],
        monotonic=lambda: ticks[0],
    )
    assert budget.remaining() == 5
    wall[0] -= dt.timedelta(hours=1)
    ticks[0] += 5
    with pytest.raises(RuntimeCancelled, match="deadline"):
        budget.check()
    assert budget.hard_remaining() <= 1


def test_dispatch_requires_full_configured_request_bound():
    from services.personal.deadlines import (
        RuntimeBudget,
        RuntimeCancelled,
        bind_budget,
        require_call_budget,
    )

    now = dt.datetime.now(dt.UTC)
    with bind_budget(RuntimeBudget(now + dt.timedelta(seconds=3), now + dt.timedelta(seconds=4))):
        with pytest.raises(RuntimeCancelled, match="request"):
            require_call_budget(5)
        assert 0 < require_call_budget(1) <= 1


def test_runtime_cancellation_bypasses_item_exception_handlers():
    from services.personal.deadlines import RuntimeCancelled

    assert not issubclass(RuntimeCancelled, Exception)


def test_child_watchdog_survives_launcher_exit_and_ends_owned_group():
    """Actual two-process test: launcher exits, child has original 350ms bound."""
    script = """
import datetime as dt, subprocess, sys
code = '''import datetime as dt, time, signal
signal.signal(signal.SIGUSR1, lambda *_: None)
from services.personal.deadlines import RuntimeBudget, RuntimeWatchdog
now = dt.datetime.now(dt.UTC)
budget = RuntimeBudget(now + dt.timedelta(seconds=.2), now + dt.timedelta(seconds=.35))
watchdog = RuntimeWatchdog(budget, owned_group=True)
watchdog.start()
print('ready', flush=True)
while True: time.sleep(1)
'''
p = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
print(p.pid, flush=True)
"""
    began = time.monotonic()
    launched = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert 0.30 <= time.monotonic() - began < 3
    assert launched.returncode == 0, launched.stderr
    pid = int(launched.stdout.splitlines()[0])
    assert "ready" in launched.stdout
    # Captured stdout closes only after the independently owned child exits.
    assert launched.stderr == ""
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_supervisor_passes_fixed_identifiers_and_never_shell(monkeypatch):
    from services.personal.supervisor import PersonalSupervisor

    calls = []

    class Process:
        pid = 812345

        def poll(self):
            return 0

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    supervisor = PersonalSupervisor(
        record_launch=lambda *args: None,
        record_exit=lambda *args: None,
        read_deadline=lambda *args: dt.datetime.now(dt.UTC) + dt.timedelta(seconds=120),
        popen=launch,
    )
    supervisor.launch(uuid.UUID(int=1), uuid.UUID(int=2), 4)
    argv, kwargs = calls[0]
    assert argv == [
        sys.executable,
        "-m",
        "services.personal.child",
        str(uuid.UUID(int=1)),
        str(uuid.UUID(int=2)),
        "4",
    ]
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    supervisor.shutdown(timeout=0)


def test_supervisor_refuses_launch_after_shutdown():
    from services.personal.supervisor import LauncherUnavailable, PersonalSupervisor

    supervisor = PersonalSupervisor(
        record_launch=lambda *args: None, record_exit=lambda *args: None
    )
    supervisor.shutdown(timeout=0)
    with pytest.raises(LauncherUnavailable):
        supervisor.launch(uuid.uuid4(), uuid.uuid4(), 1)


def test_paid_dispatch_insufficient_remaining_budget_never_reserves():
    from services.personal.deadlines import RuntimeBudget, RuntimeCancelled, bind_budget
    from services.personal.paid_runtime import DurablePaidHTTPClient
    from services.personal.spending import PaidRoute
    from tests.unit.test_personal_spending import model_route

    class Ledger:
        def reserve(self, *args, **kwargs):
            pytest.fail("paid reservation must not begin without full configured request budget")

    route = PaidRoute.from_mapping(model_route()["generation"], role="generation")
    client = DurablePaidHTTPClient(route=route, ledger=Ledger(), delegate=object())
    now = dt.datetime.now(dt.UTC)
    with bind_budget(RuntimeBudget(now + dt.timedelta(seconds=1), now + dt.timedelta(seconds=2))):
        with pytest.raises(RuntimeCancelled):
            client.post(
                "/v1/chat/completions",
                json={
                    "model": route.model,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "synthetic"}],
                },
            )


def test_supervisor_spawn_failure_confirms_no_child_and_refuses_unowned_signal():
    from services.personal.supervisor import PersonalSupervisor

    launches, exits = [], []

    def fail_spawn(*_args, **_kwargs):
        raise OSError("synthetic executable launch failure")

    supervisor = PersonalSupervisor(
        record_launch=lambda *args: launches.append(args),
        record_exit=lambda *args: exits.append(args),
        read_deadline=lambda *args: dt.datetime.now(dt.UTC) + dt.timedelta(seconds=120),
        popen=fail_spawn,
    )
    with pytest.raises(OSError):
        supervisor.launch(uuid.uuid4(), uuid.uuid4(), 1)
    assert len(launches) == len(exits) == 1
    assert launches[0][4] is None
    assert exits[0][-1] is None
    assert not supervisor._children


def test_shutdown_singleton_is_not_recreated_by_health(monkeypatch):
    import services.personal.supervisor as module

    supervisor = module.PersonalSupervisor()
    monkeypatch.setattr(module, "_singleton", supervisor)
    supervisor.shutdown(timeout=0)
    assert module.get_supervisor() is supervisor
    assert module.get_supervisor().ready is False


def test_personal_paid_4a_assembly_retains_redis_cache_and_rate_limiter(monkeypatch):
    from packages.config.settings import Settings
    from services.llm.cache import RedisLLMPromptCache
    from services.llm.limiter import CompositeProviderLimiter
    from services.personal import paid_runtime
    from services.personal.spending import PaidRoute
    from tests.unit.test_personal_spending import model_route

    route = PaidRoute.from_mapping(model_route()["generation"], role="generation")
    redis = object()
    monkeypatch.setattr(paid_runtime, "_client", lambda *args: object())
    orchestrator = paid_runtime.build_paid_orchestrator(
        session=object(),
        settings=Settings(app_env="test", openai_api_key="synthetic-key"),
        route=route,
        ledger=object(),
        redis_client=redis,
    )
    assert isinstance(orchestrator._cache, RedisLLMPromptCache)
    assert orchestrator._cache.redis_client is redis
    assert isinstance(orchestrator._limiter, CompositeProviderLimiter)


@pytest.mark.parametrize("slow_part", ["headers", "body"])
def test_rss_total_timeout_cancels_headers_or_trickling_body(slow_part):
    import asyncio

    import httpx

    from services.personal.rss_runtime import DeadlineRSSProvider

    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self):
            closed.append(True)

    async def reply(request):
        if slow_part == "headers":
            await asyncio.sleep(0.2)
        return httpx.Response(200, request=request, stream=Stream())

    provider = DeadlineRSSProvider(timeout=0.025, transport=httpx.MockTransport(reply))
    began = time.monotonic()
    with pytest.raises(TimeoutError, match="RSS total deadline"):
        provider.fetch_capture("https://synthetic.test/rss")
    assert time.monotonic() - began < 0.2
    if slow_part == "body":
        assert closed


def test_legacy_soft_limit_and_grouped_cleanup_become_fatal_cancellation():
    from celery.exceptions import SoftTimeLimitExceeded

    from services.personal.deadlines import RuntimeCancelled, fatal_cancellations, propagate_fatal

    with fatal_cancellations((SoftTimeLimitExceeded,)):
        with pytest.raises(RuntimeCancelled):
            propagate_fatal(SoftTimeLimitExceeded())
    with pytest.raises(RuntimeCancelled):
        propagate_fatal(
            BaseExceptionGroup("cleanup", [RuntimeCancelled("deadline"), RuntimeError("close")])
        )


def test_fenced_audit_returns_readable_telemetry_with_default_session_expiration(monkeypatch):
    from sqlalchemy import String, create_engine, select
    from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

    from services.personal import audit_repository

    class AuditBase(DeclarativeBase):
        pass

    class AuditRow(AuditBase):
        __tablename__ = "synthetic_audit"
        id = mapped_column(String, primary_key=True)
        prompt_name = mapped_column(String)

    engine = create_engine("sqlite://")
    AuditBase.metadata.create_all(engine)
    factory = sessionmaker(engine)  # Production default: expire_on_commit=True.
    locks = []
    monkeypatch.setattr(audit_repository, "lock_owned_run", lambda *args: locks.append(args[1:]))
    subject = AuditRow(id="synthetic-run", prompt_name="personal_brief_synthetic")
    repository = audit_repository.PersonalAuditRepository(
        factory, uuid.UUID(int=1), uuid.UUID(int=2)
    )
    repository.save_llm_run(subject)
    assert subject.prompt_name == "personal_brief_synthetic"
    assert locks == [(uuid.UUID(int=1), uuid.UUID(int=2))]
    with factory() as session:
        assert session.scalar(select(AuditRow.prompt_name)) == subject.prompt_name
    engine.dispose()


def test_paid_response_after_deadline_reconciles_only_accounting_then_cancels():
    import httpx

    from services.personal.deadlines import (
        RuntimeBudget,
        RuntimeCancelled,
        bind_budget,
        check_budget,
    )
    from services.personal.paid_runtime import DurablePaidHTTPClient
    from services.personal.spending import PaidRoute
    from tests.unit.test_personal_spending import model_route

    mapping = model_route()["generation"]
    mapping["deadline_seconds"] = 1
    route = PaidRoute.from_mapping(mapping, role="generation")
    now = dt.datetime.now(dt.UTC)
    wall = [now]
    writes = []

    class Ledger:
        def reserve(self, *args, **kwargs):
            writes.append("reserve")
            return uuid.UUID(int=3)

        def dispatch(self, *args):
            writes.append("dispatch")

        def reconcile(self, *args, **kwargs):
            check_budget()  # Mirrors the child database acquisition/statement guard.
            writes.append("reconcile")

    class Delegate:
        def post(self, *args, **kwargs):
            wall[0] += dt.timedelta(seconds=2)
            return httpx.Response(
                200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            )

    client = DurablePaidHTTPClient(route=route, ledger=Ledger(), delegate=Delegate())
    with bind_budget(
        RuntimeBudget(
            now + dt.timedelta(seconds=1.5),
            now + dt.timedelta(seconds=3),
            wall_clock=lambda: wall[0],
        )
    ):
        with pytest.raises(RuntimeCancelled):
            client.post(
                "/v1/chat/completions",
                json={
                    "model": route.model,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "synthetic"}],
                },
            )
    assert writes == ["reserve", "dispatch", "reconcile"]


@pytest.mark.parametrize(
    "configured_mode,db_mode,configured_transport,delivery_transport,recorded_transport",
    [
        ("legacy", "personal", "celery", "celery", None),
        ("personal", "legacy", "celery", "celery", None),
        ("personal", "personal", "subprocess", "celery", None),
        ("personal", "personal", "celery", "celery", "subprocess"),
    ],
)
def test_delivery_configuration_mismatch_blocks_before_any_business_work(
    configured_mode, db_mode, configured_transport, delivery_transport, recorded_transport
):
    from types import SimpleNamespace

    from services.personal.runner import validate_delivery_configuration
    from services.personal.runs import WriterModeConflict

    runtime = SimpleNamespace(
        personal_processing_mode=configured_mode, personal_processing_transport=configured_transport
    )
    with pytest.raises(WriterModeConflict):
        validate_delivery_configuration(runtime, db_mode, delivery_transport, recorded_transport)


def test_redis_dependency_cannot_start_a_call_past_remaining_budget():
    from services.personal.deadlines import RuntimeBudget, RuntimeCancelled, bind_budget
    from services.personal.runner import BudgetRedisClient

    calls = []

    class Redis:
        def get(self, key):
            calls.append(key)

    now = dt.datetime.now(dt.UTC)
    client = BudgetRedisClient(Redis())
    with bind_budget(RuntimeBudget(now + dt.timedelta(seconds=1), now + dt.timedelta(seconds=2))):
        with pytest.raises(RuntimeCancelled):
            client.get("synthetic-prompt")
    assert calls == []


def test_supervisor_passes_original_handoff_deadline_before_child_bootstrap():
    from services.personal.supervisor import PersonalSupervisor

    deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=120)
    calls = []

    class Process:
        pid = 812345

        def poll(self):
            return 0

    def launch(argv, **kwargs):
        calls.append(kwargs["env"])
        return Process()

    supervisor = PersonalSupervisor(
        record_launch=lambda *args: None,
        record_exit=lambda *args: None,
        read_deadline=lambda *args: deadline,
        popen=launch,
    )
    supervisor.launch(uuid.UUID(int=1), uuid.UUID(int=2), 4)
    assert dt.datetime.fromisoformat(calls[0]["PERSONAL_HANDOFF_DEADLINE"]) == deadline
    supervisor.shutdown(timeout=0)


def test_registered_celery_delivery_rotates_running_ownership_token(monkeypatch):
    from services.personal import runner
    from workers.personal_tasks import run_personal_daily_task

    deliveries = []

    def execute(run_id, token, **kwargs):
        deliveries.append((run_id, token, kwargs))
        return {"status": "succeeded"}

    monkeypatch.setattr(runner, "execute_personal_task", execute)
    run_id, token = uuid.uuid4(), uuid.uuid4()
    assert run_personal_daily_task.run(str(run_id), str(token)) == {"status": "succeeded"}
    assert deliveries[0][:2] == (run_id, token)
    assert deliveries[0][2]["delivery_transport"] == "celery"
    assert deliveries[0][2]["rotate_token"] is True
