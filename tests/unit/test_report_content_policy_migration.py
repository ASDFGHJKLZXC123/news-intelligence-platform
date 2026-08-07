"""Convergence checks for migration 0019's live-metadata compatibility."""

from __future__ import annotations

import importlib
from typing import Any

import pytest


def test_upgrade_is_a_noop_when_live_metadata_already_created_policy_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("db.migrations.versions.0019_report_content_policy")
    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(migration, "_columns", lambda: {"content_policy"})
    monkeypatch.setattr(
        migration,
        "_checks",
        lambda: {"ck_reports_content_policy"},
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *args: calls.append(("add_column", args)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args: calls.append(("create_check", args)),
    )

    migration.upgrade()

    assert migration.revision == "0019"
    assert migration.down_revision == "0018"
    assert calls == []


def test_upgrade_adds_each_missing_policy_object_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("db.migrations.versions.0019_report_content_policy")
    calls: list[str] = []
    monkeypatch.setattr(migration, "_columns", lambda: set())
    monkeypatch.setattr(migration, "_checks", lambda: set())
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *_args: calls.append("column"),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *_args: calls.append("check"),
    )

    migration.upgrade()

    assert calls == ["column", "check"]
