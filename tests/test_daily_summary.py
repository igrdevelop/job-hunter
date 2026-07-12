"""Tests for daily applications summary: get_applications_on_date,
_format_daily_summary, _scheduled_daily_summary, and updated
cmd_check_responses with days parameter.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch


from hunter import tracker
from hunter.telegram_bot import _format_daily_summary
from hunter.db import get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def _insert(
    tracker_db, *, date: str, company: str, title: str, ats: str = "85%", url: str = ""
) -> None:
    if not url:
        url = f"https://example.com/{uuid.uuid4().hex[:6]}"
    norm = tracker.normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex[:8], date, company, title, ats, url, norm),
        )


# ---------------------------------------------------------------------------
# get_applications_on_date
# ---------------------------------------------------------------------------


def test_get_applications_returns_matching_rows(tracker_db):
    _insert(tracker_db, date="2026-05-21", company="NASK", title="Frontend Dev")
    _insert(tracker_db, date="2026-05-20", company="Acme", title="Angular Dev")
    _insert(tracker_db, date="2026-05-21", company="Sigma", title="JS Dev")
    result = tracker.get_applications_on_date("2026-05-21")
    assert len(result) == 2
    companies = {r["company"] for r in result}
    assert companies == {"NASK", "Sigma"}


def test_get_applications_empty_when_no_match(tracker_db):
    _insert(tracker_db, date="2026-05-20", company="Acme", title="Angular Dev")
    assert tracker.get_applications_on_date("2026-05-21") == []


def test_get_applications_empty_when_no_tracker(tracker_db):
    # Empty DB → empty result
    assert tracker.get_applications_on_date("2026-05-21") == []


def test_get_applications_result_fields(tracker_db):
    _insert(
        tracker_db,
        date="2026-05-21",
        company="NASK",
        title="Senior Frontend Developer",
        ats="91%",
        url="https://nask.pl/job/1",
    )
    result = tracker.get_applications_on_date("2026-05-21")
    assert len(result) == 1
    r = result[0]
    assert r["company"] == "NASK"
    assert r["title"] == "Senior Frontend Developer"
    assert r["ats"] == "91%"
    assert r["url"] == "https://nask.pl/job/1"
    assert r["date"] == "2026-05-21"


def test_get_applications_skips_rows_without_company(tracker_db):
    # Insert a row with empty company directly (tracker API skips empty company in daily query)
    norm = tracker.normalize_url("https://example.com/nocompany")
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm) "
            "VALUES (?, '2026-05-21', '', 'Some Dev', '85%', 'https://example.com/nocompany', ?)",
            (uuid.uuid4().hex[:8], norm),
        )
    _insert(tracker_db, date="2026-05-21", company="NASK", title="Frontend Dev")
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
    assert "(—)" not in msg
    assert "(-)" not in msg


# ---------------------------------------------------------------------------
# _scheduled_daily_summary — notification logic
# ---------------------------------------------------------------------------


def test_scheduled_daily_summary_sends_when_apps_found():
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


def test_scheduled_daily_summary_silent_when_no_apps():
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
    call_text = status_msg.edit_text.call_args[0][0]
    assert "No confirmation emails found" in call_text


def test_cmd_check_responses_custom_days():
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
    first_call_text = update.message.reply_text.call_args[0][0]
    assert "60" in first_call_text


def test_cmd_check_responses_invalid_days_arg():
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
