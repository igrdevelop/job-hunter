"""commands/process_manual.py — /process_manual command handler."""

import asyncio
import logging
import sys

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import APPLY_AGENT_PATH
from hunter.bot.state import _APPLY_AGENT_TIMEOUT

logger = logging.getLogger(__name__)


async def cmd_process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process all MANUAL-pending tracker rows whose job_posting.txt is already filled."""
    from hunter.tracker import get_all_manual_pending
    from hunter.sources.jobleads import try_load_manual_job_posting

    rows = await asyncio.to_thread(get_all_manual_pending)
    if not rows:
        await update.message.reply_text("✅ No MANUAL vacancies to process.")
        return

    ready = []
    for row in rows:
        content = await asyncio.to_thread(try_load_manual_job_posting, row["url"])
        if content:
            ready.append(row)

    if not ready:
        lines = [
            f"  Row {r['row']}: <b>{r['company']}</b> - {r['title']}"
            + (f"\n    📁 <code>{r['folder']}</code>" if r.get("folder") else "")
            for r in rows
        ]
        await update.message.reply_text(
            f"📝 <b>Found {len(rows)} MANUAL vacancies, none ready.</b>\n\n"
            + "\n".join(lines)
            + "\n\nAdd the job text below the marker in <code>job_posting.txt</code> and retry.",
            parse_mode=ParseMode.HTML,
        )
        return

    not_ready_count = len(rows) - len(ready)
    note = f" ({not_ready_count} waiting for text)" if not_ready_count else ""
    await update.message.reply_text(
        f"🚀 <b>Processing {len(ready)} ready vacancies{note}…</b>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("[process_manual] Processing %d ready MANUAL rows", len(ready))

    ok = failed = 0
    total = len(ready)
    for i, row in enumerate(ready, 1):
        url = row["url"]
        try:
            await update.message.reply_text(
                f"⏳ [{i}/{total}] <b>{row['company']}</b> — {row['title']}\n🔗 {url}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(APPLY_AGENT_PATH),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_APPLY_AGENT_TIMEOUT)

        if proc.returncode == 0:
            ok += 1
            logger.info("[process_manual] OK: %s", url)
        else:
            failed += 1
            logger.error(
                "[process_manual] FAIL: %s\n%s",
                url, stderr.decode(errors="replace")[-300:],
            )

    await update.message.reply_text(
        f"🏁 <b>process_manual done</b>\n✅ {ok} / ❌ {failed} / Total: {total}",
        parse_mode=ParseMode.HTML,
    )
