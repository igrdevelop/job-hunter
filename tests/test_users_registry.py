"""Multi-user Phase B3 item 2 — user registry (hunter/users.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from hunter import users
from hunter.db import get_db

OWNER = "owner-uid"
OTHER = "other-uid"


def _link(db, chat_id: int, user_id: str) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
        )


def _set_setting(db, user_id: str, key: str, value: str) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, key, value, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, key, value, datetime.now(timezone.utc).isoformat()),
        )


def _make_candidate_yaml(users_root, user_id: str) -> None:
    cdir = users_root / user_id / "candidate"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "candidate.yaml").write_text("identity:\n  full_name: Test\n")


def _activate_env(monkeypatch, tmp_path, tracker_db, owner: str = OWNER):
    users_root = tmp_path / "users"
    monkeypatch.setattr("hunter.config.USERS_ROOT", users_root)
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", owner)
    monkeypatch.setattr("hunter.config.TRACKER_DB_PATH", tracker_db)
    return users_root


# ── resolve_user / resolve_chat ───────────────────────────────────────────────


def test_resolve_user_linked(tracker_db):
    _link(tracker_db, 42, OWNER)
    assert users.resolve_user(42) == OWNER


def test_resolve_user_unbound(tracker_db):
    assert users.resolve_user(42) is None


def test_resolve_chat_roundtrip(tracker_db):
    _link(tracker_db, 42, OWNER)
    assert users.resolve_chat(OWNER) == 42
    assert users.resolve_chat("nobody") is None


# ── user_paths ────────────────────────────────────────────────────────────────


def test_user_paths_layout(monkeypatch, tmp_path):
    monkeypatch.setattr("hunter.config.USERS_ROOT", tmp_path / "users")
    p = users.user_paths("abc")
    assert p.root == tmp_path / "users" / "abc"
    assert p.candidate_yaml == p.root / "candidate" / "candidate.yaml"
    assert p.applications_dir == p.root / "Applications"
    assert p.templates_dir == p.root / "templates"


# ── list_active_users ─────────────────────────────────────────────────────────


def test_owner_with_candidate_yaml_is_active(monkeypatch, tmp_path, tracker_db):
    users_root = _activate_env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, 1, OWNER)
    _make_candidate_yaml(users_root, OWNER)
    assert users.list_active_users() == [OWNER]


def test_non_owner_excluded_in_b3(monkeypatch, tmp_path, tracker_db):
    """B3 scope decision: hunting is owner-only until B3.5."""
    users_root = _activate_env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, 1, OWNER)
    _link(tracker_db, 2, OTHER)
    _make_candidate_yaml(users_root, OWNER)
    _make_candidate_yaml(users_root, OTHER)
    _set_setting(tracker_db, OTHER, "hunting_enabled", "true")
    assert users.list_active_users() == [OWNER]


def test_owner_without_candidate_yaml_inactive(monkeypatch, tmp_path, tracker_db):
    _activate_env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, 1, OWNER)
    assert users.list_active_users() == []


def test_owner_hunting_disabled_inactive(monkeypatch, tmp_path, tracker_db):
    users_root = _activate_env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, 1, OWNER)
    _make_candidate_yaml(users_root, OWNER)
    _set_setting(tracker_db, OWNER, "hunting_enabled", "false")
    assert users.list_active_users() == []


def test_no_default_user_id_skips_owner_filter(monkeypatch, tmp_path, tracker_db):
    """Dev setup without DEFAULT_USER_ID: any linked user with a yaml counts."""
    users_root = _activate_env(monkeypatch, tmp_path, tracker_db, owner="")
    _link(tracker_db, 2, OTHER)
    _make_candidate_yaml(users_root, OTHER)
    assert users.list_active_users() == [OTHER]


def test_unlinked_user_never_active(monkeypatch, tmp_path, tracker_db):
    users_root = _activate_env(monkeypatch, tmp_path, tracker_db)
    _make_candidate_yaml(users_root, OWNER)  # yaml exists but no telegram link
    assert users.list_active_users() == []
