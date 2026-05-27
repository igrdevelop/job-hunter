"""Tests for B5 — cooldown: don't re-apply to same company+title within N days."""
import datetime
import uuid

import pytest

from hunter.tracker import is_in_cooldown, company_cooldown_active
from hunter.db import get_db
from hunter import tracker


# ---------------------------------------------------------------------------
# Helper: insert a row directly into the DB
# ---------------------------------------------------------------------------

def _insert(tracker_db, *, date_str: str, company: str, title: str,
            ats: str, url: str = "") -> None:
    norm = tracker.normalize_url(url) if url else ""
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex[:8], date_str, company, title, ats, url, norm),
        )


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# is_in_cooldown — basic cases
# ---------------------------------------------------------------------------

def test_cooldown_false_when_tracker_empty(tracker_db) -> None:
    assert not is_in_cooldown("Acme", "Senior Angular Developer")


def test_cooldown_false_for_unknown_company(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(5), company="OtherCo",
            title="Frontend Dev", ats="98%")
    assert not is_in_cooldown("Acme", "Senior Angular Developer")


def test_cooldown_true_when_applied_recently(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(10), company="Acme",
            title="Senior Angular Developer", ats="97%")
    assert is_in_cooldown("Acme", "Senior Angular Developer", cooldown_days=30)


def test_cooldown_false_when_applied_long_ago(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(45), company="Acme",
            title="Senior Angular Developer", ats="97%")
    assert not is_in_cooldown("Acme", "Senior Angular Developer", cooldown_days=30)


def test_cooldown_boundary_exactly_at_limit_is_ok(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(30), company="Acme",
            title="Angular Dev", ats="97%")
    assert not is_in_cooldown("Acme", "Angular Dev", cooldown_days=30)


def test_cooldown_uses_most_recent_date(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(60), company="Acme",
            title="Angular Dev", ats="97%")
    _insert(tracker_db, date_str=_days_ago(5), company="Acme",
            title="Angular Dev", ats="99%")
    assert is_in_cooldown("Acme", "Angular Dev", cooldown_days=30)


def test_cooldown_company_normalization(tracker_db) -> None:
    """UPVANTA and Upvanta Sp. z o.o. should hit the same cooldown."""
    _insert(tracker_db, date_str=_days_ago(5), company="Upvanta Sp. z o.o.",
            title="Angular Developer", ats="99%")
    assert is_in_cooldown("UPVANTA", "Angular Developer", cooldown_days=30)


def test_cooldown_skipped_rows_not_counted(tracker_db) -> None:
    """SKIP rows should not trigger cooldown."""
    _insert(tracker_db, date_str=_days_ago(5), company="Acme",
            title="Angular Dev", ats="SKIP")
    assert not is_in_cooldown("Acme", "Angular Dev", cooldown_days=30)


def test_cooldown_default_is_12_days(tracker_db) -> None:
    """Default cooldown is 12 days."""
    _insert(tracker_db, date_str=_days_ago(5), company="Acme",
            title="Angular Dev", ats="97%")
    assert is_in_cooldown("Acme", "Angular Dev")


def test_cooldown_default_12_not_triggered_after_13_days(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(13), company="Acme",
            title="Angular Dev", ats="97%")
    assert not is_in_cooldown("Acme", "Angular Dev")


# ---------------------------------------------------------------------------
# company_cooldown_active
# ---------------------------------------------------------------------------

def test_company_cooldown_true_when_applied_recently(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(30), company="Acme",
            title="Angular Developer", ats="97%")
    assert company_cooldown_active("Acme", days=180)


def test_company_cooldown_false_when_only_skip_rows(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(10), company="Acme",
            title="Angular Dev", ats="SKIP")
    _insert(tracker_db, date_str=_days_ago(10), company="Acme",
            title="Backend Dev", ats="EXPIRED")
    assert not company_cooldown_active("Acme", days=180)


def test_company_cooldown_false_for_different_company(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(10), company="OtherCo",
            title="Angular Dev", ats="97%")
    assert not company_cooldown_active("Acme", days=180)


def test_company_cooldown_false_when_old_enough(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(200), company="Acme",
            title="Angular Dev", ats="99%")
    assert not company_cooldown_active("Acme", days=180)


def test_company_cooldown_legal_suffix_normalization(tracker_db) -> None:
    _insert(tracker_db, date_str=_days_ago(10), company="Acme Sp. z o.o.",
            title="Angular Dev", ats="97%")
    assert company_cooldown_active("ACME", days=180)
