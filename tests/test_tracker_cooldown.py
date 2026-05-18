"""Tests for B5 — cooldown: don't re-apply to same company+title within N days."""
import datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from hunter.tracker import is_in_cooldown, COMPANY_COL_INDEX, TITLE_COL_INDEX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker(tmp_path: Path, rows: list[dict]) -> Path:
    """Create a minimal tracker.xlsx with the given rows."""
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = [
        "Date", "Company", "Job Title", "Stack", "ATS %", "URL",
        "Folder", "Sent", "Re-application", "To Learn", "ID",
    ]
    ws.append(headers)
    for r in rows:
        ws.append([
            r.get("date"),
            r.get("company", ""),
            r.get("title", ""),
            r.get("stack", ""),
            r.get("ats", ""),
            r.get("url", ""),
            r.get("folder", ""),
            r.get("sent", ""),
            r.get("reapp", ""),
            r.get("tolearn", ""),
            r.get("rid", ""),
        ])
    path = tmp_path / "tracker.xlsx"
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# is_in_cooldown — basic cases
# ---------------------------------------------------------------------------

def test_cooldown_false_when_tracker_empty(tmp_path: Path) -> None:
    path = _make_tracker(tmp_path, [])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert not is_in_cooldown("Acme", "Senior Angular Developer")


def test_cooldown_false_for_unknown_company(tmp_path: Path) -> None:
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=5), "company": "OtherCo", "title": "Frontend Dev", "ats": "98%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert not is_in_cooldown("Acme", "Senior Angular Developer")


def test_cooldown_true_when_applied_recently(tmp_path: Path) -> None:
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=10), "company": "Acme", "title": "Senior Angular Developer", "ats": "97%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert is_in_cooldown("Acme", "Senior Angular Developer", cooldown_days=30)


def test_cooldown_false_when_applied_long_ago(tmp_path: Path) -> None:
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=45), "company": "Acme", "title": "Senior Angular Developer", "ats": "97%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert not is_in_cooldown("Acme", "Senior Angular Developer", cooldown_days=30)


def test_cooldown_boundary_exactly_at_limit_is_ok(tmp_path: Path) -> None:
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=30), "company": "Acme", "title": "Angular Dev", "ats": "97%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert not is_in_cooldown("Acme", "Angular Dev", cooldown_days=30)


def test_cooldown_uses_most_recent_date(tmp_path: Path) -> None:
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=60), "company": "Acme", "title": "Angular Dev", "ats": "97%"},
        {"date": today - datetime.timedelta(days=5), "company": "Acme", "title": "Angular Dev", "ats": "99%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert is_in_cooldown("Acme", "Angular Dev", cooldown_days=30)


def test_cooldown_company_normalization(tmp_path: Path) -> None:
    """UPVANTA and Upvanta Sp. z o.o. should hit the same cooldown."""
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=5), "company": "Upvanta Sp. z o.o.", "title": "Angular Developer", "ats": "99%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert is_in_cooldown("UPVANTA", "Angular Developer", cooldown_days=30)


def test_cooldown_skipped_rows_not_counted(tmp_path: Path) -> None:
    """SKIP rows should not trigger cooldown — we only blocked, didn't apply."""
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=5), "company": "Acme", "title": "Angular Dev", "ats": "SKIP"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert not is_in_cooldown("Acme", "Angular Dev", cooldown_days=30)


def test_cooldown_default_is_30_days(tmp_path: Path) -> None:
    today = datetime.date.today()
    path = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=20), "company": "Acme", "title": "Angular Dev", "ats": "97%"},
    ])
    with patch("hunter.tracker.TRACKER_PATH", path):
        assert is_in_cooldown("Acme", "Angular Dev")  # default cooldown_days=30
