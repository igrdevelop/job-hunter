"""commands/url_message.py — URL/text message handler + Apply/Skip button callbacks."""

import asyncio
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.models import Job
from hunter.tracker import add_skipped, lookup_url
from hunter.bot.state import _pending_jobs, _force_waiting
from hunter.bot.apply_runner import _run_apply_agent, _run_linkedin_batch, _handle_paste
from hunter.bot.paste import _looks_like_paste, _extract_url
from hunter.commands.force import _force_run

logger = logging.getLogger(__name__)


# ── Callback handler (Apply / Skip buttons) ──────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, job_id = data.split(":", 1)
    job: Optional[Job] = _pending_jobs.get(job_id)

    if not job:
        await query.edit_message_text(
            query.message.text + "\n\n⚠️ Expired — restart bot and run /hunt again.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "skip":
        await _handle_skip(query, job, job_id)
    elif action == "apply":
        await _handle_apply(query, job, job_id, context)


async def _handle_skip(query, job: Job, job_id: str) -> None:
    row = await asyncio.to_thread(add_skipped, job)
    _pending_jobs.pop(job_id, None)
    if row:
        try:
            from hunter.tracker_cache import cache
            await cache.add(row)
            from hunter import gsheets_sync
            await gsheets_sync.mirror_new_row(row)
        except Exception as _e:
            logger.warning("[skip] cache/gsheets update failed: %s", _e)

    original = query.message.text
    await query.edit_message_text(
        original + "\n\n❌ <i>Skipped</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )
    logger.info(f"[Skip] {job.company} — {job.title}")


async def _handle_apply(query, job: Job, job_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    _pending_jobs.pop(job_id, None)

    original = query.message.text
    await query.edit_message_text(
        original + "\n\n⏳ <i>Generating documents...</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )

    # linkedin_scout_relay jobs have no real fetchable URL (no LinkedIn feed
    # post permalink survives without extra clicking — see
    # hunter/sources/linkedin_scout_relay.py). Route through the paste flow
    # instead, using the raw post text saved by the queue relay.
    post_text = (job.raw or {}).get("post_text") if job.source == "linkedin_scout_relay" else None
    if post_text:
        paste_path = _write_paste_temp_file(post_text)
        if paste_path is None:
            await query.message.reply_text(
                "❌ Failed to save the scouted post text to a temp file — apply aborted.",
            )
            return
        logger.info(f"[Apply] Launching apply_agent (paste mode, linkedin_scout) for: {job.url}")
        asyncio.create_task(_run_apply_agent(job.url, paste_file=paste_path))
        return

    logger.info(f"[Apply] Launching apply_agent for: {job.url}")

    # Run apply_agent.py as a detached subprocess so bot stays responsive
    # apply_agent.py will send its own Telegram notification when done
    asyncio.create_task(_run_apply_agent(job.url))


def _write_paste_temp_file(text: str) -> Optional[str]:
    """Save `text` to a temp file for apply_agent's --paste-file flow.

    Mirrors bot/apply_runner.py::_handle_paste's temp-file mechanics — kept
    separate since that helper is driven by an `Update` (chat message reply),
    not a button callback.
    """
    import tempfile

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", prefix="li_scout_paste_", delete=False,
        )
        with tmp as fh:
            fh.write(text)
        return tmp.name
    except OSError as e:
        logger.exception("[Apply] failed to write linkedin_scout paste temp file: %s", e)
        return None


# ── URL message handler ───────────────────────────────────────────────────────

async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages.

    - If chat is in _force_waiting state → treat as force URL/text input
    - Long pasted job text (>= _PASTE_TEXT_MIN_LEN, with or without URL) → paste flow
    - Single job URL (JustJoin, NoFluffJobs, LinkedIn /jobs/view/...) → apply_agent
    - LinkedIn search / alert URL (/jobs/search?...) → extract job ids → batch apply
    """
    from hunter.config import MAX_JOBS_PER_RUN

    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # Force two-step: user replied after bare /force
    if chat_id in _force_waiting:
        _force_waiting.discard(chat_id)
        url = _extract_url(text) if text.startswith("http") else None
        await _force_run(update, url=url, body=text)
        return

    # Paste-mode branch: user forwarded/pasted the posting text itself.
    if _looks_like_paste(text):
        n = len(text.strip())
        await update.message.reply_text(
            f"📥 <b>Job posting received</b> — {n} chars. Saving and checking tracker…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await _handle_paste(update, text)
        return

    if not text.startswith("http"):
        await update.message.reply_text(
            "ℹ️ Send a job URL (starting with http) to generate docs.\n"
            "Or paste the full job posting text (with or without a URL) — "
            "it will be processed directly.\n\n"
            "You can also send a LinkedIn alert URL — all job ids will be extracted.",
            parse_mode=ParseMode.HTML,
        )
        return

    from hunter.sources.linkedin import is_linkedin_search, parse_linkedin_job_ids, normalize_linkedin_url

    # Normalize LinkedIn view URLs — strip tracking params (?trk=...&refId=...)
    text = normalize_linkedin_url(text)

    if is_linkedin_search(text):
        job_ids = parse_linkedin_job_ids(text)
        if not job_ids:
            await update.message.reply_text(
                "⚠️ LinkedIn URL recognised but no job ids found.\n"
                "Try sending a direct link to a specific vacancy.",
                parse_mode=ParseMode.HTML,
            )
            return

        capped = job_ids[:MAX_JOBS_PER_RUN]
        skipped = len(job_ids) - len(capped)

        msg = (
            f"🔗 <b>LinkedIn alert</b>: found <b>{len(job_ids)}</b> jobs\n"
            + (f"⚠️ Processing first {MAX_JOBS_PER_RUN} (MAX_JOBS_PER_RUN)\n" if skipped else "")
            + "Starting sequentially…"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        logger.info(f"[URL handler] LinkedIn batch: {len(capped)} jobs from alert")

        asyncio.create_task(_run_linkedin_batch(capped, update))
        return

    # Single job URL — check tracker first
    entries = await asyncio.to_thread(lookup_url, text)
    if entries:
        only_manual = all(str(e.get("ats") or "").strip().upper() == "MANUAL" for e in entries)
        if only_manual:
            from hunter.sources.jobleads import try_load_manual_job_posting
            manual_content = await asyncio.to_thread(try_load_manual_job_posting, text)
            if manual_content:
                await update.message.reply_text(
                    f"✅ <b>Job posting found in file — starting generation…</b>\n"
                    f"🔗 {text}\n\nEstimated 1–2 minutes.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                logger.info(f"[URL handler] MANUAL row with ready file, launching apply_agent: {text}")
                asyncio.create_task(_run_apply_agent(text))
                return
            else:
                e = entries[-1]
                folder_info = f'\n📁 <code>{e["folder"]}</code>' if e.get("folder") else ""
                await update.message.reply_text(
                    f"📝 <b>Vacancy waiting for text (MANUAL)</b>\n\n"
                    f"  ID {e['id']}: <b>{e['company']}</b> - {e['title']}{folder_info}\n\n"
                    f"Paste the full job text below the marker in <code>job_posting.txt</code> and send this URL again.\n"
                    f"Or send the job text here (with or without the URL) — it will be processed immediately.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return

        lines = []
        for e in entries:
            sent_info = f' | Sent: {e["sent"]}' if e["sent"] else ""
            folder_info = f'\n    Folder: <code>{e["folder"]}</code>' if e["folder"] else ""
            lines.append(
                f'  ID {e["id"]}: <b>{e["company"]}</b> - {e["title"]}\n'
                f'    ATS: {e["ats"]}{sent_info}{folder_info}'
            )
        detail = "\n".join(lines)
        await update.message.reply_text(
            f"⚠️ <b>This vacancy is already in the tracker!</b>\n\n"
            f"{detail}\n\n"
            f"Send /force {text}\nto process it again.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text(
        f"⏳ <b>Starting generation…</b>\n🔗 {text}\n\nEstimated 1–2 minutes.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(f"[URL handler] Launching apply_agent for: {text}")
    asyncio.create_task(_run_apply_agent(text))
