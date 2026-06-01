"""Tests for tracker.get_drive_url_by_url and tracker.set_drive_url."""

import pytest

from hunter import tracker
from hunter.tracker import COL_DRIVE_URL
from hunter.db import get_db


def _insert_row(tracker_db, *, url: str, drive_url: str = "", row_id: str = "abc12345") -> None:
    """Insert a minimal row directly into the SQLite DB."""
    import uuid
    norm = tracker.normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, drive_url)
            VALUES (?, '2026-05-22', 'Acme', 'Dev', '85%', ?, ?, ?)
            """,
            (row_id, url, norm, drive_url),
        )


# ---------------------------------------------------------------------------
# get_drive_url_by_url
# ---------------------------------------------------------------------------

def test_get_drive_url_returns_none_when_no_tracker(tracker_db):
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1") is None


def test_get_drive_url_returns_none_when_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.get_drive_url_by_url("https://example.com/jobs/99") is None


def test_get_drive_url_returns_none_when_col_empty(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1", drive_url="")
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1") is None


def test_get_drive_url_returns_stored_url(tracker_db):
    drive = "https://drive.google.com/drive/folders/abc"
    _insert_row(tracker_db, url="https://example.com/jobs/1", drive_url=drive)
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1") == drive


def test_get_drive_url_normalizes_job_url(tracker_db):
    drive = "https://drive.google.com/drive/folders/xyz"
    _insert_row(tracker_db, url="https://example.com/jobs/1", drive_url=drive)
    # URL with trailing slash and utm param — should still match
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1/?utm_source=test") == drive


# ---------------------------------------------------------------------------
# set_drive_url
# ---------------------------------------------------------------------------

def test_set_drive_url_noop_when_empty_db(tracker_db):
    # Should not raise even if no rows exist
    tracker.set_drive_url("https://example.com/jobs/1", "https://drive.google.com/x")


def test_set_drive_url_noop_when_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    tracker.set_drive_url("https://example.com/jobs/99", "https://drive.google.com/x")
    # Original row should be untouched
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1") is None


def test_set_drive_url_writes_value(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    drive = "https://drive.google.com/drive/folders/newid"
    tracker.set_drive_url("https://example.com/jobs/1", drive)
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1") == drive


def test_set_drive_url_is_idempotent(tracker_db):
    drive = "https://drive.google.com/drive/folders/abc"
    _insert_row(tracker_db, url="https://example.com/jobs/1", drive_url=drive)
    tracker.set_drive_url("https://example.com/jobs/1", drive)
    assert tracker.get_drive_url_by_url("https://example.com/jobs/1") == drive


def test_set_drive_url_updates_all_matching_rows(tracker_db):
    """set_drive_url updates all rows with matching url_norm (re-apply scenario)."""
    _insert_row(tracker_db, url="https://example.com/jobs/1", row_id="aaa11111")
    _insert_row(tracker_db, url="https://example.com/jobs/1", row_id="bbb22222")
    drive = "https://drive.google.com/drive/folders/first"
    tracker.set_drive_url("https://example.com/jobs/1", drive)

    with get_db(tracker_db) as conn:
        rows = conn.execute(
            "SELECT drive_url FROM applications ORDER BY rowid"
        ).fetchall()
    # Both rows updated (SQLite UPDATE without LIMIT updates all matching)
    assert all(r["drive_url"] == drive for r in rows)


# ---------------------------------------------------------------------------
# read_all_tracker_rows includes Drive URL
# ---------------------------------------------------------------------------

def test_read_all_tracker_rows_includes_drive_url(tracker_db):
    drive = "https://drive.google.com/drive/folders/abc"
    _insert_row(tracker_db, url="https://example.com/jobs/1", drive_url=drive)
    rows = tracker.read_all_tracker_rows()
    assert len(rows) == 1
    assert rows[0]["Drive URL"] == drive


def test_read_all_tracker_rows_drive_url_blank_for_rows_without_it(tracker_db):
    """Rows with no drive_url return empty string."""
    _insert_row(tracker_db, url="https://example.com/jobs/1", drive_url="")
    rows = tracker.read_all_tracker_rows()
    assert rows[0]["Drive URL"] == ""
