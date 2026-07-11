"""
bot/notifications.py — Outbound Telegram messages.

Public API used by hunter/main.py:
  send_text(context, text)
  send_job_cards(context, jobs)

Internal helper used by apply_runner.py:
  _tg_notify(text)  — sends without a context (uses Bot token directly)
"""

import logging
import re

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from hunter.models import Job
from hunter.bot.keyboards import _make_keyboard
from hunter.bot.state import _pending_jobs

logger = logging.getLogger(__name__)


async def send_text(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


async def send_job_cards(context: ContextTypes.DEFAULT_TYPE, jobs: list[Job]) -> None:
    """Send one Telegram message per job with Apply/Skip buttons."""
    for job in jobs:
        jid = job.job_id()
        _pending_jobs[jid] = job
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=job.telegram_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_make_keyboard(jid),
            disable_web_page_preview=True,
        )


# Formatting tags used in bot messages — stripped for the plain-text resend.
_TAG_RE = re.compile(r"</?(?:b|i|u|s|a|code|pre)(?:\s[^<>]*)?>")


async def _tg_notify(text: str) -> None:
    """Send a message to the configured chat via bot token (no context needed)."""
    try:
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest as e:
                # HTML parse failure (unescaped content in an interpolated
                # snippet) — a plain message beats a silently lost one.
                logger.warning("[tg_notify] HTML rejected (%s) — resending plain", e)
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=_TAG_RE.sub("", text),
                    disable_web_page_preview=True,
                )
    except Exception as e:
        logger.error("[tg_notify] failed: %s", e)
