"""
telegram_bot.py — Telegram send layer, shared state, apply dispatch.

Public API (used by hunter/main.py and hunter/app.py):
  send_text(context, text)
  send_job_cards(context, jobs)

Shared state (used by commands/ modules):
  _pending_jobs, _active_apply_urls, _save_pending, _load_pending
  _run_apply_agent, _looks_like_paste, _extract_url, _handle_paste

Pending jobs (Apply/Skip buttons) are persisted to pending_jobs.json so
buttons survive bot restarts.
"""

import asyncio
import dataclasses
import json
import logging
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import (
    APPLY_AGENT_PATH,
    PROJECT_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TRACKER_PATH,
)
from hunter.models import Job
from hunter.tracker import (
    add_skipped,
    lookup_url,
    manual_jobleads_job_posting_path,
    normalize_url,
)

logger = logging.getLogger(__name__)

_PENDING_JOBS_FILE = PROJECT_DIR / "pending_jobs.json"

# In-memory store: job_id → Job. Persisted to disk so buttons survive restarts.
_pending_jobs: dict[str, Job] = {}

# URLs currently being processed by _run_apply_agent (for /status display).
_active_apply_urls: set[str] = set()

_APPLY_AGENT_TIMEOUT = 900  # 15-min hard cap


def _save_pending() -> None:
    try:
        data = {jid: dataclasses.asdict(job) for jid, job in _pending_jobs.items()}
        _PENDING_JOBS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("[pending] save failed: %s", exc)


def _load_pending() -> None:
    if not _PENDING_JOBS_FILE.exists():
        return
    try:
        data = json.loads(_PENDING_JOBS_FILE.read_text(encoding="utf-8"))
        for jid, job_dict in data.items():
            _pending_jobs[jid] = Job(**job_dict)
        logger.info("[pending] restored %d pending job(s) from disk", len(_pending_jobs))
    except Exception as exc:
        logger.warning("[pending] load failed (ignoring): %s", exc)


# ── Keyboard factory ─────────────────────────────────────────────────────────

def _make_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"apply:{job_id}"),
        InlineKeyboardButton("❌ Skip",  callback_data=f"skip:{job_id}"),
    ]])


# ── Public send API ───────────────────────────────────────────────────────────

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
    if jobs:
        _save_pending()


# ── Telegram notification (no context required) ──────────────────────────────

async def _tg_notify(text: str) -> None:
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


# ── Apply dispatch ────────────────────────────────────────────────────────────

async def _run_apply_agent(
    url: str,
    force: bool = False,
    paste_file: Optional[str] = None,
) -> None:
    """Run apply_agent.py via apply_service; does not block the event loop."""
    from hunter.services.apply_service import run_apply_agent_for_url

    label = url or "(pasted text)"
    if url:
        _active_apply_urls.add(url)
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
            err_block = f"\n\n<pre>{error_detail[:800]}</pre>" if error_detail else ""
            await _tg_notify(f"❌ <b>apply_agent завершился с ошибкой</b>\n🔗 {label}{err_block}")
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
            try:
                from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
                if GDRIVE_ENABLED:
                    from hunter.tracker import get_folder_by_url
                    folder_str = await asyncio.to_thread(get_folder_by_url, url)
                    if folder_str:
                        from hunter import gdrive_sync
                        drive_url = await gdrive_sync.upload_application_folder(
                            PROJECT_DIR / folder_str
                        )
                        if drive_url:
                            await _tg_notify(
                                f'📁 <a href="{drive_url}">Открыть папку на Drive</a>'
                            )
            except Exception as _e:
                logger.warning("[apply_agent] gdrive upload failed: %s", _e)
    except Exception as e:
        logger.error(f"[apply_agent] exception: {e}")
        await _tg_notify(f"❌ <b>apply_agent exception</b>\n{e}\n🔗 {label}")
    finally:
        _active_apply_urls.discard(url)
        if paste_file:
            try:
                Path(paste_file).unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(
                    f"[apply_agent] could not delete paste file {paste_file}: {cleanup_err}"
                )


