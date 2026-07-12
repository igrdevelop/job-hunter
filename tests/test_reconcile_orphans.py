"""Tests for orphan reconciliation: DB rows deleted from the Sheet.

Covers tracker.mark_orphans_expired() and gsheets_sync._reconcile_deleted_rows()
plus the pull_full_snapshot() integration that wires it after the conflict matrix.

Background: the pull conflict matrix only inserts + updates rows matched by ID; it
never reacts to deletions. A row removed from the Sheet would otherwise linger in
the DB with a blank Sent and pollute the unsent count forever.
"""

import asyncio
from unittest.mock import MagicMock, patch

from hunter import tracker
from hunter.db import get_db


def run(coro):
    return asyncio.run(coro)


def _sheet_row(row_id, url, **extra):
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


def _get(tracker_db, row_id):
    with get_db(tracker_db) as conn:
        return conn.execute(
            "SELECT sent, sheets_dirty, sheets_row FROM applications WHERE id=?",
            (row_id,),
        ).fetchone()


# ── mark_orphans_expired ──────────────────────────────────────────────────────


def test_mark_orphans_expired_stamps_and_clears(tracker_db):
    _insert_db_row(
        tracker_db,
        row_id="orph0001",
        url="https://example.com/1",
        sent="",
        sheets_dirty=1,
        sheets_row=492,
    )
    marked = tracker.mark_orphans_expired(["orph0001"])
    assert marked == 1
    r = _get(tracker_db, "orph0001")
    assert r["sent"] == "EXPIRED"
    assert r["sheets_dirty"] == 0  # never re-pushed
    assert r["sheets_row"] is None  # stale pointer cleared


def test_mark_orphans_expired_preserves_existing_sent(tracker_db):
    _insert_db_row(
        tracker_db, row_id="orph0002", url="https://example.com/2", sent="28 05", sheets_row=10
    )
    marked = tracker.mark_orphans_expired(["orph0002"])
    assert marked == 0  # guarded: Sent already set
    r = _get(tracker_db, "orph0002")
    assert r["sent"] == "28 05"
    assert r["sheets_row"] == 10


def test_mark_orphans_expired_empty_and_blank_ids(tracker_db):
    assert tracker.mark_orphans_expired([]) == 0
    assert tracker.mark_orphans_expired(["", "   "]) == 0


def test_mark_orphans_expired_skips_never_mirrored(tracker_db):
    # Row that was never pushed to the Sheet (sheets_row NULL) — e.g. the Sheets
    # token was down at apply time. It is absent from the Sheet because it was
    # never mirrored, NOT because the user deleted it → must NOT be expired.
    _insert_db_row(
        tracker_db, row_id="never001", url="https://example.com/n", sent="", sheets_dirty=1
    )  # sheets_row left NULL
    marked = tracker.mark_orphans_expired(["never001"])
    assert marked == 0
    r = _get(tracker_db, "never001")
    assert r["sent"] == ""  # still live/unsent
    assert r["sheets_row"] is None


# ── _reconcile_deleted_rows ───────────────────────────────────────────────────


def test_reconcile_marks_blank_orphan(tracker_db):
    # Several in-sheet rows (kept) so the read is not "partial", + 1 orphan gone
    # from the sheet with a blank sent.
    kept = [f"keep{i:04d}" for i in range(5)]
    for i, rid in enumerate(kept):
        _insert_db_row(tracker_db, row_id=rid, url=f"https://example.com/k{i}", sent="")
    # A genuinely deleted-from-Sheet row WAS mirrored before (sheets_row set).
    _insert_db_row(
        tracker_db, row_id="gone0001", url="https://example.com/gone", sent="", sheets_row=777
    )

    sheets_rows = [
        (i + 2, _sheet_row(rid, f"https://example.com/k{i}")) for i, rid in enumerate(kept)
    ]

    from hunter import gsheets_sync

    marked = gsheets_sync._reconcile_deleted_rows(sheets_rows)
    assert marked == 1
    assert _get(tracker_db, "gone0001")["sent"] == "EXPIRED"
    assert _get(tracker_db, "keep0000")["sent"] == ""  # in sheet → untouched


