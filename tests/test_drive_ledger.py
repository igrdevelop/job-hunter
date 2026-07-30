"""Tests for hunter/drive_ledger.py — content-signature ledger for shadow uploads.

Every test isolates on a fresh temp DB via the `ledger_db` fixture (mirrors
tests/test_source_health.py's `health_db` pattern) — never touches the real
tracker.db in the repo root.
"""

from __future__ import annotations

import pytest

from hunter import drive_ledger


@pytest.fixture()
def ledger_db(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setattr(drive_ledger, "DB_PATH", db)
    return db


# ---------------------------------------------------------------------------
# signature()
# ---------------------------------------------------------------------------


def test_signature_empty_folder(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    assert drive_ledger.signature(folder) == "0:0:0"


def test_signature_missing_folder(tmp_path):
    assert drive_ledger.signature(tmp_path / "does_not_exist") == "0:0:0"


def test_signature_reflects_file_count(tmp_path):
    folder = tmp_path / "shadow"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"x")
    sig1 = drive_ledger.signature(folder)
    (folder / "b.pdf").write_bytes(b"y")
    sig2 = drive_ledger.signature(folder)
    assert sig1 != sig2
    assert sig1.split(":")[0] == "1"
    assert sig2.split(":")[0] == "2"


def test_signature_reflects_total_size(tmp_path):
    folder = tmp_path / "shadow"
    folder.mkdir()
    f = folder / "a.pdf"
    f.write_bytes(b"x")
    sig1 = drive_ledger.signature(folder)
    f.write_bytes(b"a much longer body than before")
    sig2 = drive_ledger.signature(folder)
    assert sig1 != sig2


def test_signature_reflects_mtime(tmp_path):
    import os
    import time

    folder = tmp_path / "shadow"
    folder.mkdir()
    f = folder / "a.pdf"
    f.write_bytes(b"x")
    sig1 = drive_ledger.signature(folder)
    time.sleep(0.01)
    # Bump mtime without changing size/count (e.g. a re-render that produces
    # byte-identical content but a fresh file).
    os.utime(f, None)
    sig2 = drive_ledger.signature(folder)
    assert sig1 != sig2


def test_signature_ignores_subdirectories(tmp_path):
    folder = tmp_path / "shadow"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"x")
    sig1 = drive_ledger.signature(folder)
    (folder / "nested").mkdir()
    (folder / "nested" / "b.pdf").write_bytes(b"y")
    sig2 = drive_ledger.signature(folder)
    # Applications/ folders are flat — only direct files count, matching
    # gdrive_client.upload_folder's own non-recursive assumption.
    assert sig1 == sig2


# ---------------------------------------------------------------------------
# is_current / record / forget
# ---------------------------------------------------------------------------


def test_is_current_false_when_never_recorded(ledger_db):
    assert drive_ledger.is_current("/some/path", "1:2:3") is False


def test_record_then_is_current_true(ledger_db):
    drive_ledger.record("/some/path", "1:2:3", "https://drive.google.com/drive/folders/x")
    assert drive_ledger.is_current("/some/path", "1:2:3") is True


def test_is_current_false_on_signature_mismatch(ledger_db):
    drive_ledger.record("/some/path", "1:2:3", "https://drive.google.com/drive/folders/x")
    assert drive_ledger.is_current("/some/path", "9:9:9") is False


def test_record_overwrites_previous_signature(ledger_db):
    drive_ledger.record("/some/path", "1:2:3", "url1")
    drive_ledger.record("/some/path", "9:9:9", "url2")
    assert drive_ledger.is_current("/some/path", "1:2:3") is False
    assert drive_ledger.is_current("/some/path", "9:9:9") is True


def test_forget_clears_signature(ledger_db):
    drive_ledger.record("/some/path", "1:2:3", "url1")
    drive_ledger.forget("/some/path")
    assert drive_ledger.is_current("/some/path", "1:2:3") is False


def test_forget_nonexistent_path_is_a_noop(ledger_db):
    drive_ledger.forget("/never/recorded")  # must not raise


def test_paths_are_independent(ledger_db):
    drive_ledger.record("/a", "1:1:1", "url_a")
    drive_ledger.record("/b", "1:1:1", "url_b")
    drive_ledger.forget("/a")
    assert drive_ledger.is_current("/a", "1:1:1") is False
    assert drive_ledger.is_current("/b", "1:1:1") is True
