"""Multi-user Phase B3 item 7 — tracks/dual settings move to user_settings."""

from __future__ import annotations

import sqlite3

from hunter import config, llm_profiles
from hunter.db import get_db

OWNER = "owner-uid"
OTHER = "other-uid"


def _env(monkeypatch, tracker_db, uid: str = OWNER):
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", uid)
    monkeypatch.setattr("hunter.config.TRACKER_DB_PATH", tracker_db)
    monkeypatch.delenv("JOB_HUNTER_USER_ID", raising=False)


def _legacy_set(db, key: str, value: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))


def _user_rows(db) -> dict:
    with get_db(db) as conn:
        return {
            (r["user_id"], r["key"]): r["value"]
            for r in conn.execute("SELECT user_id, key, value FROM user_settings")
        }


# ── current_user_id ───────────────────────────────────────────────────────────


def test_current_user_id_env_wins(monkeypatch):
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", OWNER)
    monkeypatch.setenv("JOB_HUNTER_USER_ID", OTHER)
    assert config.current_user_id() == OTHER
    monkeypatch.delenv("JOB_HUNTER_USER_ID")
    assert config.current_user_id() == OWNER


# ── tracks ────────────────────────────────────────────────────────────────────


def test_set_active_tracks_writes_user_settings(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    config.set_active_tracks({"angular", "react"})
    assert _user_rows(tracker_db)[(OWNER, "tracks_enabled")] == "angular,react"
    assert config.active_tracks() == frozenset({"angular", "react"})


def test_active_tracks_per_user_isolation(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    config.set_user_setting(OWNER, "tracks_enabled", "angular")
    config.set_user_setting(OTHER, "tracks_enabled", "react")
    assert config.active_tracks() == frozenset({"angular"})
    monkeypatch.setenv("JOB_HUNTER_USER_ID", OTHER)
    assert config.active_tracks() == frozenset({"react"})


def test_active_tracks_legacy_config_row_fallback(tracker_db, monkeypatch):
    """Pre-B3 data written by /tracks into the global config table still answers."""
    _env(monkeypatch, tracker_db)
    _legacy_set(tracker_db, "tracks_enabled", "angular,react")
    assert config.active_tracks() == frozenset({"angular", "react"})


def test_set_active_tracks_no_user_id_uses_legacy_row(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db, uid="")
    config.set_active_tracks({"react"})
    assert _user_rows(tracker_db) == {}
    assert config.active_tracks() == frozenset({"react"})


# ── dual-apply keys ───────────────────────────────────────────────────────────


def test_set_dual_writes_user_settings(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    llm_profiles.set_dual(True)
    assert _user_rows(tracker_db)[(OWNER, "dual_apply_enabled")] == "1"
    assert llm_profiles.dual_enabled() is True
    llm_profiles.set_dual(False)
    assert llm_profiles.dual_enabled() is False


def test_dual_enabled_per_user_isolation(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    config.set_user_setting(OWNER, "dual_apply_enabled", "1")
    assert llm_profiles.dual_enabled() is True
    monkeypatch.setenv("JOB_HUNTER_USER_ID", OTHER)
    assert llm_profiles.dual_enabled() is False


def test_dual_legacy_config_row_fallback(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    _legacy_set(tracker_db, "dual_apply_enabled", "1")
    assert llm_profiles.dual_enabled() is True


def test_shadow_profile_per_user(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config.set_user_setting(OWNER, "dual_shadow_profile", "deepseek-v4-pro")
    prof = llm_profiles.shadow_profile()
    assert prof is not None and prof.name == "deepseek-v4-pro"
    monkeypatch.setenv("JOB_HUNTER_USER_ID", OTHER)
    prof = llm_profiles.shadow_profile()
    assert prof is not None and prof.name == "deepseek-v3"  # default, not owner's


# ── llm_outage_until stays global ────────────────────────────────────────────


def test_llm_outage_key_stays_in_global_config(tracker_db, monkeypatch):
    _env(monkeypatch, tracker_db)
    from hunter import llm_outage

    llm_outage.arm_pause(1)
    with sqlite3.connect(tracker_db) as conn:
        row = conn.execute("SELECT value FROM config WHERE key = 'llm_outage_until'").fetchone()
    assert row is not None
    assert "llm_outage_until" not in {k for (_, k) in _user_rows(tracker_db)}
