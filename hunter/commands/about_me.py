"""commands/about_me.py — /about_me command handler."""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from hunter.config import PROJECT_DIR


async def cmd_about_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate (or regenerate) About Me for a job URL in the tracker.

    Usage: /about_me <lang> <url>
    lang: en | pl
    """
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /about_me <lang> <url>\nExample: /about_me pl https://justjoin.it/job-offer/..."
        )
        return

    lang, url = args[0].lower(), args[1]
    if lang not in ("en", "pl"):
        await update.message.reply_text("lang must be 'en' or 'pl'")
        return

    from hunter.tracker import get_folder_by_url, normalize_url

    normalized = normalize_url(url)
    folder_str = get_folder_by_url(normalized)
    if not folder_str:
        await update.message.reply_text("URL not found in tracker. Run /force to process it first.")
        return

    folder_path = PROJECT_DIR / folder_str
    if not (folder_path / "job_posting.txt").exists():
        await update.message.reply_text("No job_posting.txt in folder - cannot generate.")
        return

    await update.message.reply_text(f"⏳ Generating About Me ({lang.upper()})...")

    from hunter.about_me_agent import generate_about_me

    result = await asyncio.to_thread(generate_about_me, folder_path, lang)
    if not result:
        await update.message.reply_text("❌ Generation failed - check logs.")
        return

    await update.message.reply_text(result)
    await update.message.reply_text(f"✅ Saved to {folder_str}/About_Me_{lang.upper()}.txt")
