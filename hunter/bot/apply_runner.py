"""
bot/apply_runner.py — Apply pipeline runners for the Telegram bot.

Functions:
  _run_apply_agent(url, force, paste_file)  — launches apply_agent subprocess (non-blocking)
  _run_linkedin_batch(job_ids, update)      — sequentially applies LinkedIn jobs
  _handle_paste(update, text, force)        — saves pasted text to tmpfile, triggers apply_agent
"""

import asyncio
import html
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import Update

from hunter.config import APPLY_AGENT_PATH
from hunter.bot.state import _active_apply_urls, _APPLY_AGENT_TIMEOUT
from hunter.bot.notifications import _tg_notify
from hunter.bot.paste import _extract_url

logger = logging.getLogger(__name__)


async def _run_apply_agent(
    url: str,
    force: bool = False,
    paste_file: Optional[str] = None,
    permalink: Optional[str] = None,
) -> None:
    """Run apply_agent.py via apply_service, don't block the event loop.

    If ``paste_file`` is set, URL may be empty — apply_agent will use the
    pasted text instead of fetching the URL. ``permalink``, when given, is
    the real clickable link behind a synthetic ``url`` (e.g. a captured
    LinkedIn Scout post permalink).
    """
    from hunter.services.apply_service import run_apply_agent_for_url

    label = url or "(pasted text)"
    if url:
        _active_apply_urls[url] = datetime.now(timezone.utc)
    try:
        outcome, error_detail = await run_apply_agent_for_url(
            url=url,
            timeout_sec=_APPLY_AGENT_TIMEOUT,
            apply_agent_path=APPLY_AGENT_PATH,
            python_executable=sys.executable,
            force=force,
            paste_file=paste_file,
            permalink=permalink,
        )
        if outcome == "fail":
            logger.error("[apply_agent] failed for %s", label)
            # error_detail is a raw stderr/stdout tail — escape it or a stray
            # `<` makes Telegram reject the whole failure message (HTML parse).
            err_block = f"\n\n<pre>{html.escape(error_detail[:800])}</pre>" if error_detail else ""
            await _tg_notify(f"❌ <b>apply_agent failed</b>\n🔗 {label}{err_block}")
        else:
            logger.info("[apply_agent] done (%s) for %s", outcome, label)
            if url:
                try:
                    from hunter.tracker_cache import cache
                    await cache.load_from_db()
                    row = await cache.get_row_by_url(url)
                    if row:
                        from hunter import gsheets_sync
                        await gsheets_sync.mirror_new_row(row)
                except Exception as _e:
                    logger.warning("[apply_agent] gsheets mirror failed: %s", _e)
            # Upload application folder to Google Drive (best-effort)
            try:
                from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
                if GDRIVE_ENABLED:
                    from hunter.tracker import get_folder_by_url
                    folder_str = await asyncio.to_thread(get_folder_by_url, url)
                    if folder_str:
                        from hunter import gdrive_sync
                        drive_url = await gdrive_sync.upload_application_folder(
                            PROJECT_DIR / folder_str, job_url=url
                        )
                        if drive_url:
                            await _tg_notify(
                                f'📁 <a href="{drive_url}">Open folder on Drive</a>'
                            )
            except Exception as _e:
                logger.warning("[apply_agent] gdrive upload failed: %s", _e)
    except Exception as e:
        logger.error("[apply_agent] exception: %s", e)
        await _tg_notify(f"❌ <b>apply_agent exception</b>\n{e}\n🔗 {label}")
    finally:
        _active_apply_urls.pop(url, None)
        if paste_file:
            try:
                Path(paste_file).unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(
                    "[apply_agent] could not delete paste file %s: %s",
                    paste_file, cleanup_err,
                )


