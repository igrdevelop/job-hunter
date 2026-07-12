"""Tests for the Sheets→DB bootstrap dedup self-heal.

Covers tracker.insert_pulled_rows() (insert Sheets rows missing from a fresh DB)
and the pull_full_snapshot() integration that wires it before the conflict matrix.

See docs/archive/BOOTSTRAP_DEDUP_PLAN.md.
"""

import asyncio
from unittest.mock import MagicMock, patch

from hunter import tracker
from hunter.db import get_db


def run(coro):
    return asyncio.run(coro)


def _sheet_row(row_id, url, **extra):
    """Build a Sheets row dict as returned by gsheets_client.read_all."""
    row = {
        "Date": "2026-06-01",
        "Company": "Acme",
        "Job Title": "Senior Angular Dev",
        "Stack": "Angular",
        "ATS %": "85%",
        "URL": url,
        "Folder": "/app/Applications/2026-06-01/Acme",
        "Sent": "",
        "Re-application": "",
        "To Learn": "",
        "ID": row_id,
    }
    row.update(extra)
    return row


def _insert_db_row(tracker_db, *, row_id, url, **extra):
    """Insert a minimal applications row directly into the DB."""
    norm = tracker.normalize_url(url) if url else ""
    cols = {
        "id": row_id,
        "date": "2026-05-01",
        "company": "Existing",
        "title": "Dev",
        "ats_status": "80%",
        "url": url,
        "url_norm": norm,
    }
    cols.update(extra)
    keys = ", ".join(cols.keys())
    placeholders = ", ".join(f":{k}" for k in cols)
    with get_db(tracker_db) as conn:
        conn.execute(f"INSERT INTO applications ({keys}) VALUES ({placeholders})", cols)


# ── insert_pulled_rows ────────────────────────────────────────────────────────


def test_insert_pulled_rows_inserts_missing(tracker_db):
    rows = [
        (2, _sheet_row("aaaaaaaa", "https://example.com/jobs/1")),
        (3, _sheet_row("bbbbbbbb", "https://example.com/jobs/2")),
        (4, _sheet_row("cccccccc", "https://example.com/jobs/3")),
    ]
    inserted = tracker.insert_pulled_rows(rows)
    assert inserted == 3

    known = tracker.get_known_urls()
    assert tracker.normalize_url("https://example.com/jobs/1") in known
    assert tracker.normalize_url("https://example.com/jobs/2") in known
    assert tracker.normalize_url("https://example.com/jobs/3") in known

    # sheets_row + sheets_dirty persisted correctly
    with get_db(tracker_db) as conn:
        r = conn.execute(
            "SELECT sheets_row, sheets_dirty FROM applications WHERE id='aaaaaaaa'"
        ).fetchone()
    assert r["sheets_row"] == 2
    assert r["sheets_dirty"] == 0


def test_insert_pulled_rows_skips_existing_by_id(tracker_db):
    _insert_db_row(tracker_db, row_id="aaaaaaaa", url="https://example.com/jobs/1")
    rows = [(2, _sheet_row("aaaaaaaa", "https://example.com/other", Company="Changed"))]
    inserted = tracker.insert_pulled_rows(rows)
    assert inserted == 0

    # existing row untouched
    with get_db(tracker_db) as conn:
        r = conn.execute("SELECT company FROM applications WHERE id='aaaaaaaa'").fetchone()
    assert r["company"] == "Existing"


def test_insert_pulled_rows_skips_existing_by_url_norm(tracker_db):
    _insert_db_row(tracker_db, row_id="oldid000", url="https://example.com/jobs/1")
    # Same URL, different ID — should be deduped by url_norm
    rows = [(2, _sheet_row("newid111", "https://example.com/jobs/1/?utm_source=x"))]
    inserted = tracker.insert_pulled_rows(rows)
    assert inserted == 0

    with get_db(tracker_db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
    assert n == 1


def test_insert_pulled_rows_dedups_within_batch(tracker_db):
    rows = [
        (2, _sheet_row("aaaaaaaa", "https://example.com/jobs/1")),
        (3, _sheet_row("bbbbbbbb", "https://example.com/jobs/1/?utm_campaign=y")),
    ]
    inserted = tracker.insert_pulled_rows(rows)
    assert inserted == 1


def test_insert_pulled_rows_skips_blank_id(tracker_db):
    rows = [
        (2, _sheet_row("", "https://example.com/jobs/1")),
        (3, _sheet_row("bbbbbbbb", "https://example.com/jobs/2")),
    ]
    inserted = tracker.insert_pulled_rows(rows)
    assert inserted == 1
    known = tracker.get_known_urls()
    assert tracker.normalize_url("https://example.com/jobs/1") not in known
    assert tracker.normalize_url("https://example.com/jobs/2") in known


def test_insert_pulled_rows_empty_input(tracker_db):
    assert tracker.insert_pulled_rows([]) == 0


# ── pull_full_snapshot integration ────────────────────────────────────────────


def test_pull_full_snapshot_inserts_then_updates(tracker_db):
    """Pull inserts a missing row and applies conflict matrix to an existing one."""
    # Existing DB row that the Sheet has a newer Sent for
    _insert_db_row(tracker_db, row_id="exist000", url="https://example.com/old", sent="")

    sheets_rows = [
        (2, _sheet_row("exist000", "https://example.com/old", Sent="2026-06-02")),
        (3, _sheet_row("newrow11", "https://example.com/new")),
    ]

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_rows),
    ):
        from hunter import gsheets_sync

        result = run(gsheets_sync.pull_full_snapshot())

    assert result["pulled"] == 2
    assert result["inserted"] == 1  # newrow11 inserted
    assert result["updated"] == 1  # exist000 Sent updated

    known = tracker.get_known_urls()
    assert tracker.normalize_url("https://example.com/new") in known

    with get_db(tracker_db) as conn:
        r = conn.execute("SELECT sent FROM applications WHERE id='exist000'").fetchone()
    assert r["sent"] == "2026-06-02"


def test_pull_self_heals_blind_dedup(tracker_db):
    """Regression: after pull, previously-applied URLs are 'known' again.

    Simulates a fresh/empty tracker.db that pulls history from the shared Sheet;
    get_known_urls() (the dedup source in main.py) must then contain those URLs.
    """
    assert tracker.get_known_urls() == set()  # blind DB

    sheets_rows = [
        (2, _sheet_row("aaaaaaaa", "https://example.com/jobs/1")),
        (3, _sheet_row("bbbbbbbb", "https://example.com/jobs/2")),
    ]
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_rows),
    ):
        from hunter import gsheets_sync

        run(gsheets_sync.pull_full_snapshot())

    known = tracker.get_known_urls()
    for u in ("https://example.com/jobs/1", "https://example.com/jobs/2"):
        assert tracker.normalize_url(u) in known
