"""Tests for the ats_verdict DB column + tracker.set_ats_verdict / get_row_id_by_url.

Phase 2 of the ATS-verdict work (docs/ATS_VERDICT_PHASE2_PLAN.md, M1): the
independent PDF-verdict score is stamped on the tracker row post-hoc (the row
already exists when the verdict is computed), matched by normalized URL.
"""

from hunter import tracker
from hunter.db import get_db


def _insert_row(tracker_db, *, url: str, row_id: str = "abc12345") -> None:
    norm = tracker.normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm)
            VALUES (?, '2026-07-02', 'Acme', 'Dev', '97%', ?, ?)
            """,
            (row_id, url, norm),
        )


def _verdict_of(tracker_db, row_id: str):
    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT ats_verdict FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    return row["ats_verdict"] if row else None


# ── Schema migration ──────────────────────────────────────────────────────────

def test_ats_verdict_column_exists(tracker_db):
    """The lazy migration in db._ensure_columns adds ats_verdict to fresh DBs."""
    with get_db(tracker_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    assert "ats_verdict" in cols


# ── set_ats_verdict ───────────────────────────────────────────────────────────

def test_set_ats_verdict_writes_value(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_ats_verdict("https://example.com/jobs/1", 91.0) is True
    assert _verdict_of(tracker_db, "abc12345") == 91.0


def test_set_ats_verdict_normalizes_url(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_ats_verdict(
        "https://example.com/jobs/1/?utm_source=x", 88.5
    ) is True
    assert _verdict_of(tracker_db, "abc12345") == 88.5


def test_set_ats_verdict_false_when_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_ats_verdict("https://example.com/jobs/99", 90.0) is False
    assert _verdict_of(tracker_db, "abc12345") is None


def test_set_ats_verdict_false_on_empty_url(tracker_db):
    assert tracker.set_ats_verdict("", 90.0) is False


def test_set_ats_verdict_overwrites_previous(tracker_db):
    """A re-run (e.g. /force) refreshes the verdict."""
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    tracker.set_ats_verdict("https://example.com/jobs/1", 80.0)
    tracker.set_ats_verdict("https://example.com/jobs/1", 92.0)
    assert _verdict_of(tracker_db, "abc12345") == 92.0


def test_set_ats_verdict_never_raises(tracker_db, monkeypatch):
    """Best-effort contract: DB failure logs and returns False."""
    def _boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(tracker, "get_db", _boom)
    assert tracker.set_ats_verdict("https://example.com/jobs/1", 90.0) is False


# ── get_row_id_by_url ─────────────────────────────────────────────────────────

def test_get_row_id_by_url_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1", row_id="dead0001")
    assert tracker.get_row_id_by_url("https://example.com/jobs/1") == "dead0001"


def test_get_row_id_by_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.get_row_id_by_url("https://example.com/jobs/99") is None


def test_get_row_id_by_url_empty_url(tracker_db):
    assert tracker.get_row_id_by_url("") is None