def test_reconcile_skips_never_mirrored_orphan(tracker_db):
    # Reproduces the prod bug: while the Sheets token was down, a batch of new
    # rows were inserted but never mirrored (sheets_row NULL). A later successful
    # pull must NOT mass-EXPIRE them just because their IDs aren't in the Sheet.
    kept = [f"keep{i:04d}" for i in range(5)]
    for i, rid in enumerate(kept):
        _insert_db_row(tracker_db, row_id=rid, url=f"https://example.com/k{i}", sent="")
    # never-mirrored newcomers (sheets_row NULL) absent from the Sheet read
    for i in range(3):
        _insert_db_row(
            tracker_db,
            row_id=f"new{i:05d}",
            url=f"https://example.com/new{i}",
            sent="",
            sheets_dirty=1,
        )

    sheets_rows = [
        (i + 2, _sheet_row(rid, f"https://example.com/k{i}")) for i, rid in enumerate(kept)
    ]

    from hunter import gsheets_sync

    marked = gsheets_sync._reconcile_deleted_rows(sheets_rows)
    assert marked == 0
    for i in range(3):
        assert _get(tracker_db, f"new{i:05d}")["sent"] == ""  # all stay live


def test_reconcile_leaves_annotated_orphan(tracker_db):
    # Orphan that the user already marked Sent — must not be touched
    kept = [f"keep{i:04d}" for i in range(5)]
    for i, rid in enumerate(kept):
        _insert_db_row(tracker_db, row_id=rid, url=f"https://example.com/k{i}", sent="")
    _insert_db_row(tracker_db, row_id="anno0001", url="https://example.com/anno", sent="01 06")

    sheets_rows = [
        (i + 2, _sheet_row(rid, f"https://example.com/k{i}")) for i, rid in enumerate(kept)
    ]

    from hunter import gsheets_sync

    marked = gsheets_sync._reconcile_deleted_rows(sheets_rows)
    assert marked == 0
    assert _get(tracker_db, "anno0001")["sent"] == "01 06"


def test_reconcile_partial_read_guard(tracker_db):
    # 10 blank DB rows, sheet returns only 1 ID → looks partial → skip entirely
    for i in range(10):
        _insert_db_row(tracker_db, row_id=f"row{i:05d}", url=f"https://example.com/{i}", sent="")
    sheets_rows = [(2, _sheet_row("row00000", "https://example.com/0"))]

    from hunter import gsheets_sync

    marked = gsheets_sync._reconcile_deleted_rows(sheets_rows)
    assert marked == 0
    # nothing got EXPIRED
    with get_db(tracker_db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM applications WHERE sent='EXPIRED'").fetchone()["c"]
    assert n == 0


def test_reconcile_empty_sheet_is_noop(tracker_db):
    _insert_db_row(tracker_db, row_id="row00001", url="https://example.com/1", sent="")
    from hunter import gsheets_sync

    assert gsheets_sync._reconcile_deleted_rows([]) == 0
    assert _get(tracker_db, "row00001")["sent"] == ""


# ── pull_full_snapshot integration ────────────────────────────────────────────


def test_pull_reconciles_and_reports_count(tracker_db):
    # Several in-sheet rows stay; gone0001 is an orphan to reconcile
    kept = [f"keep{i:04d}" for i in range(5)]
    for i, rid in enumerate(kept):
        _insert_db_row(tracker_db, row_id=rid, url=f"https://example.com/k{i}", sent="")
    _insert_db_row(
        tracker_db, row_id="gone0001", url="https://example.com/gone", sent="", sheets_row=777
    )

    sheets_rows = [
        (i + 2, _sheet_row(rid, f"https://example.com/k{i}")) for i, rid in enumerate(kept)
    ]

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_rows),
    ):
        from hunter import gsheets_sync

        result = run(gsheets_sync.pull_full_snapshot())

    assert result["reconciled"] == 1
    assert _get(tracker_db, "gone0001")["sent"] == "EXPIRED"
    assert _get(tracker_db, "keep0000")["sent"] == ""
