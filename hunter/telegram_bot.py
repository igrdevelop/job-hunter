"""
telegram_bot.py — Telegram bot: notifications, inline buttons, callback handlers.

Pending jobs are stored in memory (dict job_id → Job) per session.
If the bot restarts, old buttons become "expired" — that's acceptable.
"""

import asyncio
import logging
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from hunter.config import (
    APPLY_AGENT_PATH,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIMEZONE,
    SCHEDULE_TIMES,
    SCHEDULE_SOURCE_OFFSET_MIN,
    TRACKER_BACKUP_ENABLED,
    TRACKER_BACKUP_TIME,
    EXPIRED_CHECK_TIME,
    GSHEETS_ENABLED,
    GSHEETS_REFRESH_INTERVAL_MIN,
    TRACKER_PATH,
    EMAIL_RESPONSE_CHECK_TIME,
    EMAIL_RESPONSE_LOOKBACK_DAYS,
)
from hunter.models import Job
from hunter.tracker import (
    add_skipped,

    lookup_url,
    lookup_company,
    manual_jobleads_job_posting_path,
    normalize_url,
)

logger = logging.getLogger(__name__)

# In-memory store: job_id (10-char hash) → Job
# Cleared on bot restart — acceptable trade-off vs complexity of persistence
_pending_jobs: dict[str, Job] = {}


# ── Keyboard factory ──────────────────────────────────────────────────────────

def _make_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"apply:{job_id}"),
        InlineKeyboardButton("❌ Skip",  callback_data=f"skip:{job_id}"),
    ]])


# ── Public API (called from main.py) ─────────────────────────────────────────

async def send_text(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


async def send_job_cards(context: ContextTypes.DEFAULT_TYPE, jobs: list[Job]) -> None:
    """Send one Telegram message per job with Apply/Skip buttons."""
    for job in jobs:
        jid = job.job_id()
        _pending_jobs[jid] = job
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=job.telegram_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_make_keyboard(jid),
            disable_web_page_preview=True,
        )


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>Job Hunter Bot</b>\n\n"
        "Commands:\n"
        "/hunt [source …] - run search (all sources, or e.g. <code>/hunt arbeitnow justjoin</code>)\n"
        "/status - source schedule + bot status\n"
        "/force — force generation: <code>/force URL</code> or <code>/force</code> "
        "+ full job posting text (bypasses dedup and React-only; JobLeads: "
        "<code>job_posting.txt</code>)\n"
        "/process_manual - process MANUAL rows with filled job_posting.txt\n"
        "/sync_sent - sync Sent column from Google Sheets → tracker.xlsx\n"
        "/unsent - count unsent applications and how many have ANGULAR in stack\n"
        "/check_expired - check tracker for expired vacancies\n"
        "/gsheets_status - Google Sheets integration status\n"
        "/gsheets_push_missing - push tracker.xlsx rows missing from Sheets\n"
        "/gdrive_upload_missing - upload all tracker.xlsx folders to Google Drive\n\n"
        "Or just send a job URL to generate docs.",
        parse_mode=ParseMode.HTML,
    )


def _parse_hunt_source_args(args: list[str], valid_names: set[str]) -> tuple[list[str] | None, list[str]]:
    """Split /hunt arguments into source slugs. Returns (names or None for «all», unknown slugs)."""
    requested: list[str] = []
    for a in args:
        for part in a.split(","):
            part = part.strip().lower()
            if part:
                requested.append(part)
    if not requested:
        return None, []
    seen: set[str] = set()
    unique: list[str] = []
    for r in requested:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    unknown = [r for r in unique if r not in valid_names]
    if unknown:
        return [], unknown
    return unique, []


async def cmd_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger — full hunt or a subset of sources (same names as in /schedule)."""
    from hunter.main import run_hunt
    from hunter.sources import ALL_SOURCES

    valid_names = {s.name for s in ALL_SOURCES}
    source_names, unknown = _parse_hunt_source_args(context.args or [], valid_names)

    if unknown:
        avail = ", ".join(sorted(valid_names))
        await update.message.reply_text(
            f"❌ Unknown source(s): <b>{', '.join(unknown)}</b>\n\n"
            f"Available: <code>{avail}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if source_names:
        label = ", ".join(source_names)
        await update.message.reply_text(
            f"🔍 Running hunt: <b>{label}</b>",
            parse_mode=ParseMode.HTML,
        )
        await run_hunt(context, source_names=source_names)
    else:
        await update.message.reply_text("🔍 Running hunt (all sources)...")
        await run_hunt(context)


async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-process: URL from tracker / React-only, or full pasted posting after /force."""
    raw = (update.message.text or "").strip()
    m = re.match(r"/force(?:@\w+)?\s*(.*)\Z", raw, flags=re.DOTALL | re.IGNORECASE)
    body = (m.group(1) or "").strip() if m else ""

    if not body:
        await update.message.reply_text(
            "<b>/force</b> — force generation (<code>--force</code>):\n\n"
            "• <code>/force https://…</code> — by URL\n"
            "• <code>/force</code> followed by full job posting text — "
            "same as paste flow but bypasses dedup and React-only\n\n"
            "Text must be long enough (like a full JD); otherwise send an http URL.",
            parse_mode=ParseMode.HTML,
        )
        return

    if _looks_like_paste(body):
        await update.message.reply_text(
            f"🔧 <b>Force + job text</b> — {len(body.strip())} chars. "
            "Bypasses: tracker dedup, React-only. Starting…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[Force] paste mode ({len(body)} chars)")
        await _handle_paste(update, body, force=True)
        return

    if body.startswith("http"):
        url = _extract_url(body) or body.split()[0].strip()
        await update.message.reply_text(
            f"⏳ <b>Force: starting generation</b> (<code>--force</code>)\n"
            f"🔗 {url}\n\n"
            "Bypasses: tracker dedup, React-only skip; for JobLeads — existing "
            "<code>job_posting.txt</code> will be used on fetch.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[Force] Launching apply_agent --force for: {url}")
        asyncio.create_task(_run_apply_agent(url, force=True))
        return

    await update.message.reply_text(
        "After <code>/force</code> provide an <b>http(s) URL</b> or full job posting text "
        "(same as paste flow). A single word without a URL is not valid.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process all MANUAL-pending tracker rows whose job_posting.txt is already filled."""
    from hunter.tracker import get_all_manual_pending
    from job_fetch.jobleads import try_load_manual_job_posting

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
    logger.info(f"[process_manual] Processing {len(ready)} ready MANUAL rows")

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
            logger.info(f"[process_manual] OK: {url}")
        else:
            failed += 1
            logger.error(f"[process_manual] FAIL: {url}\n{stderr.decode(errors='replace')[-300:]}")

    await update.message.reply_text(
        f"🏁 <b>process_manual done</b>\n✅ {ok} / ❌ {failed} / Total: {total}",
        parse_mode=ParseMode.HTML,
    )


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

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_build_schedule_text(), parse_mode=ParseMode.HTML)


