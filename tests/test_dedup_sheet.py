"""Tests for tools/dedup_sheet.py — historical duplicate cleanup in the Sheet."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("dedup_sheet", ROOT / "tools" / "dedup_sheet.py")
dedup_sheet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dedup_sheet)


@pytest.fixture(autouse=True)
def _restore_state():
    """_resolve_sheet_id mutates gsheets_sync._state; restore it after each test."""
    saved = dict(dedup_sheet.gsheets_sync._state)
    yield
    dedup_sheet.gsheets_sync._state = saved


def _row(url, date="2026-05-01", sent="", company="Acme"):
    return {
        "Date": date,
        "Company": company,
        "Job Title": "Dev",
        "Stack": "Angular",
        "ATS %": "85%",
        "URL": url,
        "Folder": "",
        "Sent": sent,
        "Re-application": "",
        "To Learn": "",
        "ID": "x",
    }


# ── _parse_date ───────────────────────────────────────────────────────────────


def test_parse_date_iso():
    from datetime import date

    assert dedup_sheet._parse_date("2026-05-10") == date(2026, 5, 10)


def test_parse_date_datetime_string():
    from datetime import date

    assert dedup_sheet._parse_date("2026-05-10 00:00:00") == date(2026, 5, 10)


def test_parse_date_unknown_sorts_last():
    from datetime import date

    assert dedup_sheet._parse_date("garbage") == date.max
    assert dedup_sheet._parse_date("") == date.max


# ── _pick_keeper ──────────────────────────────────────────────────────────────


def test_pick_keeper_prefers_filled_sent():
    group = [
        (2, _row("u", date="2026-05-01", sent="")),
        (3, _row("u", date="2026-05-05", sent="2026-05-05")),
    ]
    keep_idx, _ = dedup_sheet._pick_keeper(group)
    assert keep_idx == 3  # the one with Sent, even though later date


def test_pick_keeper_earliest_when_no_sent():
    group = [
        (5, _row("u", date="2026-05-09", sent="")),
        (2, _row("u", date="2026-05-02", sent="")),
        (7, _row("u", date="2026-05-20", sent="")),
    ]
    keep_idx, _ = dedup_sheet._pick_keeper(group)
    assert keep_idx == 2


def test_pick_keeper_earliest_among_sent():
    group = [
        (2, _row("u", date="2026-05-09", sent="2026-05-09")),
        (3, _row("u", date="2026-05-02", sent="2026-05-02")),
    ]
    keep_idx, _ = dedup_sheet._pick_keeper(group)
    assert keep_idx == 3


# ── main (dry run / apply) ────────────────────────────────────────────────────


def _patches(rows):
    return (
        patch.object(dedup_sheet.gsheets_sync, "_get_service", return_value=MagicMock()),
        patch.object(dedup_sheet.gsheets_sync, "_read_state", return_value={"sheet_id": "s1"}),
        patch.object(dedup_sheet.gsheets_sync, "_sheet_id", return_value="s1"),
        patch.object(dedup_sheet, "read_all", return_value=rows),
    )


def test_main_dry_run_deletes_nothing(monkeypatch):
    rows = [
        (2, _row("https://x.com/1", date="2026-05-01")),
        (3, _row("https://x.com/1", date="2026-05-02")),
        (4, _row("https://x.com/2")),
    ]
    monkeypatch.setattr("sys.argv", ["dedup_sheet.py"])  # no --apply
    p = _patches(rows)
    with p[0], p[1], p[2], p[3], patch.object(dedup_sheet, "delete_sheet_row") as mock_del:
        rc = dedup_sheet.main()
    assert rc == 0
    mock_del.assert_not_called()


def test_main_apply_deletes_losers_high_to_low(monkeypatch):
    rows = [
        (2, _row("https://x.com/1", date="2026-05-01", sent="2026-05-01")),
        (3, _row("https://x.com/1", date="2026-05-02")),
        (5, _row("https://x.com/1", date="2026-05-03")),
        (4, _row("https://x.com/2")),  # unique → keep
    ]
    monkeypatch.setattr("sys.argv", ["dedup_sheet.py", "--apply"])
    p = _patches(rows)
    with p[0], p[1], p[2], p[3], patch.object(dedup_sheet, "delete_sheet_row") as mock_del:
        rc = dedup_sheet.main()
    assert rc == 0
    # keeper is row 2 (has Sent); rows 3 and 5 deleted, highest first
    deleted_idx = [call.args[2] for call in mock_del.call_args_list]
    assert deleted_idx == [5, 3]


def test_main_skips_rows_without_url(monkeypatch):
    rows = [
        (2, _row("", date="2026-05-01")),
        (3, _row("", date="2026-05-02")),
    ]
    monkeypatch.setattr("sys.argv", ["dedup_sheet.py", "--apply"])
    p = _patches(rows)
    with p[0], p[1], p[2], p[3], patch.object(dedup_sheet, "delete_sheet_row") as mock_del:
        rc = dedup_sheet.main()
    assert rc == 0
    mock_del.assert_not_called()


def test_main_errors_without_service(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dedup_sheet.py"])
    with patch.object(dedup_sheet.gsheets_sync, "_get_service", return_value=None):
        rc = dedup_sheet.main()
    assert rc == 1
