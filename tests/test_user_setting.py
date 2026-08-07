"""Tests for hunter.config.user_setting (Phase B2)."""

from pathlib import Path

import pytest

from hunter.db import get_db, init_db


@pytest.fixture()
def setting_db(tmp_path: Path, monkeypatch):
    """Isolated DB with user_settings table; patch TRACKER_DB_PATH."""
    db = tmp_path / "tracker.db"
    init_db(db, xlsx_path=tmp_path / "no.xlsx")
    # Patch the module-level constant the function reads
    import hunter.config as cfg

    monkeypatch.setattr(cfg, "TRACKER_DB_PATH", db)
    return db


def test_user_setting_returns_stored_value(setting_db):
    from hunter.config import user_setting

    with get_db(setting_db) as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value) VALUES('u1','AUTO_APPLY','true')"
        )
    assert user_setting("u1", "AUTO_APPLY", "false") == "true"


def test_user_setting_returns_default_when_missing(setting_db):
    from hunter.config import user_setting

    assert user_setting("u1", "NO_SUCH_KEY", "fallback") == "fallback"


def test_user_setting_scoped_by_user(setting_db):
    from hunter.config import user_setting

    with get_db(setting_db) as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value) VALUES('u1','TRACKS','angular')"
        )
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value) VALUES('u2','TRACKS','react')"
        )
    assert user_setting("u1", "TRACKS", "") == "angular"
    assert user_setting("u2", "TRACKS", "") == "react"
    assert user_setting("u3", "TRACKS", "both") == "both"


def test_user_setting_returns_default_on_db_error(monkeypatch):
    """Never raises — always returns the default on any DB problem."""
    import hunter.config as cfg

    monkeypatch.setattr(cfg, "TRACKER_DB_PATH", Path("/nonexistent/tracker.db"))
    from hunter.config import user_setting

    assert user_setting("u1", "key", "safe") == "safe"