async def cmd_unsent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Count unsent applications in tracker and how many have ANGULAR in stack."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        total = await cache.unsent_count()
        angular_n = await cache.unsent_angular_count()
        if total == 0:
            msg = "📭 <b>No unsent applications.</b>"
        else:
            msg = (
                f"📋 <b>Unsent applications:</b> {total}\n"
                f"🔷 <b>With ANGULAR in stack:</b> {angular_n}"
            )
    except Exception as exc:
        logger.exception("[unsent] Failed: %s", exc)
        msg = f"❌ Failed to read tracker: <code>{exc}</code>"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_check_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check all unsent tracker rows for expired job offers."""
    from hunter.expired_marker import run_check

    status_msg = await update.message.reply_text(
        "🔍 Checking tracker for expired vacancies…\n"
        "<i>EXPIRED will be written directly to tracker.xlsx.</i>",
        parse_mode=ParseMode.HTML,
    )

    async def progress_cb(text: str) -> None:
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    try:
        result = await run_check(progress_cb=progress_cb)
    except Exception as e:
        logger.exception("[check_expired] Failed: %s", e)
        await status_msg.edit_text(
            f"❌ Error: <code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    total   = result["total"]
    alive   = result["alive"]
    expired = result["expired"]
    errors  = result["errors"]
    skipped = result.get("skipped", [])

    lines = [f"✅ <b>Check complete</b> — {total} vacancies\n"]

    if expired:
        lines.append(f"⏭ <b>Expired ({len(expired)}):</b>")
        for item in expired:
            lines.append(f"  • {item['company']} — {item['title']}")
        lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
        lines.append("")

    if errors:
        lines.append(f"⚠️ <b>Fetch errors ({len(errors)}):</b>")
        for item in errors[:5]:
            lines.append(f"  • {item['company']}: {item['error'][:60]}")
        if len(errors) > 5:
            lines.append(f"  … {len(errors) - 5} more")
        lines.append("")

    lines.append(f"✅ Alive: <b>{alive}</b>")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): <b>{len(skipped)}</b>")

    await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_debug_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic: show step-by-step expired detection for a single URL.

    Usage: /debug_url <url>
    """
    args = (context.args or [])
    if not args:
        await update.message.reply_text(
            "Usage: /debug_url &lt;url&gt;\n"
            "Example: /debug_url https://www.pracuj.pl/praca/x,oferta,123",
            parse_mode=ParseMode.HTML,
        )
        return

    url = args[0].strip()
    msg = await update.message.reply_text(f"🔍 Diagnosing: <code>{url[:80]}</code>…", parse_mode=ParseMode.HTML)

    lines = [f"🔍 <b>debug_url</b>: <code>{url[:80]}</code>\n"]

    try:
        from urllib.parse import urlparse
        from job_fetch import _clean_url, fetch_job_text
        from hunter.expired_check import is_job_expired, is_expired_by_html
        from hunter.expired_marker import _quick_html_expired, _is_cloudflare_challenge

        domain = urlparse(url).hostname or ""
        clean = _clean_url(url)
        lines.append(f"<b>Domain:</b> {domain}")
        lines.append(f"<b>Clean URL:</b> <code>{clean[:80]}</code>")

        # 1. Is it in unsent rows?
        from hunter.tracker import (
            iter_unsent_rows,
            ATS_COL_INDEX, SENT_COL_INDEX, ID_COL_INDEX,
            URL_COL_INDEX, COMPANY_COL_INDEX, TITLE_COL_INDEX,
        )
        from hunter.config import TRACKER_PATH
        import openpyxl as _openpyxl

        offer_id = url.split(",oferta,")[-1].split("?")[0] if ",oferta," in url else ""

        def _url_matches(row_url: str) -> bool:
            if offer_id and offer_id in row_url:
                return True
            return row_url == clean or row_url == url

        rows = await asyncio.to_thread(iter_unsent_rows)
        matching = [r for r in rows if _url_matches(r.get("url", ""))]
        if matching:
            r = matching[0]
            lines.append(f"\n✅ <b>In unsent tracker rows:</b> {r['company']} — {r['title']}")
            lines.append(f"   ATS={r['ats']} | Sent={repr(r['sent'])} | ID={r['id'][:8]}")
        else:
            lines.append("\n⚠️ <b>NOT in unsent tracker rows</b>")

            # Scan ALL rows (including excluded ones) to explain why
            def _find_row_in_tracker():
                if not TRACKER_PATH.exists():
                    return None
                wb = _openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
                ws = wb.active
                try:
                    for row in ws.iter_rows(min_row=2, values_only=True):
                        if not row:
                            continue
                        row_url = str(row[URL_COL_INDEX - 1] or "").strip()
                        if _url_matches(row_url):
                            return {
                                "company": str(row[COMPANY_COL_INDEX - 1] or "").strip(),
                                "title": str(row[TITLE_COL_INDEX - 1] or "").strip(),
                                "ats": str(row[ATS_COL_INDEX - 1] or "").strip(),
                                "sent": str(row[SENT_COL_INDEX - 1] or "").strip(),
                                "id": str(row[ID_COL_INDEX - 1] or "").strip(),
                                "url": row_url,
                            }
                finally:
                    wb.close()
                return None

            found_row = await asyncio.to_thread(_find_row_in_tracker)
            if found_row:
                ats = found_row["ats"]
                sent = found_row["sent"]
                row_id = found_row["id"]
                lines.append(f"   Found in all rows: {found_row['company']} — {found_row['title']}")
                lines.append(f"   ATS={repr(ats)} | Sent={repr(sent)} | ID={repr(row_id[:8] if row_id else '')}")
                reasons = []
                if ats == "SKIP":
                    reasons.append("ATS=SKIP")
                if sent:
                    reasons.append(f"Sent={repr(sent)} (non-empty → excluded from /check_expired)")
                if not row_id:
                    reasons.append("no ID")
                if reasons:
                    lines.append(f"   ❌ Excluded because: {', '.join(reasons)}")
                else:
                    lines.append("   ⚠️ No obvious exclusion reason — URL matching may have missed it")
            else:
                lines.append("   ❌ Not found in tracker at all (not applied, or URL mismatch)")

        # 2. Quick HTML check
        lines.append("\n<b>Step 1 — quick HTML check:</b>")
        import cloudscraper as _cs
        import requests as _req
        _scraper = _cs.create_scraper()
        try:
            resp = await asyncio.to_thread(lambda: _scraper.get(clean, timeout=20))
            html = resp.text
            lines.append(f"  cloudscraper → HTTP {resp.status_code}, {len(html)} bytes")
            lines.append(f"  is_cloudflare_challenge: {_is_cloudflare_challenge(html)}")
            lines.append(f"  is_expired_by_html: {is_expired_by_html(html, domain)}")
            # show which marker matched
            from hunter.expired_check import HTML_EXPIRED_MARKERS
            for key, markers in HTML_EXPIRED_MARKERS.items():
                if key in domain:
                    for m in markers:
                        if m.lower() in html.lower():
                            lines.append(f"  ✅ HTML marker hit: <code>{m[:50]}</code>")
                            break
        except Exception as e:
            lines.append(f"  cloudscraper ERROR: {str(e)[:100]}")

        quick_result = await asyncio.to_thread(_quick_html_expired, url, domain)
        lines.append(f"  _quick_html_expired → <b>{quick_result}</b>")

        # 3. Full fetch
        lines.append("\n<b>Step 2 — full fetch_job_text:</b>")
        try:
            text = await asyncio.to_thread(fetch_job_text, url)
            lines.append(f"  length: {len(text)} chars")
            expired = is_job_expired(text)
            lines.append(f"  is_job_expired: <b>{expired}</b>")
            # show last 300 chars (where archived notice appears)
            tail = text[-300:].replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"  tail:\n<pre>{tail}</pre>")
        except Exception as e:
            lines.append(f"  ERROR: {str(e)[:150]}")

    except Exception as e:
        lines.append(f"\n❌ Diagnostic failed: {e}")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_sync_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull Sent/To Learn/Re-application changes from Google Sheets → tracker.xlsx."""
    from hunter import gsheets_sync
    from hunter.config import GSHEETS_ENABLED

    if not GSHEETS_ENABLED:
        await update.message.reply_text(
            "ℹ️ Google Sheets disabled (GSHEETS_ENABLED=false). /sync_sent unavailable.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text("⏳ Syncing Sheets → tracker.xlsx…")
    try:
        result = await gsheets_sync.pull_full_snapshot()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Pull error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pulled = result["pulled"]
    updated = result["updated"]
    errors = result["errors"]

    lines = [f"✅ <b>sync_sent done</b>"]
    lines.append(f"  Rows from Sheets: {pulled}")
    lines.append(f"  Updated in tracker.xlsx: {updated}")
    if errors:
        lines.append(f"⚠️ Errors: {'; '.join(errors[:2])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_gsheets_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Google Sheets integration status."""
    from hunter import gsheets_sync
    try:
        report = await gsheets_sync.status_report()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to get status: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = report["enabled"]
    if not enabled:
        await update.message.reply_text(
            "ℹ️ <b>Google Sheets disabled</b> (GSHEETS_ENABLED=false).\n"
            "Set GSHEETS_ENABLED=true in .env to enable.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["📊 <b>Google Sheets status</b>"]
    lines.append(f"  Service: {'✅ OK' if report['service_ok'] else '❌ not initialized'}")
    if report.get("sheet_url"):
        lines.append(f"  Sheet: <a href=\"{report['sheet_url']}\">open</a>")
    elif report.get("sheet_id"):
        lines.append(f"  ID: <code>{report['sheet_id']}</code>")
    else:
        lines.append("  Sheet: not configured")
    dirty = report.get("dirty_count", 0)
    lines.append(f"  Dirty rows: {dirty}")
    if dirty:
        lines.append("  ℹ️ Run /gsheets_push_missing to resync")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_gdrive_upload_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload all tracker.xlsx application folders to Google Drive (runs in background)."""
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        await update.message.reply_text(
            "⚠️ GDRIVE_ENABLED=false — Google Drive is not enabled.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await update.message.reply_text(
        "⏳ Upload to Google Drive started in background…",
        parse_mode=ParseMode.HTML,
    )

    async def _run() -> None:
        async def _progress(text: str) -> None:
            try:
                await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

        try:
            from hunter import gdrive_sync
            result = await gdrive_sync.upload_missing_folders(PROJECT_DIR, progress_cb=_progress)
        except Exception as e:
            await update.message.reply_text(
                f"❌ gdrive_upload_missing error: <code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        uploaded = result["uploaded"]
        already = result.get("already_uploaded", 0)
        skipped = result["skipped_missing"]
        errors = result.get("errors", [])
        err_note = ""
        if errors:
            err_lines = "\n".join(f"  • {e[:120]}" for e in errors[:5])
            err_note = f"\n⚠️ Errors ({len(errors)}):\n<code>{err_lines}</code>"
        await update.message.reply_text(
            f"✅ <b>gdrive_upload_missing</b>\n"
            f"  📤 Uploaded: {uploaded}\n"
            f"  ✔ Already on Drive: {already}\n"
            f"  ⏭ Missing locally: {skipped}"
            f"{err_note}",
            parse_mode=ParseMode.HTML,
        )

    context.application.create_task(_run())


async def cmd_gsheets_push_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Push tracker.xlsx rows that are absent from Google Sheets (by ID)."""
    from hunter import gsheets_sync
    await update.message.reply_text(
        "⏳ Looking for tracker.xlsx rows absent from Sheets…",
        parse_mode=ParseMode.HTML,
    )
    try:
        result = await gsheets_sync.push_missing_rows()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pushed = result["pushed"]
    already = result["already_present"]
    errors = result.get("errors", [])
    err_note = f"\n⚠️ <code>{errors[0][:200]}</code>" if errors else ""
    await update.message.reply_text(
        f"✅ <b>gsheets_push_missing</b>\n"
        f"  📤 Pushed: {pushed}\n"
        f"  ✔️ Already present: {already}"
        f"{err_note}",
        parse_mode=ParseMode.HTML,
    )


def _build_schedule_text() -> str:
    from hunter.sources import ALL_SOURCES

    lines = []
    for idx, source in enumerate(ALL_SOURCES):
        times = []
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            times.append(f"{total // 60:02d}:{total % 60:02d}")
        lines.append(f"  <b>{source.name}</b>: {' / '.join(times)}")

    schedule_str = "\n".join(lines)
    return (
        f"⏰ <b>Schedule</b> ({TIMEZONE}, offset {SCHEDULE_SOURCE_OFFSET_MIN} min):\n"
        f"{schedule_str}"
    )


# ── Email response checker ───────────────────────────────────────────────────

def _format_check_responses_report(results) -> str:
    """Format run_confirmation_check() results into a Telegram HTML message."""
    from hunter.email_response_checker import MatchResult

    confirmed = [r for r in results if r.match_type in ("exact", "fuzzy") and r.row_num]
    ambiguous = [r for r in results if r.match_type == "ambiguous"]
    no_match  = [r for r in results if r.match_type == "no_match"]

    if not results:
        return "📭 <b>No confirmation emails found</b> in the last few days."

    lines = []

    if confirmed:
        lines.append(f"✅ <b>Confirmed ({len(confirmed)}):</b>")
        for r in confirmed:
            c = r.candidates[0]
            tag = f"[{r.email.platform}]"
            lines.append(f"  • <b>{c['company']}</b> — {c['title']} <i>{tag}</i>")
        lines.append("")

    if ambiguous:
        lines.append(f"❓ <b>Ambiguous — needs review ({len(ambiguous)}):</b>")
        for r in ambiguous:
            company = r.email.company or "?"
            title   = r.email.title   or "(no title extracted)"
            lines.append(f"  • {company} — {title}")
            cands = ", ".join(c["title"] for c in r.candidates[:3])
            lines.append(f"    <i>Candidates: {cands}</i>")
        lines.append("")

    if no_match:
        lines.append(f"📭 <b>Not matched ({len(no_match)}):</b>")
        for r in no_match:
            company = r.email.company or "(no company)"
            title   = r.email.title   or "(no title)"
            lines.append(f"  • {company} — {title}")

    return "\n".join(lines).strip()


async def cmd_check_responses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check Gmail for application confirmation emails and update tracker.

    Usage: /check_responses [days]
      days — how many days back to scan (default: EMAIL_RESPONSE_LOOKBACK_DAYS = 2).
      Example: /check_responses 60  — backfill last 60 days.
    """
    days: int | None = None
    if context.args:
        try:
            days = int(context.args[0])
            if days < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"⚠️ Invalid argument: <code>{context.args[0]}</code>\n"
                "Usage: <code>/check_responses [days]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    days_label = days if days is not None else EMAIL_RESPONSE_LOOKBACK_DAYS
    status_msg = await update.message.reply_text(
        f"📬 Checking Gmail for confirmation emails (last {days_label} day(s))…",
        parse_mode=ParseMode.HTML,
    )
    try:
        from hunter.email_response_checker import run_confirmation_check
        results = await asyncio.to_thread(run_confirmation_check, days)
    except FileNotFoundError:
        await status_msg.edit_text(
            "❌ <b>Gmail not configured.</b>\n"
            "Run <code>python tools/gmail_auth.py</code> to set up access.",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        logger.exception("[check_responses] Failed: %s", e)
        await status_msg.edit_text(
            f"❌ Error: <code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    report = _format_check_responses_report(results)
    await status_msg.edit_text(report, parse_mode=ParseMode.HTML)


# ── Callback handler (Apply / Skip buttons) ───────────────────────────────────

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

    logger.info(f"[Apply] Launching apply_agent for: {job.url}")

    # Run apply_agent.py as a detached subprocess so bot stays responsive
    # apply_agent.py will send its own Telegram notification when done
    asyncio.create_task(_run_apply_agent(job.url))


_APPLY_AGENT_TIMEOUT = 900  # 15 min hard cap per job

# Active manual apply_agent URLs → start datetime, used by /status.
_active_apply_urls: dict[str, "datetime"] = {}


async def _tg_notify(text: str) -> None:
    """Send a message to the configured chat via bot token (no context needed)."""
    from telegram import Bot
    try:
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception as e:
        logger.error(f"[tg_notify] failed: {e}")


async def _run_apply_agent(
    url: str,
    force: bool = False,
    paste_file: Optional[str] = None,
) -> None:
    """Run apply_agent.py via apply_service, don't block the event loop.

    If ``paste_file`` is set, URL may be empty — apply_agent will use the pasted
    text instead of fetching.
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
        )
        if outcome == "fail":
            logger.error(f"[apply_agent] failed for {label}")
            err_block = (
                f"\n\n<pre>{error_detail[:800]}</pre>" if error_detail else ""
            )
            await _tg_notify(
                f"❌ <b>apply_agent failed</b>\n🔗 {label}{err_block}"
            )
        else:
            logger.info(f"[apply_agent] done ({outcome}) for {label}")
            if url:
                try:
                    from hunter.tracker_cache import cache
                    from hunter.config import TRACKER_PATH
                    await cache.load_from_excel(TRACKER_PATH)
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
        logger.error(f"[apply_agent] exception: {e}")
        await _tg_notify(f"❌ <b>apply_agent exception</b>\n{e}\n🔗 {label}")
    finally:
        _active_apply_urls.pop(url, None)
        if paste_file:
            try:
                Path(paste_file).unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(
                    f"[apply_agent] could not delete paste file {paste_file}: {cleanup_err}"
                )


