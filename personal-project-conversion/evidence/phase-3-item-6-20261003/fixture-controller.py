#!/usr/bin/env python3
"""Identity-checked, synthetic database fixtures for item 6 browser observations.

This is evidence tooling, not application code. No feed/model network I/O is permitted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ["APP_ENV"] = "test"
os.environ["PERSONAL_PAID_RUNTIME_ENABLED"] = "false"
for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ[key] = ""

# Fail closed if an unexpected Python transport is introduced. PostgreSQL remains loopback.
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex

def _guard_address(address):
    if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise AssertionError("item 6 fixtures prohibit non-loopback network dispatch")

def _connect(sock, address):
    _guard_address(address)
    return _real_connect(sock, address)

def _connect_ex(sock, address):
    _guard_address(address)
    return _real_connect_ex(sock, address)

socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db.models import PersonalCapture, PersonalProfileRevision, PersonalRun, Source
from db.models.personal_spending import PersonalPaidRequest
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from scripts.personal_phase2_offline_db import MARKER_TABLE, _load_manifest
from services.personal.coordinator import PersonalCoordinatorError, run_personal_daily
from services.personal.offline_fixture import OfflinePersonalFixture
from services.personal.reader import collection_status, pending_backlog, raw_articles
from services.personal.repository import PersonalRepository
from services.personal.runs import create_daily_run
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.spending import spending_summary, validate_model_route
from services.personal.workspace import get_workspace

UTC = dt.UTC
FIXTURE_ID = "phase-3-item-6-browser-v1"
A_URL = "https://offline.personal.test/item6/technology.xml"
B_URL = "https://offline.personal.test/item6/policy.xml"
SCOPE = ROOT / "personal-project-conversion/evidence/phase-3-item-6-20261003"

class StepClock:
    def __init__(self, stamp):
        self.stamp = stamp
    def __call__(self):
        value = self.stamp
        self.stamp += dt.timedelta(seconds=1)
        return value

class NoPaidProvider:
    model_name = "disabled"
    model_version = "raw"
    def embed(self, _texts):
        raise AssertionError("raw browser fixture attempted optional processing")

def no_report(*_args):
    raise AssertionError("raw browser fixture attempted report generation")

def synthetic_live_route():
    base = {
        "provider": "openai", "model_version": "fixture-v1",
        "price_revision": "synthetic-item6-prices-not-live",
        "price_source_url": "https://openai.com/api/pricing/",
        "input_usd_per_million_tokens": "1000",
        "max_input_tokens": 1000, "deadline_seconds": 10,
    }
    return validate_model_route({"mode": "live", "generation": {
        **base, "model": "synthetic-item6-generation", "output_usd_per_million_tokens": "1000",
        "max_output_tokens": 1000,
    }, "embedding": {
        **base, "model": "synthetic-item6-embedding", "output_usd_per_million_tokens": "0",
        "max_output_tokens": 0,
    }})

def identity_engine(path):
    manifest = _load_manifest(path)
    database = manifest["database"]
    if database.get("owned") is not True or not database.get("oid") or not database.get("owner"):
        raise RuntimeError("owned database OID/role identity must be retained in manifest")
    url = make_url(database["url"])
    if (url.host not in {"127.0.0.1", "localhost", "::1"} or url.port in {None, 5432}
            or url.database != manifest["database_name"]):
        raise RuntimeError("fixture database must be exact owned loopback non-default database")
    engine = create_engine(database["url"])
    try:
        with engine.connect() as connection:
            actual = dict(connection.execute(text(
                "SELECT current_database() AS name, d.oid::bigint AS oid, "
                "pg_get_userbyid(d.datdba) AS owner FROM pg_database d "
                "WHERE d.datname=current_database()"
            )).mappings().one())
            token = connection.scalar(text(f"SELECT owner_token FROM {MARKER_TABLE}"))
        if actual != {"name": manifest["database_name"], "oid": int(database["oid"]), "owner": database["owner"]}:
            raise RuntimeError("current database OID/name/owner differs from owned manifest")
        if token != manifest["owner_token"]:
            raise RuntimeError("database marker token differs from owned manifest")
    except Exception:
        engine.dispose()
        raise
    identity = {**actual, "loopback_host": url.host, "port": url.port,
                "marker_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return manifest, engine, identity

def _workspace(session):
    workspace, _ = get_workspace(session)
    if workspace is None:
        raise RuntimeError("launcher workspace missing")
    return workspace

def _sources(session):
    sources = list(session.scalars(select(Source).where(Source.feed_url.in_([A_URL, B_URL])).order_by(Source.feed_url)))
    if len(sources) != 2:
        raise RuntimeError("initial fixture must create both item 6 sources")
    return sources

def revise(engine, *, mode="raw", enabled=False, monthly=None, route=None, limit=26, selected=None, timezone=None):
    with Session(engine) as session:
        workspace = _workspace(session)
        sources = _sources(session)
        request = PersonalSettingsUpdate(
            selected_source_ids=selected if selected is not None else [source.id for source in sources],
            include_phrases=[], exclude_phrases=[], execution_profile=mode,
            daily_article_limit=100, run_article_limit=limit, enrichment_article_limit=limit,
            max_enabled_feeds=10, ai_enabled=enabled, monthly_allowance_usd=monthly,
            run_allowance_usd=None, model_route=route or {}, timezone=timezone or workspace.timezone,
        )
        profile = update_settings(session, workspace, request)
        session.commit()
        return str(profile.id)

def new_run(engine, stamp):
    with Session(engine) as session:
        workspace = _workspace(session)
        decision = create_daily_run(session, workspace, now=stamp)
        if not decision.created:
            raise RuntimeError(f"fixture date already has a run: {decision.run.id}")
        run_id, token = decision.run.id, decision.run.ownership_token
        session.commit()
    return run_id, token

def execute(engine, stamp, provider, embedding=None, orchestrator=None):
    run_id, token = new_run(engine, stamp)
    try:
        result = run_personal_daily(
            run_id, token, session_factory=lambda: Session(engine, autoflush=False),
            rss_provider_factory=provider, embedding_provider=embedding or NoPaidProvider(),
            orchestrator_factory=orchestrator or no_report, now=stamp,
            clock=StepClock(stamp + dt.timedelta(seconds=10)),
        )
        return {"run_id": str(run_id), "raised": None,
                "captured": result.articles_captured, "admitted": result.articles_admitted,
                "published": bool(result.report and result.report.published)}
    except PersonalCoordinatorError as exc:
        return {"run_id": str(run_id), "raised": type(exc).__name__, "message": str(exc)}

def item(index, source, stamp):
    title = f"Synthetic item 6 {source}: retained raw article {index:02d}"
    summary = ("Synthetic retained snippet. The agency reported a browser fixture observation. "
               if index else "Unknown publication time fixture. Retained summary: " + "boundary-text " * 200)
    return RSSItem(guid=f"item6-{source}-{index}", title=title,
                   summary=summary, url=f"https://offline.personal.test/item6/{source}/{index}?utm_source=item6",
                   published_at=None if index == 0 else stamp - dt.timedelta(hours=index),
                   source=source, provider_name="offline-personal-fixture",
                   source_refs=(f"fixture:{FIXTURE_ID}:{source}",), evidence_refs=(f"synthetic:item6:{source}:{index}",))

def initial(engine, manifest, now):
    with Session(engine) as session:
        if session.scalar(select(func.count()).select_from(PersonalRun)):
            raise RuntimeError("initial scenario requires empty owned run table; will not overwrite retained data")
        old_source = session.get(Source, uuid.UUID(manifest["source_id"]))
        old_source.name = "Synthetic item 6 technology feed"
        old_source.feed_url = A_URL
        b = Source(name="Synthetic item 6 policy feed", feed_url=B_URL, active=True)
        session.add(b)
        session.flush()
        a_id, b_id = old_source.id, b.id
        session.commit()
    brief_stamp = now - dt.timedelta(days=3)
    published_item = RSSItem(
        guid="item6-grouped-published", title="Synthetic: Agency approves browser verification project",
        summary="The agency confirmed the browser verification project was approved.",
        url="https://offline.personal.test/item6/grouped-project?utm_source=item6",
        published_at=brief_stamp - dt.timedelta(hours=1), provider_name="offline-personal-fixture",
        source_refs=(f"fixture:{FIXTURE_ID}:grouped",), evidence_refs=("synthetic:item6:grouped",),
    )
    fixture = OfflinePersonalFixture(FIXTURE_ID, "Synthetic item 6 browser only fixture", {
        A_URL: (published_item,), B_URL: (),
    }, "Synthetic offline brief: the agency confirmed this verification project was approved.",
       "Synthetic grouped story: the agency confirmed approval of the verification project.")
    revise(engine, mode="assisted", route=fixture.route, selected=[a_id], limit=1, timezone="America/Los_Angeles")
    brief = execute(engine, brief_stamp, fixture.rss_provider, fixture.embedding_provider(), fixture.orchestrator)
    if not brief["published"]:
        raise RuntimeError("offline fixture failed to publish retained brief")
    with Session(engine) as session:
        run = session.get(PersonalRun, uuid.UUID(brief["run_id"]))
        if len(run.event_ids) != 1:
            raise RuntimeError("fixture expected exactly one grouped story")
        PersonalRepository(session, _workspace(session)).save(run.event_ids[0])
        brief["event_id"], brief["article_id"], brief["report_id"] = str(run.event_ids[0]), str(run.admitted_article_ids[0]), str(run.report_id)
        session.commit()
    raw_stamp = now - dt.timedelta(days=2)
    rows_a = [item(i, "technology", raw_stamp) for i in range(18)]
    rows_b = [item(i, "policy", raw_stamp) for i in range(18)]
    # Exactly 501 fixture records: first 18 unique, then duplicated URL identities.
    # Production bound_rss_items observes the sentinel, processes 500, and retains 18.
    over_cap_a = rows_a + [rows_a[0]] * (501 - len(rows_a))
    revise(engine, mode="raw", route=fixture.route, limit=26)
    raw = execute(engine, raw_stamp, lambda source: FakeRSSProvider(over_cap_a if source.feed_url == A_URL else rows_b))
    with Session(engine) as session:
        if len(raw_articles(session, _workspace(session), limit=100)["items"]) != 26:
            raise RuntimeError("initial fixture did not yield 26 ungrouped readable records")
        if pending_backlog(session, _workspace(session))["total"] != 10:
            raise RuntimeError("initial fixture did not yield 10 retained pending records")
    return {"brief": brief, "raw": raw,
            "input_record_counts": {A_URL: 501, B_URL: 18},
            "unique_raw_candidates": 36, "source_ids": [str(a_id), str(b_id)],
            "expected_collection": {"observed_candidates": 37, "pending_candidates": 10,
                                    "admitted_articles": 27, "grouped_articles": 1, "completed_enrichment": 1},
            "expected_default_raw_total": 26, "expected_initial_backlog_eligible": 10,
            "boundary": {"technology_observed": 501, "technology_processed": 500,
                         "technology_retained": 18, "technology_total_entries_known": False,
                         "field_truncation": "summary", "retained_summary_chars": 2000},
            "provenance": "Actual coordinator + FakeRSSProvider + OfflinePersonalFixture; no real feeds or providers"}

def failure(engine, now, kind):
    if kind == "capture_failure":
        revise(engine, mode="raw", limit=1)
        class FailedFeed:
            def fetch(self, _feed_url):
                raise RuntimeError("synthetic-item6-capture-failure")
        return execute(engine, now - dt.timedelta(days=1), lambda _source: FailedFeed())
    fixture = OfflinePersonalFixture(FIXTURE_ID, "Synthetic provider failure fixture", {A_URL: (), B_URL: ()}, "unused", "unused")
    revise(engine, mode="assisted", route=fixture.route, limit=10)
    class FailedEmbedding:
        model_name = "synthetic-item6-failed-embedding"
        model_version = "fixture-v1"
        def embed(self, _texts):
            raise RuntimeError("synthetic-item6-provider-failure-before-any-transport")
    # Pending records are admitted first. Empty deterministic feeds add no new candidates.
    return execute(engine, now, lambda _source: FakeRSSProvider([]), FailedEmbedding(), no_report)

def spending(engine, now):
    route = synthetic_live_route()
    revise(engine, mode="assisted", enabled=True, monthly="1.00", route=route, limit=26)
    month = now.date().replace(day=1)
    prior = (month - dt.timedelta(days=1)).replace(day=1)
    with Session(engine) as session:
        workspace = _workspace(session)
        run = session.scalar(select(PersonalRun).order_by(PersonalRun.local_date.desc()).limit(1))
        if session.scalar(select(func.count()).select_from(PersonalPaidRequest)):
            raise RuntimeError("spending scenario is one-shot; retained requests will not be overwritten")
        rows = [
            ("current_finalized", month, "reconciled", "0.20", "0.20", 100, 100),
            ("current_reserved", month, "reserved", "0.10", None, 99, 1),
            ("current_uncertain", month, "uncertain", "0.05", None, 49, 1),
            ("older_uncertain", prior, "uncertain", "0.15", None, 149, 1),
            ("older_finalized_excluded", prior, "reconciled", "0.90", "0.90", 450, 450),
        ]
        retained = []
        for label, period, status, reservation, actual, input_bound, output_bound in rows:
            stamp = dt.datetime.combine(period, dt.time(12), UTC)
            row = PersonalPaidRequest(
                workspace_id=workspace.id, run_id=run.id, attempt=run.attempt,
                accounting_month=period, status=status, role="generation", route=route["generation"],
                input_token_bound=input_bound, output_token_bound=output_bound,
                reserved_usd=Decimal(reservation), actual_usd=Decimal(actual) if actual else None,
                input_tokens=input_bound if actual else None, output_tokens=output_bound if actual else None,
                reserved_at=stamp, dispatch_attempt_at=stamp if status != "reserved" else None,
                reconciled_at=stamp if actual else None,
                evidence={"kind": "synthetic_browser_fixture", "fixture_id": FIXTURE_ID,
                          "label": label, "real_provider_dispatches": 0},
            )
            session.add(row)
            session.flush()
            retained.append({"id": str(row.id), "label": label, "status": status,
                             "accounting_period": period.strftime("%Y-%m"), "reserved_usd": reservation,
                             "actual_usd": actual})
        session.commit()
        summary = spending_summary(session, workspace.id, now=now)
        for key, expected in {"finalized_usd": "0.20", "reserved_usd": "0.15",
                              "unresolved_usd": "0.30", "remaining_usd": "0.65"}.items():
            if Decimal(summary[key]) != Decimal(expected):
                raise RuntimeError(f"synthetic fixture summary differs for {key}")
        if summary["status"] != "ready":
            raise RuntimeError(f"unexpected spending fixture status: {summary['status']}")
        return {"requests": retained, "summary": summary,
                "provenance": "Direct, explicit synthetic ledger records for browser rendering only; no provider transport or ledger execution claim",
                "prices": "Invented synthetic amounts, not live prices; official URL validates route shape only and is never fetched"}

def snapshot(engine, now):
    with Session(engine) as session:
        workspace = _workspace(session)
        sources = [{"id": str(s.id), "name": s.name, "feed_url": s.feed_url, "active": s.active}
                   for s in session.scalars(select(Source).order_by(Source.name))]
        runs = [{"id": str(r.id), "local_date": r.local_date, "state": r.state,
                 "profile_revision_id": str(r.profile_revision_id), "coverage": r.coverage,
                 "stage_results": r.stage_results, "result": r.result, "error": r.error,
                 "admitted_article_ids": [str(i) for i in r.admitted_article_ids],
                 "enrichment_article_ids": [str(i) for i in r.enrichment_article_ids],
                 "event_ids": [str(i) for i in r.event_ids]}
                for r in session.scalars(select(PersonalRun).order_by(PersonalRun.local_date))]
        return {"workspace_id": str(workspace.id), "timezone": workspace.timezone, "sources": sources,
                "collection": collection_status(session, workspace),
                "backlog": pending_backlog(session, workspace, limit=100),
                "raw_page_1": raw_articles(session, workspace, limit=20),
                "raw_page_2": raw_articles(session, workspace, limit=20, offset=20),
                "browser_raw_page_1": raw_articles(session, workspace, limit=12),
                "browser_raw_page_2": raw_articles(session, workspace, limit=12, offset=12),
                "browser_raw_page_3": raw_articles(session, workspace, limit=12, offset=24),
                "all_raw_including_grouped": raw_articles(session, workspace, ungrouped=False, limit=100),
                "spending": spending_summary(session, workspace.id, now=now), "runs": runs}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scenario", required=True, choices=("initial", "capture_failure", "provider_failure", "spending", "summary", "reset_settings", "missing_config", "gate_off", "allowance"))
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != SCOPE or output.suffix != ".json":
        raise RuntimeError("retained output must be a JSON file directly in item 6 scoped evidence")
    if output.exists():
        raise RuntimeError("retained output already exists; choose a new path")
    manifest, engine, identity = identity_engine(args.manifest.resolve())
    now = dt.datetime.now(UTC).replace(microsecond=0)
    try:
        actions = {}
        if args.scenario == "initial":
            actions = initial(engine, manifest, now)
        elif args.scenario in {"capture_failure", "provider_failure"}:
            actions = failure(engine, now, args.scenario)
        elif args.scenario == "spending":
            actions = spending(engine, now)
        elif args.scenario in {"reset_settings", "missing_config", "gate_off", "allowance"}:
            actions = {"profile_revision_id": revise(engine,
                mode="raw" if args.scenario == "reset_settings" else "assisted",
                enabled=args.scenario != "reset_settings",
                monthly="0.00" if args.scenario == "allowance" else "1.00" if args.scenario == "gate_off" else None,
                route={} if args.scenario == "missing_config" else synthetic_live_route())}
        payload = {"schema": "personal-phase3-item6-browser-fixture.v1", "scenario": args.scenario,
                   "dataset": "synthetic", "executed_at": now, "database_identity": identity,
                   "runtime": {"APP_ENV": "test", "real_provider_credentials": "cleared",
                               "PERSONAL_PAID_RUNTIME_ENABLED": False, "external_dispatches": 0},
                   "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "actions": actions, "snapshot": snapshot(engine, now)}
        output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        print(json.dumps({"scenario": args.scenario, "output": str(output),
                          "dataset": "synthetic", "external_dispatches": 0}))
    finally:
        engine.dispose()

if __name__ == "__main__":
    main()
