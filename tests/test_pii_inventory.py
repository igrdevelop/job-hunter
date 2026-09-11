"""Tests for tools/pii_inventory.py (docs/improvement-2026-09/07-COMPLIANCE_PLAN.md M0)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import uuid
from pathlib import Path

from hunter.db import get_db

TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_module():
    spec = importlib.util.spec_from_file_location("pii_inventory", TOOLS_DIR / "pii_inventory.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pii_inventory"] = module
    spec.loader.exec_module(module)
    return module


pii = _load_module()


# ── discover_user_id_tables / count_rows_by_user_id ────────────────────────


def test_discover_user_id_tables_finds_known_tables(tracker_db):
    with get_db(tracker_db) as conn:
        tables = pii.discover_user_id_tables(conn)
    for expected in ("applications", "user_settings", "telegram_links", "profile_jobs"):
        assert expected in tables


def test_count_rows_by_user_id_scoped_to_uid(tracker_db):
    uid = uuid.uuid4().hex
    other_uid = uuid.uuid4().hex
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, url, url_norm, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                "2026-06-01",
                "Co",
                "Dev",
                "https://x.com/a",
                "https://x.com/a",
                uid,
            ),
        )
        conn.execute(
            "INSERT INTO applications (id, date, company, title, url, url_norm, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                "2026-06-01",
                "Co",
                "Dev",
                "https://x.com/b",
                "https://x.com/b",
                other_uid,
            ),
        )
        n = pii.count_rows_by_user_id(conn, "applications", uid)
    assert n == 1


def test_count_rows_by_user_id_rejects_unsafe_identifier(tracker_db):
    with get_db(tracker_db) as conn:
        assert pii.count_rows_by_user_id(conn, "applications; DROP TABLE applications", "x") == 0


def test_tracker_inventory_seeded_uid_across_tables(tracker_db):
    uid = uuid.uuid4().hex
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, url, url_norm, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                "2026-06-01",
                "Co",
                "Dev",
                "https://x.com/a",
                "https://x.com/a",
                uid,
            ),
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?,?,?)", (uid, "k", "v")
        )
        conn.execute(
            "INSERT INTO telegram_links (chat_id, user_id, linked_at) VALUES (?,?,?)",
            (12345, uid, "2026-06-01T00:00:00Z"),
        )

    counts = pii.tracker_inventory(tracker_db, uid)
    assert counts["applications"] == 1
    assert counts["user_settings"] == 1
    assert counts["telegram_links"] == 1
    assert counts.get("profile_jobs", 0) == 0


def test_tracker_inventory_missing_db_returns_empty(tmp_path):
    assert pii.tracker_inventory(tmp_path / "nope.db", "uid") == {}


# ── app_sqlite_inventory (heuristic, generic schema) ────────────────────────


def _make_fake_app_sqlite(path: Path, uid: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT)")
    conn.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY, userId TEXT, data TEXT)")
    conn.execute("CREATE TABLE email_verification_tokens (token TEXT PRIMARY KEY, userId TEXT)")
    conn.execute("INSERT INTO users (id, email) VALUES (?, ?)", (uid, "a@b.com"))
    conn.execute("INSERT INTO users (id, email) VALUES (?, ?)", ("someone-else", "c@d.com"))
    conn.execute("INSERT INTO profiles (id, userId, data) VALUES (?, ?, ?)", ("p1", uid, "{}"))
    conn.execute(
        "INSERT INTO email_verification_tokens (token, userId) VALUES (?, ?)", ("tok1", uid)
    )
    conn.commit()
    conn.close()


def test_app_sqlite_inventory_finds_camel_and_snake_case_columns(tmp_path):
    uid = uuid.uuid4().hex
    db_path = tmp_path / "app.sqlite"
    _make_fake_app_sqlite(db_path, uid)

    hits = pii.app_sqlite_inventory(db_path, uid)
    by_table = {(h["table"], h["column"]): h["count"] for h in hits}
    assert by_table[("users", "id")] == 1
    assert by_table[("profiles", "userId")] == 1
    assert by_table[("email_verification_tokens", "userId")] == 1


def test_app_sqlite_inventory_missing_file_returns_empty(tmp_path):
    assert pii.app_sqlite_inventory(tmp_path / "nope.sqlite", "uid") == []


# ── dir_stats / grep_file_count ─────────────────────────────────────────────


def test_dir_stats_counts_files_and_bytes(tmp_path):
    root = tmp_path / "users" / "abc"
    (root / "candidate").mkdir(parents=True)
    (root / "candidate" / "candidate.yaml").write_text("x" * 10, encoding="utf-8")
    (root / "Applications").mkdir()
    (root / "Applications" / "content.json").write_text("y" * 20, encoding="utf-8")

    stats = pii.dir_stats(root)
    assert stats["files"] == 2
    assert stats["bytes"] == 30


def test_dir_stats_missing_dir():
    assert pii.dir_stats(Path("/definitely/not/a/real/path/xyz")) == {"files": 0, "bytes": 0}


def test_grep_file_count_finds_mentions(tmp_path):
    uid = "user-1234-uuid"
    (tmp_path / "a.jsonl").write_text(
        f'{{"user_id": "{uid}", "outcome": "fail"}}\n', encoding="utf-8"
    )
    (tmp_path / "b.jsonl").write_text('{"user_id": "someone-else"}\n', encoding="utf-8")

    assert pii.grep_file_count(tmp_path, uid) == 1


def test_grep_file_count_missing_dir():
    assert pii.grep_file_count(Path("/definitely/not/a/real/path/xyz"), "uid") == 0


def test_grep_file_count_empty_needle():
    assert pii.grep_file_count(Path("."), "") == 0


# ── build_report / decision rule ────────────────────────────────────────────


def test_build_report_finds_extra_places(tracker_db, tmp_path):
    uid = uuid.uuid4().hex
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, url, url_norm, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                "2026-06-01",
                "Co",
                "Dev",
                "https://x.com/a",
                "https://x.com/a",
                uid,
            ),
        )

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "apply_failures.jsonl").write_text(f'{{"user_id": "{uid}"}}\n', encoding="utf-8")

    report = pii.build_report(
        uid,
        db_path=tracker_db,
        app_sqlite_path=None,
        users_root=tmp_path / "users",
        logs_dir=logs_dir,
        backups_dir=tmp_path / "backups",
    )

    extra = report["extra_places_beyond_admin_delete_user"]
    assert any("applications" in p for p in extra)
    assert any("logs/" in p for p in extra)


def test_build_report_profile_jobs_excluded_from_extra_places(tracker_db, tmp_path):
    uid = uuid.uuid4().hex
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO profile_jobs (id, user_id, kind, created_at) VALUES (?,?,?,?)",
            (uuid.uuid4().hex[:8], uid, "render", "2026-06-01T00:00:00Z"),
        )

    report = pii.build_report(
        uid,
        db_path=tracker_db,
        app_sqlite_path=None,
        users_root=tmp_path / "users",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
    )
    extra = report["extra_places_beyond_admin_delete_user"]
    assert not any("profile_jobs" in p for p in extra)


def test_build_report_nothing_found_clean(tracker_db, tmp_path):
    uid = uuid.uuid4().hex
    report = pii.build_report(
        uid,
        db_path=tracker_db,
        app_sqlite_path=None,
        users_root=tmp_path / "users",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
    )
    assert report["extra_places_beyond_admin_delete_user"] == []


def test_format_report_includes_decision_rule(tracker_db, tmp_path):
    uid = uuid.uuid4().hex
    report = pii.build_report(
        uid,
        db_path=tracker_db,
        app_sqlite_path=None,
        users_root=tmp_path / "users",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
    )
    text = pii.format_report(report)
    assert "Decision rule" in text
    assert uid in text