# ── URL message handler ───────────────────────────────────────────────────────

# Any message longer than this counts as "pasted job posting" if it isn't a single URL.
# Typical job postings are 1-4 KB; short greetings / single URLs stay well below this.
# 200 catches compact JD summaries users paste from recruiters (~250 chars) without
# reacting to casual chat.
_PASTE_TEXT_MIN_LEN = 200

_URL_RE = re.compile(r"https?://\S+")


def _looks_like_paste(text: str) -> bool:
    """True when user likely pasted a job posting (with or without URL)."""
    stripped = text.strip()
    if len(stripped) < _PASTE_TEXT_MIN_LEN:
        return False
    # Text with a URL + lots of extra content → paste with URL hint
    urls = _URL_RE.findall(stripped)
    if urls:
        non_url_len = len(_URL_RE.sub("", stripped).strip())
        return non_url_len >= _PASTE_TEXT_MIN_LEN
    # No URL at all but long message → pure paste
    return True


def _extract_url(text: str) -> str:
    """Return the first http(s) URL found in text, or ''."""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(").,;") if m else ""


async def cmd_about_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate (or regenerate) About Me for a job URL in the tracker.

    Usage: /about_me <lang> <url>
    lang: en | pl
    """
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /about_me <lang> <url>\nExample: /about_me pl https://justjoin.it/job-offer/..."
        )
        return

    lang, url = args[0].lower(), args[1]
    if lang not in ("en", "pl"):
        await update.message.reply_text("lang must be 'en' or 'pl'")
        return

    from hunter.tracker import get_folder_by_url, normalize_url
    from hunter.config import PROJECT_DIR

    normalized = normalize_url(url)
    folder_str = get_folder_by_url(normalized)
    if not folder_str:
        await update.message.reply_text(
            "URL not found in tracker. Run /force to process it first."
        )
        return

    folder_path = PROJECT_DIR / folder_str
    if not (folder_path / "job_posting.txt").exists():
        await update.message.reply_text(
            "No job_posting.txt in folder - cannot generate."
        )
        return

    await update.message.reply_text(f"⏳ Generating About Me ({lang.upper()})...")

    import asyncio
    from hunter.about_me_agent import generate_about_me

    result = await asyncio.to_thread(generate_about_me, folder_path, lang)
    if not result:
        await update.message.reply_text("❌ Generation failed - check logs.")
        return

    await update.message.reply_text(result)
    await update.message.reply_text(
        f"✅ Saved to {folder_str}/About_Me_{lang.upper()}.txt"
    )


async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages.

    - Long pasted job text (>= _PASTE_TEXT_MIN_LEN, with or without URL) → paste flow
    - Single job URL (JustJoin, NoFluffJobs, LinkedIn /jobs/view/...) → apply_agent
    - LinkedIn search / alert URL (/jobs/search?...) → extract job ids → batch apply
    """
    text = (update.message.text or "").strip()

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

    from job_fetch.linkedin_parse import is_linkedin_search, parse_linkedin_job_ids, job_view_url
    from hunter.config import MAX_JOBS_PER_RUN

    # Normalize LinkedIn view URLs — strip tracking params (?trk=...&refId=...)
    from job_fetch.linkedin_parse import normalize_linkedin_url
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
            from job_fetch.jobleads import try_load_manual_job_posting
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
                    f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}{folder_info}\n\n"
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
                f'  Row {e["row"]}: <b>{e["company"]}</b> - {e["title"]}\n'
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


