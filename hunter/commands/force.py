"""commands/force.py — /force command handler + helpers."""

import asyncio
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import PROJECT_DIR
from hunter.bot.state import _force_waiting
from hunter.bot.paste import _looks_like_paste, _extract_url
from hunter.bot.apply_runner import _run_apply_agent, _handle_paste

logger = logging.getLogger(__name__)


async def _force_cleanup(url: str, update: Update) -> str:
    """Delete old Drive folder, server folder, and tracker rows for this URL.

    Returns a human-readable HTML summary of what was deleted (best-effort).
    """
    import shutil
    from hunter.tracker import delete_all_by_url
    from hunter.tracker_cache import cache

    lines: list[str] = []

    # 1. Delete stale Sheets row FIRST — while the tracker row still exists.
    # delete_row_by_url reads sheets_row from the DB via lookup_url; if we
    # delete from the DB first the lookup returns nothing and Sheets is never cleaned.
    try:
        from hunter import gsheets_sync

        sheets_deleted = await gsheets_sync.delete_row_by_url(url)
        if sheets_deleted:
            lines.append("🗑 Sheets: old row deleted")
        else:
            lines.append("ℹ️ Sheets: row not in Sheets (or Sheets disabled)")
    except Exception as e:
        lines.append(f"⚠️ Sheets row delete failed: <code>{e}</code>")
        logger.warning("[force_cleanup] gsheets delete_row_by_url failed: %s", e)

    # 2. Delete from tracker DB (also retrieves folder + drive_url for steps below)
    tracker_result = await asyncio.to_thread(delete_all_by_url, url)
    deleted_rows = tracker_result.get("deleted", 0)
    folder_str = tracker_result.get("folder") or ""
    drive_url = tracker_result.get("drive_url") or ""

    if deleted_rows:
        lines.append(f"🗑 Tracker: removed {deleted_rows} row(s)")
    else:
        lines.append("ℹ️ Tracker: no existing rows found")

    # 3. Invalidate in-memory cache
    try:
        await cache.invalidate_url(url)
    except Exception as e:
        logger.warning("[force_cleanup] cache invalidate failed: %s", e)

    # 4. Delete server folder
    if folder_str:
        folder_path = Path(folder_str)
        if not folder_path.is_absolute():
            folder_path = PROJECT_DIR / folder_str
        if folder_path.exists() and folder_path.is_dir():
            try:
                shutil.rmtree(folder_path)
                lines.append(f"🗑 Server: deleted <code>{folder_path.name}</code>")
                logger.info("[force_cleanup] Deleted server folder: %s", folder_path)
            except Exception as e:
                lines.append(f"⚠️ Server folder delete failed: <code>{e}</code>")
                logger.warning("[force_cleanup] rmtree failed for %s: %s", folder_path, e)
        else:
            lines.append(f"ℹ️ Server folder not found: <code>{folder_str}</code>")

    # 5. Delete Google Drive folder
    if drive_url and drive_url not in ("-", "—"):
        try:
            from hunter import gdrive_sync

            deleted = await gdrive_sync.delete_application_folder(drive_url)
            if deleted:
                lines.append("🗑 Drive: folder deleted")
            else:
                lines.append("ℹ️ Drive: folder not found or GDRIVE disabled")
        except Exception as e:
            lines.append(f"⚠️ Drive delete failed: <code>{e}</code>")
            logger.warning("[force_cleanup] gdrive delete failed: %s", e)
    else:
        lines.append("ℹ️ Drive: no folder URL in tracker")

    return "\n".join(lines)


async def _force_run(update: Update, url: str | None, body: str) -> None:
    """Core force logic: cleanup existing entry then launch apply_agent.

    Called from cmd_force (inline args) and from cmd_url (_force_waiting path).
    """
    if body and _looks_like_paste(body):
        await update.message.reply_text(
            f"🔧 <b>Force + job text</b> — {len(body.strip())} chars.\n"
            "Bypasses: tracker dedup, React-only. Starting…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info("[Force] paste mode (%d chars)", len(body))
        await _handle_paste(update, body, force=True)
        return

    if not url:
        url = _extract_url(body) or (body.split()[0].strip() if body else None)

    if not url or not url.startswith("http"):
        await update.message.reply_text(
            "⚠️ Provide an <b>http(s) URL</b> or full job posting text.\n"
            "A single word without a URL is not valid.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"🔍 <b>Force: checking for existing entry…</b>\n🔗 {url}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    cleanup_summary = await _force_cleanup(url, update)

    await update.message.reply_text(
        f"<b>Cleanup done:</b>\n{cleanup_summary}\n\n"
        f"⏳ Starting generation (<code>--force</code>)…",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info("[Force] Launching apply_agent --force for: %s", url)
    asyncio.create_task(_run_apply_agent(url, force=True))


async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-process a URL: two-step (bare /force → ask for URL) or inline (/force <url>)."""
    raw = (update.message.text or "").strip()
    m = re.match(r"/force(?:@\w+)?\s*(.*)\Z", raw, flags=re.DOTALL | re.IGNORECASE)
    body = (m.group(1) or "").strip() if m else ""

    if not body:
        chat_id = update.effective_chat.id
        _force_waiting.add(chat_id)
        await update.message.reply_text(
            "🔧 <b>Force mode</b>\n\n"
            "Send a job URL or paste the full job posting text.\n\n"
            "<i>If the job is already in the tracker, old files will be deleted "
            "(Drive + server + tracker row) and regenerated from scratch.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    url = _extract_url(body) if body.startswith("http") else None
    await _force_run(update, url=url, body=body)
