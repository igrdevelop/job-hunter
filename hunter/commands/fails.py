"""commands/fails.py — /fails [N]: last N entries from the apply-failure audit log.

Reads `logs/apply_failures.jsonl` (hunter.apply_failures_log, M4 —
docs/HUNT_APPLY_SPLIT_PLAN.md) — every non-ok, non-manual apply outcome
(fail, cli_timeout, rate_limited; llm_outage is excluded, it's global
state tracked separately). Read-only, never mutates anything.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_DEFAULT_N = 10
_MAX_N = 30

_OUTCOME_EMOJI = {
    "fail": "❌",
    "cli_timeout": "⏰",
    "rate_limited": "🚦",
}


def _build_report(n: int) -> str:
    from hunter.apply_failures_log import read_last_failures

    records = read_last_failures(n)
    if not records:
        return "✅ No entries in the apply-failure log."

    lines = [f"🪵 <b>Last {len(records)} apply failure(s)</b>:", ""]
    for r in reversed(records):  # newest first for readability
        emoji = _OUTCOME_EMOJI.get(r.get("outcome"), "⚠️")
        company = html.escape(str(r.get("company") or "?"))
        title = html.escape(str(r.get("title") or "?")[:60])
        ts = r.get("ts", "?")
        outcome = r.get("outcome", "?")
        line = f"{emoji} <code>{ts}</code> {outcome} — {company} — {title}"
        error = r.get("error")
        if error:
            line += f"\n    <i>{html.escape(str(error)[:150])}</i>"
        lines.append(line)
    return "\n".join(lines)


async def cmd_fails(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the last N (default 10, max 30) apply-failure log entries."""
    n = _DEFAULT_N
    args = list(context.args or [])
    if args:
        try:
            n = max(1, min(_MAX_N, int(args[0])))
        except ValueError:
            pass
    try:
        text = await asyncio.to_thread(_build_report, n)
    except Exception as e:  # noqa: BLE001
        logger.exception("[/fails] failed")
        text = f"❌ /fails failed: {str(e)[:200]}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