async def _handle_paste(update: Update, text: str, force: bool = False) -> None:
    """Save the pasted job text to a temp file and run apply_agent in paste mode.

    The URL (if found inside the text) is passed to apply_agent so it ends up in
    the tracker. If no URL — apply_agent runs without one and writes an empty URL cell.

    ``force=True`` passes ``--force`` (bypass tracker duplicate block and React-only skip).
    """
    from job_fetch.jobleads import JOBLEADS_PASTE_MARKER

    url = _extract_url(text)
    url_inferred = False

    # If URL is already tracked, only block when it is NOT a MANUAL-pending row.
    manual_pending = False
    entries = []
    if url:
        entries = await asyncio.to_thread(lookup_url, url)
        manual_pending = any(str(e.get("ats") or "").strip().upper() == "MANUAL" for e in entries)
        if entries and not manual_pending and not force:
            detail = "\n".join(
                f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}\n"
                f"    ATS: {e['ats']}"
                + (f" | Sent: {e['sent']}" if e['sent'] else "")
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

    # If this is a MANUAL-pending JobLeads row, write into its job_posting.txt and rerun apply.
    if manual_pending and url and "jobleads.com" in url.lower():
        jp = await asyncio.to_thread(manual_jobleads_job_posting_path, url)
        if jp and jp.is_file():
            try:
                existing = jp.read_text(encoding="utf-8", errors="replace")
                if JOBLEADS_PASTE_MARKER in existing:
                    prefix, _ = existing.split(JOBLEADS_PASTE_MARKER, 1)
                    jp.write_text(prefix + JOBLEADS_PASTE_MARKER + "\n\n" + text.strip() + "\n", encoding="utf-8")
                else:
                    # Fallback: overwrite file if marker is missing for some reason.
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
            logger.info(f"[paste handler] Updated MANUAL job_posting.txt and rerun apply url={url} force={force}")
            asyncio.create_task(_run_apply_agent(url, force=force))
            return

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="tg_paste_",
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
    if url:
        inferred_note = " (URL recovered from tracker)" if url_inferred else ""
        url_line = f"🔗 {url}{inferred_note}"
    else:
        url_line = "🔗 (no URL found — processing without one)"
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
        f"[paste handler] Launching apply_agent paste mode ({chars} chars) url={url or '—'} force={force}"
    )
    asyncio.create_task(_run_apply_agent(url, force=force, paste_file=paste_path))


