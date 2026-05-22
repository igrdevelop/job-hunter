"""Tests for /check_responses Telegram command handler and scheduled job.

Uses unittest.mock to avoid touching real Telegram API or Gmail.
Async handlers are tested via asyncio.run() wrappers (no pytest-asyncio dependency).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hunter.email_response_checker import ConfirmationEmail, MatchResult
from hunter.telegram_bot import _format_check_responses_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _confirmed_result(company="NASK", title="Senior Frontend Developer",
                      platform="erecruiter", existing_response=""):
    """MatchResult for a confirmed email (response was empty before run)."""
    email = ConfirmationEmail(
        company=company, title=title,
        date="2026-05-20", subject="...", platform=platform,
    )
    candidate = {
        "row": 2, "company": company, "title": title,
        "ats": "85%", "sent": "", "url": "https://example.com/1",
        "response": existing_response,
        "title_score": 1.0,
    }
    return MatchResult(email=email, match_type="exact",
                       candidates=[candidate], row_num=2)


def _ambiguous_result(company="Acme", title="Angular Dev"):
    email = ConfirmationEmail(
        company=company, title=title,
        date="2026-05-20", subject="...", platform="erecruiter",
    )
    candidates = [
        {"row": 2, "company": company, "title": "Angular Developer",
         "ats": "85%", "sent": "", "url": "", "response": "", "title_score": 0.8},
        {"row": 3, "company": company, "title": "Angular Engineer",
         "ats": "85%", "sent": "", "url": "", "response": "", "title_score": 0.75},
    ]
    return MatchResult(email=email, match_type="ambiguous", candidates=candidates)


def _no_match_result(company="Unknown Corp", title=""):
    email = ConfirmationEmail(
        company=company, title=title,
        date="2026-05-20", subject="...", platform="direct",
    )
    return MatchResult(email=email, match_type="no_match")


# ---------------------------------------------------------------------------
# _format_check_responses_report
# ---------------------------------------------------------------------------

def test_format_empty_results():
    msg = _format_check_responses_report([])
    assert "No confirmation emails found" in msg


def test_format_confirmed_only():
    results = [_confirmed_result("NASK", "Senior Frontend Developer", "erecruiter")]
    msg = _format_check_responses_report(results)
    assert "NASK" in msg
    assert "Senior Frontend Developer" in msg
    assert "erecruiter" in msg
    assert "✅" in msg


def test_format_ambiguous_shows_candidates():
    results = [_ambiguous_result("Acme", "Angular Dev")]
    msg = _format_check_responses_report(results)
    assert "❓" in msg
    assert "Acme" in msg
    assert "Angular Developer" in msg   # candidate title
    assert "Angular Engineer" in msg    # second candidate


def test_format_no_match():
    results = [_no_match_result("Unknown Corp")]
    msg = _format_check_responses_report(results)
    assert "📭" in msg
    assert "Unknown Corp" in msg


def test_format_all_groups():
    results = [
        _confirmed_result("NASK", "Senior Frontend Developer"),
        _ambiguous_result("Acme", "Angular Dev"),
        _no_match_result("Unknown Corp"),
    ]
    msg = _format_check_responses_report(results)
    assert "✅" in msg
    assert "❓" in msg
    assert "📭" in msg


def test_format_confirmed_without_row_num_excluded():
    """Results with no row_num (ambiguous resolved to no_match) not in confirmed section."""
    email = ConfirmationEmail(company="X", title="Y", date="2026-05-20",
                              subject="...", platform="direct")
    result = MatchResult(email=email, match_type="fuzzy",
                         candidates=[{"row": 2, "company": "X", "title": "Y",
                                      "ats": "85%", "sent": "", "url": "",
                                      "response": "", "title_score": 0.8}],
                         row_num=None)  # no row_num → excluded from confirmed
    msg = _format_check_responses_report([result])
    # Should not appear in confirmed section since row_num is None
    assert "✅" not in msg


# ---------------------------------------------------------------------------
# cmd_check_responses — success path
# ---------------------------------------------------------------------------

def test_cmd_check_responses_success():
    update = MagicMock()
    context = MagicMock()
    status_msg = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    results = [_confirmed_result("NASK", "Senior Frontend Developer")]

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=results)):
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)

    run(_run())

    update.message.reply_text.assert_called_once()
    status_msg.edit_text.assert_called_once()
    call_text = status_msg.edit_text.call_args[0][0]
    assert "NASK" in call_text


def test_cmd_check_responses_no_gmail_token():
    update = MagicMock()
    context = MagicMock()
    status_msg = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    async def _run():
        with patch("asyncio.to_thread", side_effect=FileNotFoundError("gmail_token.json")):
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)

    run(_run())

    call_text = status_msg.edit_text.call_args[0][0]
    assert "Gmail not configured" in call_text
    assert "gmail_auth" in call_text


def test_cmd_check_responses_unexpected_error():
    update = MagicMock()
    context = MagicMock()
    status_msg = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)

    async def _run():
        with patch("asyncio.to_thread", side_effect=Exception("network error")):
            from hunter.telegram_bot import cmd_check_responses
            await cmd_check_responses(update, context)

    run(_run())

    call_text = status_msg.edit_text.call_args[0][0]
    assert "❌" in call_text
    assert "network error" in call_text


# ---------------------------------------------------------------------------
# _scheduled_check_email_responses — notification logic
# ---------------------------------------------------------------------------

def test_scheduled_silent_when_no_new_confirmed():
    """Scheduler sends nothing when all matches were already confirmed."""
    context = MagicMock()
    context.bot = AsyncMock()

    # existing_response="CONFIRMED" → was already confirmed → not newly written
    results = [_confirmed_result("NASK", "Senior Frontend Developer",
                                 existing_response="CONFIRMED")]

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=results)):
            from hunter.telegram_bot import _scheduled_check_email_responses
            await _scheduled_check_email_responses(context)

    run(_run())

    context.bot.send_message.assert_not_called()


def test_scheduled_notifies_when_newly_confirmed():
    """Scheduler sends message when a row was just confirmed (response was empty)."""
    context = MagicMock()
    context.bot = AsyncMock()

    results = [_confirmed_result("NASK", "Senior Frontend Developer",
                                 existing_response="")]  # was empty → newly written

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=results)):
            from hunter.telegram_bot import _scheduled_check_email_responses
            await _scheduled_check_email_responses(context)

    run(_run())

    context.bot.send_message.assert_called_once()
    call_kwargs = context.bot.send_message.call_args[1]
    assert "NASK" in call_kwargs["text"]


def test_scheduled_silent_on_missing_token():
    context = MagicMock()
    context.bot = AsyncMock()

    async def _run():
        with patch("asyncio.to_thread",
                   side_effect=FileNotFoundError("gmail_token.json")):
            from hunter.telegram_bot import _scheduled_check_email_responses
            await _scheduled_check_email_responses(context)

    run(_run())

    context.bot.send_message.assert_not_called()


def test_scheduled_silent_on_error():
    context = MagicMock()
    context.bot = AsyncMock()

    async def _run():
        with patch("asyncio.to_thread", side_effect=Exception("network error")):
            from hunter.telegram_bot import _scheduled_check_email_responses
            await _scheduled_check_email_responses(context)

    run(_run())

    context.bot.send_message.assert_not_called()


def test_scheduled_silent_when_only_ambiguous():
    context = MagicMock()
    context.bot = AsyncMock()

    results = [_ambiguous_result("Acme", "Angular Dev")]

    async def _run():
        with patch("asyncio.to_thread", new=AsyncMock(return_value=results)):
            from hunter.telegram_bot import _scheduled_check_email_responses
            await _scheduled_check_email_responses(context)

    run(_run())

    context.bot.send_message.assert_not_called()
