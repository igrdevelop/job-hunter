"""commands/retry_reset.py — /retry_reset: revive FAIL rows that gave up.

A FAIL row whose fail_count reached MAX_FAIL_RETRIES is permanently dropped
from get_failed_jobs() — nothing in the normal flow ever resets the counter
(M3, docs/LLM_OUTAGE_RESILIENCE_PLAN.md). This command is the recovery lever:

    /retry_reset            report the gave-up rows (no change)
    /retry_reset all        reset fail_count on every FAIL row
    /retry_reset <url>      reset one row

Report-first default is deliberate: a reset re-queues real LLM spend on the
next RETRY_FAILED_TIMES slot, so the no-arg form never mutates anything.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Keep the report comfortably under Telegram's 4096-char message limit.
_MAX_REPORT_ROWS = 15


def _build_report() -> str:
    """No-arg form: list gave-up rows, mutate nothing."""
    from hunter.tracker import MAX_FAIL_RETRIES, get_gave_up_failed

    rows = get_gave_up_failed()
    if not rows:
        return (
            "✅ No gave-up FAIL rows — every FAIL row is still inside the "
            f"{MAX_FAIL_RETRIES}-attempt retry budget."
        )

    lines = [
        f"🪦 <b>{len(rows)} FAIL row(s) gave up</b> "
        f"(fail_count ≥ {MAX_FAIL_RETRIES} — never retried again):",
        "",
    ]
    for r in rows[:_MAX_REPORT_ROWS]:
        company = html.escape(str(r.get("company") or "?"))
        title = html.escape(str(r.get("title") or "?")[:60])
        lines.append(f"  • {company} — {title} ({r.get('fail_count')}×)")
    if len(rows) > _MAX_REPORT_ROWS:
        lines.append(f"  … and {len(rows) - _MAX_REPORT_ROWS} more")
    lines.append(
        "\n<code>/retry_reset all</code> — reset all counters "
        "(re-queues them for the next scheduled retry slot)\n"
        "<code>/retry_reset URL</code> — reset one row"
    )
    return "\n".join(lines)


def _reset(urls: list[str] | None) -> str:
    from hunter.tracker import reset_fail_counts

    changed = reset_fail_counts(urls)
    if not changed:
        target = "that URL" if urls else "any FAIL row"
        return f"ℹ️ Nothing to reset — no non-zero fail_count on {target}."
    return (
        f"🔄 Reset fail_count on <b>{changed}</b> FAIL row(s).\n"
        "They re-enter the retry loop on the next scheduled slot "
        "(RETRY_FAILED_TIMES). Link-rotted postings will resolve as clean "
        "EXPIRED; live ones get fresh apply attempts (real LLM spend)."
    )


async def cmd_retry_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report gave-up FAIL rows; with `all` or a URL, reset their fail_count."""
    args = list(context.args or [])
    try:
        if not args:
            text = await asyncio.to_thread(_build_report)
        elif args[0].lower() == "all":
            text = await asyncio.to_thread(_reset, None)
        else:
            text = await asyncio.to_thread(_reset, [args[0]])
    except Exception as e:  # noqa: BLE001
        logger.exception("[/retry_reset] failed")
        text = f"❌ retry_reset failed: {str(e)[:200]}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