async def _run_linkedin_batch(job_ids: list[str], update) -> None:
    """Run apply_agent sequentially for each LinkedIn job id."""
    from job_fetch.linkedin_parse import job_view_url
    from hunter.models import Job
    from hunter.tracker import add_failed

    total = len(job_ids)
    ok = failed = 0

    for i, jid in enumerate(job_ids, 1):
        url = job_view_url(jid)
        try:
            await update.message.reply_text(
                f"⏳ [{i}/{total}] LinkedIn job {jid}",
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
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            ok += 1
            logger.info(f"[linkedin_batch] OK job {jid}")
        else:
            failed += 1
            logger.error(f"[linkedin_batch] FAIL job {jid}: {stderr.decode(errors='replace')[-300:]}")
            try:
                stub = Job(title=f"LinkedIn {jid}", company="LinkedIn", url=url,
                           source="linkedin", location="")
                await asyncio.to_thread(add_failed, stub)
            except Exception as e:
                logger.warning(f"[linkedin_batch] could not write FAIL to tracker for {jid}: {e}")

    try:
        await update.message.reply_text(
            f"🏁 <b>LinkedIn batch done</b>\n✅ {ok} / ❌ {failed} / Total: {total}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ── Application factory ───────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Post-init hook: register bot commands + validate gsheets startup."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start",           "Show help"),
        BotCommand("hunt",            "Run search (optional: source names)"),
        BotCommand("status",          "Current activity: active jobs, pending, FAIL queue"),
        BotCommand("schedule",        "Hunt timetable per source"),
        BotCommand("force",           "Process URL even if already in tracker"),
        BotCommand("process_manual",  "Process MANUAL rows with filled job_posting.txt"),
        BotCommand("sync_sent",       "Sync Sent column from Google Sheets"),
        BotCommand("unsent",          "Unsent applications count + Angular"),
        BotCommand("check_expired",   "Check unsent rows for expired job offers"),
        BotCommand("debug_url",       "Diagnose expired detection for a single URL"),
        BotCommand("about_me",        "Generate About Me for a job URL (lang + url)"),
        BotCommand("gsheets_status",        "Google Sheets integration status"),
        BotCommand("gsheets_push_missing",  "Push tracker rows missing from Sheets"),
        BotCommand("gdrive_upload_missing", "Upload all tracker folders to Google Drive"),
        BotCommand("check_responses",       "Check Gmail confirmations [days]"),
    ])

    # Bootstrap / validate Google Sheets on startup.
    try:
        from hunter import gsheets_sync

        if GSHEETS_ENABLED:
            # Pre-flight: credentials + token check (sync, cheap)
            preflight = gsheets_sync.validate_startup()
            if not preflight.get("ok"):
                err = preflight.get("error", "unknown error")
                logger.error("[gsheets] startup validation failed: %s", err)
                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ <b>Google Sheets not ready</b>\n<code>{err}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return  # don't try to bootstrap if creds are broken

            async def _tg_notify(text: str) -> None:
                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as _e:
                    logger.warning("[gsheets] notify failed: %s", _e)

            result = await gsheets_sync.init_or_load_spreadsheet(notify_cb=_tg_notify)
            if result.get("error"):
                logger.error("[gsheets] init failed: %s", result["error"])
            else:
                url = result.get("sheet_url", "")
                created = result.get("created", False)
                logger.info(
                    "[gsheets] %s — %s",
                    "created new spreadsheet" if created else "loaded existing spreadsheet",
                    url,
                )
    except Exception as e:
        logger.warning("[gsheets] startup init failed: %s", e)

    # Load tracker cache so /unsent, /sync_sent, and scheduled reports are
    # correct immediately after startup (not only after the first /hunt).
    try:
        from hunter.tracker_cache import cache
        await cache.load_from_excel(TRACKER_PATH)
        logger.info("[startup] tracker_cache loaded")
    except Exception as e:
        logger.warning("[startup] tracker_cache load failed: %s", e)


