"""schedules/check_expired.py — nightly expired check job callback.

`run_expired_check_and_report` is shared with the site's /pipeline control
bar (hunter/schedules/bot_commands.py, kind `check_expired`): same check,
same Telegram report — the web command just reports even when nothing
expired, since a human pressed the button and is waiting for an answer.
"""

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# One expired check at a time per process: the nightly job and the web
# command both land here, and two concurrent run_check passes would fetch
# every unsent URL twice and send two reports.
_running = False


class ExpiredCheckBusy(RuntimeError):
    """Another expired check is already running in this process."""


def is_running() -> bool:
    return _running


@contextlib.contextmanager
def exclusive() -> Iterator[None]:
    """Hold the per-process expired-check guard for the block, or raise
    ExpiredCheckBusy. Every entry point takes it: the nightly job, the web
    command and the /check_expired Telegram command."""
    global _running
    if _running:
        raise ExpiredCheckBusy("check_expired already running")
    _running = True
    try:
        yield
    finally:
        _running = False


async def run_expired_check_and_report(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    header: str = "🌙 <b>Nightly expired check</b>\n",
    report_when_nothing_expired: bool = False,
) -> dict[str, Any]:
    """Run hunter.expired_marker.run_check and send the Telegram summary.

    Raises ExpiredCheckBusy when another check is already running, and
    whatever run_check raises — the caller decides how to report a failure.
    Returns run_check's result dict.
    """
    with exclusive():
        return await _run_and_report(
            context, header=header, report_when_nothing_expired=report_when_nothing_expired
        )


async def _run_and_report(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    header: str,
    report_when_nothing_expired: bool,
) -> dict[str, Any]:
    from hunter.expired_marker import run_check

    result = await run_check()

    expired = result["expired"]
    skipped = result.get("skipped", [])
    errors = result["errors"]

    if not expired and not report_when_nothing_expired:
        logger.info("[check_expired] Nothing expired.")
        return result

    lines = [header]
    lines.append(f"⏭ Expired: <b>{len(expired)}</b>")
    for item in expired:
        lines.append(f"  • {item['company']} — {item['title']}")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): {len(skipped)}")
    if errors:
        lines.append(f"⚠️ Errors: {len(errors)}")
    if expired:
        lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )
    return result


async def scheduled_check_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily scheduled expired check — runs at midnight, marks EXPIRED in tracker.xlsx."""
    logger.info("[scheduled_check_expired] Starting daily expired check")

    try:
        await run_expired_check_and_report(context)
    except ExpiredCheckBusy:
        logger.info("[scheduled_check_expired] Skipped — a web-triggered check is running")
    except Exception as e:
        logger.exception("[scheduled_check_expired] run_check failed: %s", e)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ <b>Scheduled check_expired failed</b>\n<pre>{str(e)[:300]}</pre>",
            parse_mode=ParseMode.HTML,
        )