async def _run_linkedin_batch(job_ids: list[str], update: Update) -> None:
    """Run apply_agent sequentially for each LinkedIn job id."""
    from hunter.sources.linkedin import job_view_url
    from hunter.models import Job
    from hunter.tracker import add_failed

    total = len(job_ids)
    ok = failed = 0

    for i, jid in enumerate(job_ids, 1):
        url = job_view_url(jid)
        try:
            await update.message.reply_text(
                f"⏳ [{i}/{total}] LinkedIn job {jid}",
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
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            ok += 1
            logger.info("[linkedin_batch] OK job %s", jid)
        else:
            failed += 1
            logger.error(
                "[linkedin_batch] FAIL job %s: %s",
                jid, stderr.decode(errors="replace")[-300:],
            )
            try:
                stub = Job(
                    title=f"LinkedIn {jid}", company="LinkedIn",
                    url=url, source="linkedin", location="",
                )
                await asyncio.to_thread(add_failed, stub)
            except Exception as e:
                logger.warning(
                    "[linkedin_batch] could not write FAIL to tracker for %s: %s", jid, e
                )

    try:
        from telegram.constants import ParseMode
        await update.message.reply_text(
            f"🏁 <b>LinkedIn batch done</b>\n✅ {ok} / ❌ {failed} / Total: {total}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def _handle_paste(update: Update, text: str, force: bool = False) -> None:
    """Save pasted job text to a temp file and run apply_agent in paste mode.

    The URL (if found inside the text) is passed to apply_agent so it ends up
    in the tracker. If no URL — apply_agent runs without one.

    ``force=True`` passes ``--force`` (bypasses tracker dedup and React-only skip).
    """
    from telegram.constants import ParseMode
    from hunter.sources.jobleads import JOBLEADS_PASTE_MARKER
    from hunter.tracker import lookup_url, manual_jobleads_job_posting_path

    url = _extract_url(text)
    url_inferred = False

    # Block if URL already tracked — unless it's a MANUAL-pending row or force mode.
    manual_pending = False
    if url:
        entries = await asyncio.to_thread(lookup_url, url)
        manual_pending = any(
            str(e.get("ats") or "").strip().upper() == "MANUAL" for e in entries
        )
        if entries and not manual_pending and not force:
            detail = "\n".join(
                f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}\n"
                f"    ATS: {e['ats']}"
                + (f" | Sent: {e['sent']}" if e["sent"] else "")
                for e in entries
            )
            await update.message.reply_text(
                f"⚠️ <b>This vacancy is already in the tracker!</b>\n\n"
                f"{detail}\n\n"
                f"Send <code>/force {url}</code> or <code>/force</code> with full text to reprocess.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

    # MANUAL JobLeads row: write text into the existing job_posting.txt and rerun.
    if manual_pending and url and "jobleads.com" in url.lower():
        jp = await asyncio.to_thread(manual_jobleads_job_posting_path, url)
        if jp and jp.is_file():
            try:
                existing = jp.read_text(encoding="utf-8", errors="replace")
                if JOBLEADS_PASTE_MARKER in existing:
                    prefix, _ = existing.split(JOBLEADS_PASTE_MARKER, 1)
                    jp.write_text(
                        prefix + JOBLEADS_PASTE_MARKER + "\n\n" + text.strip() + "\n",
                        encoding="utf-8",
                    )
                else:
                    jp.write_text(text.strip() + "\n", encoding="utf-8")
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Failed to write text to <code>{jp}</code>\n<pre>{str(e)[:500]}</pre>",
                    parse_mode=ParseMode.HTML,
                )
                return

            inferred_note = " (URL recovered from tracker)" if url_inferred else ""
            force_note = " <code>--force</code>" if force else ""
            await update.message.reply_text(
                "✅ <b>Confirmed:</b> text written to <code>job_posting.txt</code>, "
                f"starting document generation{force_note}.\n"
                f"🔗 {url}{inferred_note}\n\n"
                "Estimated 1–2 min; files will be sent in a separate message.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info(
                "[paste handler] Updated MANUAL job_posting.txt and rerun apply url=%s force=%s",
                url, force,
            )
            asyncio.create_task(_run_apply_agent(url, force=force))
            return

    # Generic paste: write text to a temp file and pass it to apply_agent.
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            suffix=".txt", prefix="tg_paste_",
            delete=False,
        )
        with tmp as fh:
            fh.write(text)
        paste_path = tmp.name
    except Exception as e:
        logger.exception("[paste handler] failed to write temp file")
        await update.message.reply_text(
            f"❌ Failed to save posted text to temp file: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    chars = len(text)
    inferred_note = " (URL recovered from tracker)" if url_inferred else ""
    url_line = f"🔗 {url}{inferred_note}" if url else "🔗 (no URL found — processing without one)"
    mode = "paste + <code>--force</code>" if force else "paste mode"
    await update.message.reply_text(
        "✅ <b>Confirmed:</b> text saved, launching <code>apply_agent</code> "
        f"({mode}, {chars} chars).\n"
        f"{url_line}\n\n"
        "Estimated 1–2 min; result will be sent here.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(
        "[paste handler] Launching apply_agent paste mode (%d chars) url=%s force=%s",
        chars, url or "—", force,
    )
    asyncio.create_task(_run_apply_agent(url, force=force, paste_file=paste_path))