def build_application() -> Application:
    """Build and configure the Telegram Application instance."""
    import pytz
    from datetime import time as dt_time

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("hunt",           cmd_hunt))
    app.add_handler(CommandHandler("force",          cmd_force))
    app.add_handler(CommandHandler("process_manual", cmd_process_manual))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("schedule",       cmd_schedule))
    app.add_handler(CommandHandler("unsent",         cmd_unsent))
    app.add_handler(CommandHandler("sync_sent",      cmd_sync_sent))
    app.add_handler(CommandHandler("check_expired",  cmd_check_expired))
    app.add_handler(CommandHandler("debug_url",      cmd_debug_url))
    app.add_handler(CommandHandler("about_me",       cmd_about_me))
    app.add_handler(CommandHandler("gsheets_status",       cmd_gsheets_status))
    app.add_handler(CommandHandler("gsheets_push_missing", cmd_gsheets_push_missing))
    app.add_handler(CommandHandler("gdrive_upload_missing", cmd_gdrive_upload_missing))
    app.add_handler(CommandHandler("check_responses",      cmd_check_responses))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(button_callback))

    # Plain URL messages → auto-apply
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_url))

    # Staggered per-source scheduled hunts.
    # Each source gets its own daily job at: base_time + source_index * offset_min.
    # Times wrap past midnight with modulo 24h.
    tz = pytz.timezone(TIMEZONE)
    from hunter.sources import ALL_SOURCES

    for idx, source in enumerate(ALL_SOURCES):
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            fire_hour, fire_min = total // 60, total % 60

            app.job_queue.run_daily(
                callback=_scheduled_hunt,
                time=dt_time(fire_hour, fire_min, tzinfo=tz),
                name=f"hunt_{source.name}_{base_time}",
                data={"source_names": [source.name]},
            )
            logger.info(
                f"[Schedule] {source.name} at "
                f"{fire_hour:02d}:{fire_min:02d} {TIMEZONE}"
            )

    # Daily pending-report at 09:00 and 21:00
    for report_hour in (9, 21):
        app.job_queue.run_daily(
            callback=_scheduled_pending_report,
            time=dt_time(report_hour, 0, tzinfo=tz),
            name=f"pending_report_{report_hour:02d}00",
        )
        logger.info(f"[Schedule] pending_report at {report_hour:02d}:00 {TIMEZONE}")

    # Daily expired check at EXPIRED_CHECK_TIME (default 00:00)
    try:
        ech, ecm = map(int, EXPIRED_CHECK_TIME.strip().split(":"))
    except (ValueError, AttributeError):
        ech, ecm = 0, 0
        logger.warning("[Schedule] Invalid EXPIRED_CHECK_TIME=%r — using 00:00", EXPIRED_CHECK_TIME)
    app.job_queue.run_daily(
        callback=_scheduled_check_expired,
        time=dt_time(ech, ecm, tzinfo=tz),
        name="check_expired_daily",
    )
    logger.info(f"[Schedule] check_expired at {ech:02d}:{ecm:02d} {TIMEZONE}")

    if TRACKER_BACKUP_ENABLED:
        try:
            bh, bm = map(int, TRACKER_BACKUP_TIME.strip().split(":"))
            bh %= 24
            bm %= 60
        except (ValueError, AttributeError):
            bh, bm = 6, 5
            logger.warning(
                "[Schedule] Invalid TRACKER_BACKUP_TIME=%r — using 06:05",
                TRACKER_BACKUP_TIME,
            )
        app.job_queue.run_daily(
            callback=_scheduled_tracker_backup,
            time=dt_time(bh, bm, tzinfo=tz),
            name="tracker_backup_daily",
        )
        logger.info(f"[Schedule] tracker_backup at {bh:02d}:{bm:02d} {TIMEZONE}")

    # Retry dirty Sheets rows every 5 minutes (no-op when GSHEETS_ENABLED=false)
    app.job_queue.run_repeating(
        callback=_scheduled_gsheets_resync,
        interval=300,
        first=60,
        name="gsheets_resync",
    )
    logger.info("[Schedule] gsheets_resync every 5 min")

    # Upload missing Drive folders every 3 hours (no-op when GDRIVE_ENABLED=false)
    app.job_queue.run_repeating(
        callback=_scheduled_gdrive_upload_missing,
        interval=3 * 3600,
        first=300,
        name="gdrive_upload_missing",
    )
    logger.info("[Schedule] gdrive_upload_missing every 3 h")

    # Daily email confirmation check at EMAIL_RESPONSE_CHECK_TIME (default 09:00)
    try:
        erch, ercm = map(int, EMAIL_RESPONSE_CHECK_TIME.strip().split(":"))
    except (ValueError, AttributeError):
        erch, ercm = 9, 0
        logger.warning(
            "[Schedule] Invalid EMAIL_RESPONSE_CHECK_TIME=%r — using 09:00",
            EMAIL_RESPONSE_CHECK_TIME,
        )
    app.job_queue.run_daily(
        callback=_scheduled_check_email_responses,
        time=dt_time(erch, ercm, tzinfo=tz),
        name="check_email_responses",
    )
    logger.info("[Schedule] check_email_responses at %02d:%02d %s", erch, ercm, TIMEZONE)

    # Daily applications summary at 00:01 Warsaw time
    app.job_queue.run_daily(
        callback=_scheduled_daily_summary,
        time=dt_time(0, 1, tzinfo=tz),
        name="daily_summary",
    )
    logger.info("[Schedule] daily_summary at 00:01 %s", TIMEZONE)

    # Pull Sheets → Excel every GSHEETS_REFRESH_INTERVAL_MIN (no-op when disabled)
    if GSHEETS_ENABLED:
        pull_interval_sec = max(60, GSHEETS_REFRESH_INTERVAL_MIN * 60)
        app.job_queue.run_repeating(
            callback=_scheduled_gsheets_pull,
            interval=pull_interval_sec,
            first=120,
            name="gsheets_pull",
        )
        logger.info("[Schedule] gsheets_pull every %d min", GSHEETS_REFRESH_INTERVAL_MIN)

    return app


