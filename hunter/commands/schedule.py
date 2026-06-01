"""commands/schedule.py — /schedule command handler."""

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.bot.formatters import _build_schedule_text


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_build_schedule_text(), parse_mode=ParseMode.HTML)
