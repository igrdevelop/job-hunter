"""commands/funnel.py — /funnel command: application funnel analytics.

Usage:
    /funnel            all-time funnel (overall + per source)
    /funnel 30         only applications from the last 30 days
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_MAX_SOURCE_ROWS = 25


def _parse_days(args: list[str]) -> int | None:
    """First positional integer arg → day window, else None (all-time)."""
    for a in args or []:
        if a.isdigit() and int(a) > 0:
            return int(a)
    return None


def _build_report(days: int | None) -> str:
    from hunter.funnel import compute_funnel

    rep = compute_funnel(days=days)
    o = rep.overall
    window = f"last {days}d" if days else "all-time"

    lines = [
        f"📊 <b>Application funnel</b> ({window})",
        "",
        f"  Tracked:    <b>{o.tracked}</b>",
        f"  Generated:  <b>{o.generated}</b>",
        f"  Sent:       <b>{o.sent}</b>  ({o.sent_rate}% of generated)",
        f"  Confirmed:  <b>{o.confirmed}</b>  ({o.confirm_rate}% of sent)",
        f"  Answered:   <b>{o.answered}</b>  ({o.answer_rate}% of sent)",
    ]

    top = rep.top_sources(_MAX_SOURCE_ROWS)
    active = [(name, c) for name, c in top if c.tracked]
    if active:
        lines.append("\n<b>--- By source (tracked / gen / sent / conf / ans) ---</b>")
        for name, c in active:
            lines.append(
                f"  {name}: {c.tracked} / {c.generated} / <b>{c.sent}</b> / "
                f"{c.confirmed} / {c.answered}"
            )

    lines.append(
        "\n<i>Generated = CV built · Sent = submitted · "
        "Confirmed = ATS ack · Answered = human reply</i>"
    )
    return "\n".join(lines)


async def cmd_funnel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the application funnel: tracked → generated → sent → responded."""
    days = _parse_days(context.args if hasattr(context, "args") else [])
    try:
        text = await asyncio.to_thread(_build_report, days)
    except Exception as e:  # noqa: BLE001
        logger.exception("[/funnel] failed to build report")
        text = f"❌ Could not build funnel report: {str(e)[:200]}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