async def _scheduled_check_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily scheduled expired check — runs at midnight, marks EXPIRED in tracker.xlsx."""
    from hunter.expired_marker import run_check

    logger.info("[scheduled_check_expired] Starting daily expired check")

    try:
        result = await run_check()
    except Exception as e:
        logger.exception("[scheduled_check_expired] run_check failed: %s", e)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ <b>Scheduled check_expired failed</b>\n<pre>{str(e)[:300]}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return

    expired = result["expired"]
    skipped = result.get("skipped", [])
    errors  = result["errors"]

    if not expired:
        logger.info("[scheduled_check_expired] Nothing expired.")
        return

    lines = [f"🌙 <b>Nightly expired check</b>\n"]
    lines.append(f"⏭ Expired: <b>{len(expired)}</b>")
    for item in expired:
        lines.append(f"  • {item['company']} — {item['title']}")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): {len(skipped)}")
    if errors:
        lines.append(f"⚠️ Errors: {len(errors)}")
    lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def _scheduled_tracker_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily snapshot of tracker.xlsx (silent on success)."""
    try:
        import asyncio as _asyncio
        from hunter.tracker_backup import run_tracker_backup

        result = await _asyncio.to_thread(run_tracker_backup)
        if not result.get("ok") or result.get("errors"):
            err = "; ".join(result.get("errors") or [])[:400]
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Tracker backup failed</b>\n<pre>{err}</pre>",
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.exception("[tracker_backup] scheduled job failed: %s", exc)
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Tracker backup failed</b>\n<pre>{str(exc)[:400]}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def _scheduled_hunt(context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import run_hunt
    source_names = context.job.data.get("source_names") if context.job.data else None
    try:
        await run_hunt(context, source_names=source_names)
    except Exception as e:
        label = ", ".join(source_names) if source_names else "all"
        logger.exception(f"[scheduled_hunt] Unhandled error for {label}")
        extra = ""
        if "Content_Types" in str(e) or "archive" in str(e).lower():
            extra = (
                "\n\n<i>Likely corrupt or non-xlsx tracker.xlsx file — "
                "not a board error in parentheses.</i>"
            )
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Hunt error</b> ({label}):\n<pre>{str(e)[:500]}</pre>{extra}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def _scheduled_gdrive_upload_missing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-3-hour job: upload application folders missing from Google Drive (no-op if disabled)."""
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        return
    try:
        from hunter import gdrive_sync
        result = await gdrive_sync.upload_missing_folders(PROJECT_DIR)
        uploaded = result.get("uploaded", 0)
        if uploaded:
            logger.info("[scheduled_gdrive_upload_missing] uploaded %d folder(s)", uploaded)
    except Exception as e:
        logger.warning("[scheduled_gdrive_upload_missing] failed: %s", e)


async def _scheduled_gsheets_resync(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-5-min job: push dirty rows to Google Sheets (no-op if disabled)."""
    try:
        from hunter import gsheets_sync
        synced = await gsheets_sync.resync_dirty()
        if synced:
            logger.info("[scheduled_gsheets_resync] pushed %d dirty row(s)", synced)
    except Exception as e:
        logger.warning("[scheduled_gsheets_resync] failed: %s", e)


async def _scheduled_gsheets_pull(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: pull Sheets → tracker.xlsx (every GSHEETS_REFRESH_INTERVAL_MIN)."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        from hunter import gsheets_sync
        result = await gsheets_sync.pull_full_snapshot()
        updated = result.get("updated", 0)
        if updated:
            logger.info("[scheduled_gsheets_pull] updated %d row(s) from Sheets", updated)
        if result.get("errors"):
            logger.warning("[scheduled_gsheets_pull] errors: %s", result["errors"])
    except Exception as e:
        logger.warning("[scheduled_gsheets_pull] failed: %s", e)


async def _scheduled_pending_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: report how many unsent applications are in tracker."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        total = await cache.unsent_count()
        if total == 0:
            msg = "📭 <b>No unsent applications.</b>"
        else:
            rows = await cache.all_unsent()
            fail_n = sum(1 for r in rows if r.get("ATS %") == "FAIL")
            manual_n = sum(1 for r in rows if r.get("ATS %") == "MANUAL")
            ready_n = total - fail_n - manual_n
            parts = [f"📋 <b>Unsent applications: {total}</b>"]
            if ready_n:
                parts.append(f"  ✅ Ready to send: {ready_n}")
            if manual_n:
                parts.append(f"  📝 MANUAL (text needed): {manual_n}")
            if fail_n:
                parts.append(f"  ❌ FAIL: {fail_n}")
            msg = "\n".join(parts)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("[scheduled_pending_report] Failed")


async def _scheduled_check_email_responses(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check Gmail for new confirmation emails; notify only if new ones found."""
    try:
        from hunter.email_response_checker import run_confirmation_check
        results = await asyncio.to_thread(run_confirmation_check)
    except FileNotFoundError:
        logger.debug("[scheduled_check_email_responses] gmail_token.json missing — skipping")
        return
    except Exception as e:
        logger.warning("[scheduled_check_email_responses] failed: %s", e)
        return

    # Only notify about rows that were just written (response was empty before this run)
    newly_confirmed = [
        r for r in results
        if r.match_type in ("exact", "fuzzy")
        and r.row_num is not None
        and not r.candidates[0].get("confirmation", "")
    ]
    if not newly_confirmed:
        return

    lines = [f"📬 <b>New confirmations from email ({len(newly_confirmed)}):</b>"]
    for r in newly_confirmed:
        c = r.candidates[0]
        lines.append(f"  • <b>{c['company']}</b> — {c['title']} <i>[{r.email.platform}]</i>")

    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("[scheduled_check_email_responses] send failed: %s", e)


# ── Daily applications summary ────────────────────────────────────────────────

def _format_daily_summary(apps: list[dict], date_str: str) -> str:
    """Format yesterday's applications as an HTML list."""
    if not apps:
        return f"📋 No applications recorded on {date_str}."
    lines = [f"📋 <b>Applications on {date_str} — {len(apps)} total:</b>"]
    for a in apps:
        ats = a.get("ats", "")
        ats_label = f" ({ats})" if ats and ats not in ("-", "—", "") else ""
        lines.append(f"  • <b>{a['company']}</b> — {a['title']}{ats_label}")
    return "\n".join(lines)


async def _scheduled_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job at 00:01: send a summary of how many applications were made yesterday."""
    from datetime import date as _date, timedelta
    from hunter.tracker import get_applications_on_date

    yesterday = (_date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        apps = await asyncio.to_thread(get_applications_on_date, yesterday)
    except Exception as e:
        logger.warning("[scheduled_daily_summary] failed to read tracker: %s", e)
        return

    if not apps:
        return  # silent when nothing was applied to yesterday

    text = _format_daily_summary(apps, yesterday)
    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("[scheduled_daily_summary] send failed: %s", e)
