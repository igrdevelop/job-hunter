"""commands/start.py — /start command handler."""

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>Job Hunter Bot</b>\n\n"
        "Commands:\n"
        "/hunt [source …] - run search (all sources, or e.g. <code>/hunt arbeitnow justjoin</code>)\n"
        "/status - source schedule + bot status\n"
        "/force — force generation: <code>/force URL</code> or <code>/force</code> "
        "+ full job posting text (bypasses dedup and React-only; JobLeads: "
        "<code>job_posting.txt</code>)\n"
        "/process_manual - process MANUAL rows with filled job_posting.txt\n"
        "/sync_sent - sync Sent column from Google Sheets → tracker.xlsx\n"
        "/unsent - count unsent applications and how many have ANGULAR in stack\n"
        "/check_expired - check tracker for expired vacancies\n"
        "/gsheets_status - Google Sheets integration status\n"
        "/gsheets_push_missing - push tracker.xlsx rows missing from Sheets\n"
        "/gdrive_upload_missing - upload all tracker.xlsx folders to Google Drive\n\n"
        "Or just send a job URL to generate docs.",
        parse_mode=ParseMode.HTML,
    )
