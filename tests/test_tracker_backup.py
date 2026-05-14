"""Tests for hunter/tracker_backup.py."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_run_tracker_backup_copies_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    tracker = tmp_path / "tracker.xlsx"
    tracker.write_bytes(b"PK\x03\x04fake")

    monkeypatch.setattr(cfg, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(cfg, "TRACKER_PATH", tracker)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(cfg, "TRACKER_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(cfg, "TRACKER_BACKUP_KEEP_FILES", 5)

    r = tb.run_tracker_backup()
    assert r["ok"] is True
    assert r["errors"] == []
    assert len(r["copied"]) == 1
    assert r["copied"][0].startswith("backups/tracker_")
    assert list(backup_dir.glob("tracker_*.xlsx"))


def test_prune_keeps_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hunter.config as cfg
    import hunter.tracker_backup as tb

    tracker = tmp_path / "tracker.xlsx"
    tracker.write_bytes(b"x")
    monkeypatch.setattr(cfg, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(cfg, "TRACKER_PATH", tracker)
    bdir = tmp_path / "b"
    monkeypatch.setattr(cfg, "TRACKER_BACKUP_DIR", bdir)
    monkeypatch.setattr(cfg, "TRACKER_BACKUP_KEEP_FILES", 2)

    tb.run_tracker_backup()
    tb.run_tracker_backup()
    tb.run_tracker_backup()

    assert len(list(bdir.glob("tracker_*.xlsx"))) == 2
