"""Tests for tracker.get_url_status_flags."""
from hunter import tracker
from hunter.models import Job


def _add_row_direct(tracker_db, *, url: str, ats: str, sent: str = "") -> None:
    """Insert a minimal row directly into the SQLite DB for test setup."""
    from hunter.db import get_db
    import uuid
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, stack, ats_status, url, url_norm, sent)
            VALUES (?, '2026-04-16', 'Acme', 'Dev', 'Angular', ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex[:8], ats, url, tracker.normalize_url(url), sent),
        )


def test_get_url_status_flags_detects_success(tracker_db) -> None:
    _add_row_direct(
        tracker_db,
        url="https://example.com/jobs/1?utm_source=mail",
        ats="82%",
    )
    flags = tracker.get_url_status_flags("https://example.com/jobs/1")
    assert flags == {"has_success": True, "is_react_skip": False}


def test_get_url_status_flags_detects_react_skip(tracker_db) -> None:
    _add_row_direct(
        tracker_db,
        url="https://example.com/jobs/2",
        ats="SKIP",
        sent="—",
    )
    flags = tracker.get_url_status_flags("https://example.com/jobs/2")
    assert flags == {"has_success": False, "is_react_skip": True}


def test_get_url_status_flags_ignores_fail_and_plain_skip(tracker_db) -> None:
    _add_row_direct(tracker_db, url="https://example.com/jobs/3", ats="FAIL")
    _add_row_direct(tracker_db, url="https://example.com/jobs/3", ats="SKIP", sent="")
    flags = tracker.get_url_status_flags("https://example.com/jobs/3")
    assert flags == {"has_success": False, "is_react_skip": False}


def test_get_url_status_flags_is_case_insensitive_for_status_values(tracker_db) -> None:
    _add_row_direct(tracker_db, url="https://example.com/jobs/4", ats="skip", sent="—")
    _add_row_direct(tracker_db, url="https://example.com/jobs/5", ats="fail")
    flags_skip = tracker.get_url_status_flags("https://example.com/jobs/4")
    flags_fail = tracker.get_url_status_flags("https://example.com/jobs/5")
    assert flags_skip == {"has_success": False, "is_react_skip": True}
    assert flags_fail == {"has_success": False, "is_react_skip": False}