async def _run_linkedin_batch(job_ids: list[str], update) -> None:
    from job_fetch.linkedin_parse import job_view_url  # type: ignore[import]

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
        await update.message.reply_text(
            f"🏁 <b>LinkedIn batch done</b>\n✅ {ok} / ❌ {failed} / Total: {total}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


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
    _save_pending()
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
    _save_pending()

    original = query.message.text
    await query.edit_message_text(
        original + "\n\n⏳ <i>Generating documents...</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )
    logger.info(f"[Apply] Launching apply_agent for: {job.url}")
    asyncio.create_task(_run_apply_agent(job.url))


# ── URL / paste message handler ───────────────────────────────────────────────

_PASTE_TEXT_MIN_LEN = 200
_URL_RE = re.compile(r"https?://\S+")


def _looks_like_paste(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < _PASTE_TEXT_MIN_LEN:
        return False
    urls = _URL_RE.findall(stripped)
    if urls:
        non_url_len = len(_URL_RE.sub("", stripped).strip())
        return non_url_len >= _PASTE_TEXT_MIN_LEN
    return True


def _extract_url(text: str) -> str:
    m = _URL_RE.search(text)
    return m.group(0).rstrip(").,;") if m else ""


async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages: paste flow, single URL, or LinkedIn batch."""
    text = (update.message.text or "").strip()

    if _looks_like_paste(text):
        n = len(text.strip())
        await update.message.reply_text(
            f"📥 <b>Текст вакансии получен</b> — {n} симв. Сохраняю и проверяю трекер…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await _handle_paste(update, text)
        return

    if not text.startswith("http"):
        await update.message.reply_text(
            "ℹ️ Отправь ссылку на вакансию (начинается с http) и я сгенерирую документы.\n"
            "Либо вставь сюда полный текст вакансии (можно с ссылкой или без) — "
            "я обработаю его напрямую.\n\n"
            "Также можно отправить ссылку из LinkedIn алерта — вытащу все вакансии из неё.",
            parse_mode=ParseMode.HTML,
        )
        return

    from job_fetch.linkedin_parse import (  # type: ignore[import]
        is_linkedin_search,
        normalize_linkedin_url,
        parse_linkedin_job_ids,
    )
    from hunter.config import MAX_JOBS_PER_RUN

    text = normalize_linkedin_url(text)

    if is_linkedin_search(text):
        job_ids = parse_linkedin_job_ids(text)
        if not job_ids:
            await update.message.reply_text(
                "⚠️ LinkedIn ссылка распознана, но id вакансий не найдены.\n"
                "Попробуй прислать прямую ссылку на конкретную вакансию.",
                parse_mode=ParseMode.HTML,
            )
            return

        capped = job_ids[:MAX_JOBS_PER_RUN]
        skipped = len(job_ids) - len(capped)
        msg = (
            f"🔗 <b>LinkedIn алерт</b>: найдено <b>{len(job_ids)}</b> вакансий\n"
            + (f"⚠️ Обрабатываю первые {MAX_JOBS_PER_RUN} (MAX_JOBS_PER_RUN)\n" if skipped else "")
            + "Запускаю последовательно..."
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
            from job_fetch.jobleads import try_load_manual_job_posting  # type: ignore[import]
            manual_content = await asyncio.to_thread(try_load_manual_job_posting, text)
            if manual_content:
                await update.message.reply_text(
                    f"✅ <b>Текст вакансии найден в файле — запускаю генерацию…</b>\n"
                    f"🔗 {text}\n\nЭто займёт 1-2 минуты.",
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
                    f"📝 <b>Вакансия ожидает текста (MANUAL)</b>\n\n"
                    f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}{folder_info}\n\n"
                    f"Вставь полный текст вакансии под маркером в <code>job_posting.txt</code> и пришли эту ссылку ещё раз.\n"
                    f"Либо отправь сюда текст вакансии (можно вместе со ссылкой) — обработаю сразу.",
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
            f"⚠️ <b>Эта вакансия уже в трекере!</b>\n\n"
            f"{detail}\n\n"
            f"Отправь /force {text}\nесли хочешь обработать заново.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text(
        f"⏳ <b>Запускаю генерацию...</b>\n🔗 {text}\n\nЭто займёт 1-2 минуты.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(f"[URL handler] Launching apply_agent for: {text}")
    asyncio.create_task(_run_apply_agent(text))


async def _handle_paste(update: Update, text: str, force: bool = False) -> None:
    """Save pasted job text to temp file and run apply_agent in paste mode."""
    from job_fetch.jobleads import JOBLEADS_PASTE_MARKER  # type: ignore[import]

    url = _extract_url(text)
    url_inferred = False

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
                f"⚠️ <b>Эта вакансия уже в трекере!</b>\n\n"
                f"{detail}\n\n"
                f"Отправь <code>/force {url}</code> или <code>/force</code> с полным текстом.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

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
                    f"❌ Не удалось записать текст в <code>{jp}</code>\n<pre>{str(e)[:500]}</pre>",
                    parse_mode=ParseMode.HTML,
                )
                return

            inferred_note = " (URL восстановил из трекера)" if url_inferred else ""
            force_note = " <code>--force</code>" if force else ""
            await update.message.reply_text(
                "✅ <b>Подтверждаю:</b> текст записан в <code>job_posting.txt</code>, "
                f"запускаю генерацию документов{force_note}.\n"
                f"🔗 {url}{inferred_note}\n\n"
                "Ориентировочно 1–2 мин; готовые файлы пришлю отдельным сообщением.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info(
                f"[paste handler] Updated MANUAL job_posting.txt and rerun apply url={url} force={force}"
            )
            asyncio.create_task(_run_apply_agent(url, force=force))
            return

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", prefix="tg_paste_", delete=False,
        )
        with tmp as fh:
            fh.write(text)
        paste_path = tmp.name
    except Exception as e:
        logger.exception("[paste handler] failed to write temp file")
        await update.message.reply_text(
            f"❌ Не удалось сохранить присланный текст во временный файл: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    chars = len(text)
    if url:
        inferred_note = " (URL восстановил из трекера)" if url_inferred else ""
        url_line = f"🔗 {url}{inferred_note}"
    else:
        url_line = "🔗 (ссылка не найдена — обрабатываю без неё)"
    mode = "режим вставки + <code>--force</code>" if force else "режим вставки"
    await update.message.reply_text(
        "✅ <b>Подтверждаю:</b> текст сохранён, запускаю <code>apply_agent</code> "
        f"({mode}, {chars} симв.).\n"
        f"{url_line}\n\n"
        "Ориентировочно 1–2 мин; результат пришлю сюда же.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(
        f"[paste handler] Launching apply_agent paste mode ({chars} chars) url={url or '—'} force={force}"
    )
    asyncio.create_task(_run_apply_agent(url, force=force, paste_file=paste_path))

