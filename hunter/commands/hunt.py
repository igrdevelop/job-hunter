"""hunter/commands/hunt.py — Hunt, force, and manual-processing command handlers."""

import asyncio
import logging
import sys

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import APPLY_AGENT_PATH

logger = logging.getLogger(__name__)

_APPLY_AGENT_TIMEOUT = 900  # 15-min hard cap; mirrors telegram_bot._APPLY_AGENT_TIMEOUT


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
    import re
    from hunter.telegram_bot import _extract_url, _handle_paste, _looks_like_paste, _run_apply_agent

    raw = (update.message.text or "").strip()
    m = re.match(r"/force(?:@\w+)?\s*(.*)\Z", raw, flags=re.DOTALL | re.IGNORECASE)
    body = (m.group(1) or "").strip() if m else ""

    if not body:
        await update.message.reply_text(
            "<b>/force</b> — принудительная генерация (<code>--force</code>):\n\n"
            "• <code>/force https://…</code> — по ссылке\n"
            "• <code>/force</code> и с новой строки (или через пробел) полный текст вакансии — "
            "как обычная вставка, но с обходом дедупа и React-only\n\n"
            "Текст должен быть достаточно длинным (как при вставке JD), иначе пришли http-ссылку.",
            parse_mode=ParseMode.HTML,
        )
        return

    if _looks_like_paste(body):
        await update.message.reply_text(
            f"🔧 <b>Force + текст вакансии</b> — {len(body.strip())} симв. "
            "Обход: дедуп трекера, React-only. Запускаю…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[Force] paste mode ({len(body)} chars)")
        await _handle_paste(update, body, force=True)
        return

    if body.startswith("http"):
        url = _extract_url(body) or body.split()[0].strip()
        await update.message.reply_text(
            f"⏳ <b>Force: запускаю генерацию</b> (<code>--force</code>)\n"
            f"🔗 {url}\n\n"
            "Обход: дедуп трекера, React-only skip; для JobLeads — вставленный "
            "<code>job_posting.txt</code> подставится при fetch.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[Force] Launching apply_agent --force for: {url}")
        asyncio.create_task(_run_apply_agent(url, force=True))
        return

    await update.message.reply_text(
        "После <code>/force</code> нужна <b>http(s)-ссылка</b> или длинный текст вакансии "
        "(как при обычной вставке). Одно слово без ссылки не подходит.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process all MANUAL-pending tracker rows whose job_posting.txt is already filled."""
    from hunter.tracker import get_all_manual_pending
    from job_fetch.jobleads import try_load_manual_job_posting  # type: ignore[import]

    rows = await asyncio.to_thread(get_all_manual_pending)
    if not rows:
        await update.message.reply_text("✅ Нет MANUAL вакансий для обработки.")
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
            f"📝 <b>Найдено {len(rows)} MANUAL вакансий, но ни одна не готова.</b>\n\n"
            + "\n".join(lines)
            + "\n\nДобавь текст вакансии под маркером в <code>job_posting.txt</code> и повтори.",
            parse_mode=ParseMode.HTML,
        )
        return

    not_ready_count = len(rows) - len(ready)
    note = f" (ещё {not_ready_count} ожидают текста)" if not_ready_count else ""
    await update.message.reply_text(
        f"🚀 <b>Запускаю обработку {len(ready)} готовых вакансий{note}…</b>",
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
        f"🏁 <b>process_manual завершён</b>\n✅ {ok} / ❌ {failed} / Всего: {total}",
        parse_mode=ParseMode.HTML,
    )
