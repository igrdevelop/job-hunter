"""commands/health.py — /health command: per-source scraper health report."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _fmt_ts(ts: str | None) -> str:
    """Render an ISO timestamp as 'MM-DD HH:MM' (UTC), or '—'."""
    if not ts:
        return "—"
    # ts is like '2026-06-12T08:13:00+00:00'
    return ts[5:16].replace("T", " ")


def _build_report() -> str:
    """Build the /health text from recorded source runs. Sync — call in a thread."""
    from hunter.source_health import health_report
    from hunter.sources import ALL_SOURCES

    names = [s.name for s in ALL_SOURCES]
    rows = health_report(names)

    broken = [h for h in rows if h.status in ("BROKEN?", "ERROR")]
    idle = [h for h in rows if h.status == "IDLE"]
    ok = [h for h in rows if h.status == "OK"]
    nodata = [h for h in rows if h.status == "NODATA"]

    lines = ["🩺 <b>Scraper health</b>"]
    if broken:
        lines.append("\n<b>--- Needs attention ---</b>")
        for h in broken:
            extra = (
                f" (0× last {h.zero_streak}, avg {h.avg_yield})"
                if h.status == "BROKEN?"
                else " (last run errored)"
            )
            lines.append(f"  {h.icon} <b>{h.source}</b>{extra} · {_fmt_ts(h.last_ts)}")

    lines.append("\n<b>--- Healthy ---</b>")
    for h in ok:
        lines.append(f"  {h.icon} {h.source}: last <b>{h.last_yield}</b> · avg {h.avg_yield}")

    if idle:
        lines.append("\n<b>--- Idle (0, no history of jobs) ---</b>")
        lines.append("  " + ", ".join(h.source for h in idle))

    if nodata:
        lines.append("\n<b>--- No data yet ---</b>")
        lines.append("  " + ", ".join(h.source for h in nodata))

    lines.append(
        f"\n<i>{len(ok)} OK · {len(broken)} need attention · "
        f"{len(idle)} idle · {len(nodata)} no data</i>"
    )
    return "\n".join(lines)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show per-source yield health: which scrapers are producing, idle, or broken."""
    try:
        text = await asyncio.to_thread(_build_report)
    except Exception as e:  # noqa: BLE001
        logger.exception("[/health] failed to build report")
        text = f"❌ Could not build health report: {str(e)[:200]}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
