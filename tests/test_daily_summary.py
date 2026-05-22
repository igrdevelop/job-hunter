"""Tests for daily applications summary: get_applications_on_date,
_format_daily_summary, _scheduled_daily_summary, and updated
cmd_check_responses with days parameter.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import openpyxl
import pytest

from hunter import tracker
from hunter.telegram_bot import _format_daily_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _make_tracker(tmp_path, rows: list[dict]) -> None:
    """Minimal tracker with Date, Company, Job Title, ATS %, URL columns."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "Date", "Company", "Job Title", "Stack", "ATS %", "URL",
        "Folder", "Sent", "Re-application", "To Learn", "ID", "Drive URL",
        "Confirmation", "Answer",
    ])
    for i, r in enumerate(rows):
        ws.append([
            r.get("date", "2026-05-21"),
            r.get("company", "Acme"),
            r.get("title", "Angular Developer"),
            "Angular",
            r.get("ats", "85%"),
            r.get("url", f"https://example.com/{i}"),
            "", "", "", "", f"id{i}", "", "", "",
        ])
    wb.save(tmp_path / "tracker.xlsx")


# ---------------------------------------------------------------------------
# get_applications_on_date
# ---------------------------------------------------------------------------

def test_get_applications_returns_matching_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"date": "2026-05-21", "company": "NASK", "title": "Frontend Dev"},
        {"date": "2026-05-20", "company": "Acme", "title": "Angular Dev"},
        {"date": "2026-05-21", "company": "Sigma", "title": "JS Dev"},
    ])
    result = tracker.get_applications_on_date("2026-05-21")
    assert len(result) == 2
    companies = {r["company"] for r in result}
    assert companies == {"NASK", "Sigma"}


def test_get_applications_empty_when_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"date": "2026-05-20", "company": "Acme", "title": "Angular Dev"},
    ])
    result = tracker.get_applications_on_date("2026-05-21")
    assert result == []


def test_get_applications_empty_when_no_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "nonexistent.xlsx")
    assert tracker.get_applications_on_date("2026-05-21") == []


def test_get_applications_result_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"date": "2026-05-21", "company": "NASK", "title": "Senior Frontend Developer",
         "ats": "91%", "url": "https://nask.pl/job/1"},
    ])
    result = tracker.get_applications_on_date("2026-05-21")
    assert len(result) == 1
    r = result[0]
    assert r["company"] == "NASK"
    assert r["title"] == "Senior Frontend Developer"
    assert r["ats"] == "91%"
    assert r["url"] == "https://nask.pl/job/1"
    assert r["date"] == "2026-05-21"


def test_get_applications_skips_rows_without_company(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"date": "2026-05-21", "company": "", "title": "Some Dev"},
        {"date": "2026-05-21", "company": "NASK", "title": "Frontend Dev"},
    ])
    result = tracker.get_applications_on_date("2026-05-21")
    assert len(result) == 1
    assert result[0]["company"] == "NASK"


# ---------------------------------------------------------------------------
# _format_daily_summary
# ---------------------------------------------------------------------------

def test_format_daily_summary_empty():
    msg = _format_daily_summary([], "2026-05-21")
    assert "No applications" in msg
    assert "2026-05-21" in msg


def test_format_daily_summary_single():
    apps = [{"company": "NASK", "title": "Frontend Dev", "ats": "85%", "url": ""}]
    msg = _format_daily_summary(apps, "2026-05-21")
    assert "NASK" in msg
    assert "Frontend Dev" in msg
    assert "85%" in msg
    assert "1 total" in msg


def test_format_daily_summary_multiple():
    apps = [
        {"company": "NASK", "title": "Frontend Dev", "ats": "85%", "url": ""},
        {"company": "Sigma", "title": "Angular Dev", "ats": "78%", "url": ""},
        {"company": "Acme", "title": "JS Dev", "ats": "—", "url": ""},
    ]
    msg = _format_daily_summary(apps, "2026-05-21")
    assert "3 total" in msg
    assert "NASK" in msg
    assert "Sigma" in msg
    assert "Acme" in msg


def test_format_daily_summary_skips_dash_ats():
    apps = [{"company": "Acme", "title": "Dev", "ats": "—", "url": ""}]
    msg = _format_daily_summary(apps, "2026-05-21")
    # Dash ATS shouldn't appear as "(—)"
    assert "(—)" not in msg
    assert "(-)" not in msg


# ---------------------------------------------------------------------------
# _scheduled_daily_summary — notification logic
# ---------------------------------------------------------------------------

def test_scheduled_daily_summary_sends_when_apps_found(tmp_path, monkeypatch):
    context = MagicMock()
    context.bot = AsyncMock()

    apps = [
        {"company": "NASK", "title": "Frontend Dev", "ats": "85%", "date": "2026-05-21", "url": ""},
    ]

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=apps)):
            from hunter.telegram_bot import _scheduled_daily_summary
            await _scheduled_daily_summary(context)

    run(_run())

    context.bot.send_message.assert_called_once()
    call_kwargs = context.bot.send_message.call_args[1]
    assert "NASK" in call_kwargs["text"]


def test_scheduled_daily_summary_silent_when_no_apps(tmp_path, monkeypatch):
    context = MagicMock()
    context.bot = AsyncMock()

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=[])):
            from hunter.telegram_bot import _scheduled_daily_summary
            await _scheduled_daily_summary(context)

    run(_run())

    context.bot.send_message.assert_not_called()


def test_scheduled_daily_summary_silent_on_error():
    context = MagicMock()
    context.bot = AsyncMock()

    async def _run():
        with patch("asyncio.to_thread", side_effect=Exception("disk error")):
            from hunter.telegram_bot import _scheduled_daily_summary
            await _scheduled_daily_summary(context)

    run(_run())

    context.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_check_responses — days parameter
# ---------------------------------------------------------------------------

def test_cmd_check_responses_default_days():
    """No args → uses config default (EMAIL_RESPONSE_LOOKBACK_DAYS)."""
    update = MagicMock()
    context = MagicMock()
    context.args = []
    status_msg = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=[])):
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)

    run(_run())
    # Should succeed (no error edit)
    call_text = status_msg.edit_text.call_args[0][0]
    assert "No confirmation emails found" in call_text


def test_cmd_check_responses_custom_days():
    """Passing '60' → label shows 60 in status message."""
    update = MagicMock()
    context = MagicMock()
    context.args = ["60"]
    status_msg = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=[])):
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)

    run(_run())
    # Status message should mention 60 days
    first_call_text = update.message.reply_text.call_args[0][0]
    assert "60" in first_call_text


def test_cmd_check_responses_invalid_days_arg():
    """Non-integer arg → reply with usage hint, no gmail call."""
    update = MagicMock()
    context = MagicMock()
    context.args = ["abc"]
    update.message.reply_text = AsyncMock()

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock()) as mock_thread:
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)
            mock_thread.assert_not_called()

    run(_run())
    call_text = update.message.reply_text.call_args[0][0]
    assert "Invalid" in call_text or "invalid" in call_text


def test_cmd_check_responses_zero_days_invalid():
    """days=0 is not valid."""
    update = MagicMock()
    context = MagicMock()
    context.args = ["0"]
    update.message.reply_text = AsyncMock()

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock()) as mock_thread:
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)
            mock_thread.assert_not_called()

    run(_run())
