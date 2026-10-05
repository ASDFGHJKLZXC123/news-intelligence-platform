"""Supported episode maintenance refuses incompatible writers before provider setup."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from db.seed import seed
from services import writer_mode


def test_episode_maintenance_checks_mode_before_provider(monkeypatch):
    session = Mock()
    monkeypatch.setattr(seed, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        seed, "build_provider", lambda: pytest.fail("forbidden mode opened provider")
    )

    def reject(_session, **_kwargs):
        raise writer_mode.LegacyWriterModeConflict("personal writer active")

    monkeypatch.setattr(writer_mode, "require_legacy_writer_mode", reject)
    with pytest.raises(writer_mode.LegacyWriterModeConflict):
        seed.seed_episodes(allow_unreviewed=True)
    session.rollback.assert_called_once()
    session.close.assert_called_once()
