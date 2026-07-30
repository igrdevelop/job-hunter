"""commands/queue.py — /queue: list PENDING jobs awaiting the apply worker.

M1 (docs/HUNT_APPLY_SPLIT_PLAN.md). Read-only — never mutates anything.
Meaningful only when APPLY_QUEUE_ENABLED=true; with the flag off the hunt
loop never writes PENDING rows, so the queue is always empty.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


def _build_report(limit: int) -> str:
    from hunter.config import APPLY_QUEUE_ENABLED
    from hunter.tracker import count_in_progress, count_pending, list_pending

    pending = count_pending()
    in_progress = count_in_progress()

    lines = [
        f"📥 <b>Apply queue</b>  |  PENDING: <b>{pending}</b>  IN_PROGRESS: <b>{in_progress}</b>"
    ]
    if not APPLY_QUEUE_ENABLED:
        lines.append(
            "<i>APPLY_QUEUE_ENABLED is off — hunts apply inline, this queue stays empty.</i>"
        )
        return "\n".join(lines)

    if pending == 0:
        lines.append("<i>Nothing waiting.</i>")
        return "\n".join(lines)

    rows = list_pending(limit)
    lines.append("")
    for i, r in enumerate(rows, 1):
        company = html.escape(r.get("company") or "?")
        title = html.escape((r.get("title") or "?")[:60])
        lines.append(f"{i}. <b>{company}</b> — {title}")
    if pending > len(rows):
        lines.append(f"… +{pending - len(rows)} more")
    return "\n".join(lines)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show PENDING/IN_PROGRESS counts + the oldest N (default 20, max 50) queued jobs."""
    limit = _DEFAULT_LIMIT
    args = list(context.args or [])
    if args:
        try:
            limit = max(1, min(_MAX_LIMIT, int(args[0])))
        except ValueError:
            pass
    try:
        text = await asyncio.to_thread(_build_report, limit)
    except Exception as e:  # noqa: BLE001
        logger.exception("[/queue] failed")
        text = f"❌ /queue failed: {str(e)[:200]}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
