"""Tests for hunter/tracker_backup.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _quiesce(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, keep: int = 90) -> None:
    """Point every backup source at a safe, isolated (mostly-missing) location.

    Every test starts from this baseline and then monkeypatches in exactly
    the source(s) it cares about — keeps each test from depending on what
    this dev checkout's real tracker.db / .env happens to contain.
    """
    import hunter.config as cfg

    monkeypatch.setattr(cfg, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(cfg, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    monkeypatch.setattr(cfg, "TRACKER_DB_PATH", tmp_path / "tracker.db")
    monkeypatch.setattr(cfg, "APP_SQLITE_PATH", "")
    monkeypatch.setattr(cfg, "TRACKER_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "TRACKER_BACKUP_KEEP_FILES", keep)


def _make_sqlite_db(path: Path, rows: list[str]) -> sqlite3.Connection:
    """Create a WAL-mode sqlite db with one committed row per string in rows.

    Returns the OPEN connection (caller decides when to close it) — tests use
    this to simulate a live process whose WAL sidecar hasn't been
    checkpointed into the main file yet, which is exactly the case
    shutil.copy2 handles unsafely and Connection.backup() handles correctly.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, url TEXT NOT NULL)")
    for url in rows:
        conn.execute("INSERT INTO applications (url) VALUES (?)", (url,))
    conn.commit()
    return conn


def test_run_tracker_backup_copies_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path, keep=5)
    tracker = tmp_path / "tracker.xlsx"
    tracker.write_bytes(b"PK\x03\x04fake")
    monkeypatch.setattr(cfg, "TRACKER_PATH", tracker)

    r = tb.run_tracker_backup()
    assert r["ok"] is True
    assert r["errors"] == []
    xlsx_copies = [
        c for c in r["copied"] if c.startswith("backups/tracker_") and c.endswith(".xlsx")
    ]
    assert len(xlsx_copies) == 1
    assert list((tmp_path / "backups").glob("tracker_*.xlsx"))


def test_missing_tracker_xlsx_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy xlsx snapshot: prod only writes it on /export — absence is normal."""
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)
    r = tb.run_tracker_backup()
    assert r["ok"] is True
    assert r["errors"] == []
    assert any("tracker: missing" in s for s in r["skipped"])


def test_prune_keeps_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path, keep=2)
    tracker = tmp_path / "tracker.xlsx"
    tracker.write_bytes(b"x")
    monkeypatch.setattr(cfg, "TRACKER_PATH", tracker)

    tb.run_tracker_backup()
    tb.run_tracker_backup()
    tb.run_tracker_backup()

    assert len(list((tmp_path / "backups").glob("tracker_*.xlsx"))) == 2


def test_backup_tracker_db_is_wal_safe_and_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live WAL db (uncommitted-checkpoint pages, connection still open)
    must back up via Connection.backup(), never a raw file copy — the
    produced copy must pass integrity_check and contain the committed rows.
    """
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)
    db_path = tmp_path / "tracker.db"
    monkeypatch.setattr(cfg, "TRACKER_DB_PATH", db_path)

    # Committed rows, WAL sidecar NOT checkpointed, connection left open —
    # this is what a live bot process's db looks like at backup time.
    src_conn = _make_sqlite_db(db_path, ["https://example.com/job1", "https://example.com/job2"])
    assert (tmp_path / "tracker.db-wal").exists()

    try:
        r = tb.run_tracker_backup()
    finally:
        src_conn.close()

    assert r["ok"] is True, r["errors"]
    assert r["errors"] == []
    db_backups = list((tmp_path / "backups").glob("tracker_db_*.db"))
    assert len(db_backups) == 1
    assert any(c.startswith("backups/tracker_db_") for c in r["copied"])

    check_conn = sqlite3.connect(str(db_backups[0]))
    try:
        status = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = [row[0] for row in check_conn.execute("SELECT url FROM applications ORDER BY id")]
    finally:
        check_conn.close()

    assert status == "ok"
    assert rows == ["https://example.com/job1", "https://example.com/job2"]


def test_missing_tracker_db_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)
    r = tb.run_tracker_backup()
    assert r["ok"] is True
    assert r["errors"] == []
    assert any("tracker_db: missing" in s for s in r["skipped"])


def test_tracker_db_prune_keeps_newest_independently_of_xlsx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path, keep=2)
    db_path = tmp_path / "tracker.db"
    monkeypatch.setattr(cfg, "TRACKER_DB_PATH", db_path)

    for _ in range(3):
        conn = _make_sqlite_db(db_path, ["https://example.com/x"])
        conn.close()
        r = tb.run_tracker_backup()
        assert r["ok"] is True, r["errors"]
        # Simulate a fresh db each "run" so backup() re-copies cleanly —
        # remove the main file plus any WAL/SHM sidecars sqlite left behind.
        for sidecar in (db_path, tmp_path / "tracker.db-wal", tmp_path / "tracker.db-shm"):
            sidecar.unlink(missing_ok=True)

    assert len(list((tmp_path / "backups").glob("tracker_db_*.db"))) == 2


def test_app_sqlite_unset_is_skipped_without_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)  # APP_SQLITE_PATH == "" from _quiesce
    r = tb.run_tracker_backup()
    assert r["ok"] is True
    assert r["errors"] == []
    assert not list((tmp_path / "backups").glob("app_sqlite_*.db"))
    assert not any("app_sqlite" in s for s in r["skipped"])


def test_app_sqlite_set_and_present_is_backed_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)
    api_db = tmp_path / "app.sqlite"
    conn = sqlite3.connect(str(api_db))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO users (email) VALUES ('owner@example.com')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(cfg, "APP_SQLITE_PATH", str(api_db))

    r = tb.run_tracker_backup()
    assert r["ok"] is True, r["errors"]
    assert r["errors"] == []
    app_backups = list((tmp_path / "backups").glob("app_sqlite_*.db"))
    assert len(app_backups) == 1

    check_conn = sqlite3.connect(str(app_backups[0]))
    try:
        status = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        emails = [row[0] for row in check_conn.execute("SELECT email FROM users")]
    finally:
        check_conn.close()
    assert status == "ok"
    assert emails == ["owner@example.com"]


def test_app_sqlite_set_but_missing_is_skipped_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "APP_SQLITE_PATH", str(tmp_path / "does_not_exist.sqlite"))

    r = tb.run_tracker_backup()
    assert r["ok"] is True
    assert r["errors"] == []
    assert any("app_sqlite: missing" in s for s in r["skipped"])


def test_integrity_check_failure_is_reported_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt PRODUCED COPY must flip ok=False and land in errors — the
    whole point of running integrity_check after the backup.
    """
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    _quiesce(monkeypatch, tmp_path)
    db_path = tmp_path / "tracker.db"
    monkeypatch.setattr(cfg, "TRACKER_DB_PATH", db_path)
    conn = _make_sqlite_db(db_path, ["https://example.com/job1"])
    conn.close()

    backup_dir = tmp_path / "backups"

    def _fake_verify(label: str, dest: Path, result: dict) -> None:
        result["ok"] = False
        result["errors"].append(f"{label}: integrity_check failed: corruption simulated")

    monkeypatch.setattr(tb, "_verify_integrity", _fake_verify)

    r = tb.run_tracker_backup()
    assert r["ok"] is False
    assert any("integrity_check failed" in e for e in r["errors"])
    # The copy is still produced/kept for forensics, not silently discarded.
    assert list(backup_dir.glob("tracker_db_*.db"))
