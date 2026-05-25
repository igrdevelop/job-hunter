"""commands/status.py — /status command handler."""

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.bot.state import _pending_jobs, _active_apply_urls, _APPLY_AGENT_TIMEOUT

logger = logging.getLogger(__name__)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import _hunt_lock
    from hunter.config import AUTO_APPLY

    mode = "AUTO" if AUTO_APPLY else "MANUAL"
    hunting = "🔒 Hunt in progress" if _hunt_lock.locked() else "🔓 Idle"
    pending = len(_pending_jobs)

    lines = [
        f"🔧 Mode: <b>{mode}</b>  |  {hunting}",
        f"📋 Pending decisions: <b>{pending}</b>",
    ]

    if _active_apply_urls:
        now = datetime.now(timezone.utc)
        lines.append(f"\n⚙️ <b>Generating ({len(_active_apply_urls)}):</b>")
        for url, started in _active_apply_urls.items():
            elapsed = int((now - started).total_seconds())
            mins, secs = divmod(elapsed, 60)
            timeout_warn = " ⚠️ timeout soon" if elapsed > _APPLY_AGENT_TIMEOUT - 60 else ""
            short_url = url[:80] + "…" if len(url) > 80 else url
            lines.append(f"  • {mins}m{secs:02d}s — <code>{short_url}</code>{timeout_warn}")
    else:
        lines.append("\n💤 No active generation")

    try:
        from hunter.tracker import get_failed_jobs
        failed_count = len(await asyncio.to_thread(get_failed_jobs))
        if failed_count:
            lines.append(f"\n🔁 FAIL queue: <b>{failed_count}</b> jobs (will retry on next hunt)")
    except Exception:
        pass

    lines.append("\n<i>Use /schedule to see hunt timetable</i>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
