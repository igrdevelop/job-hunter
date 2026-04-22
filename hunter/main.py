"""
main.py — core hunt loop.

run_hunt() is called by:
  - Telegram JobQueue on schedule (08:00 / 13:00 / 19:00)
  - /hunt command for manual trigger
  - Direct call: python hunter.py --now
"""

import asyncio
import logging
import subprocess
import sys
from datetime import datetime

from telegram.ext import ContextTypes

from hunter.config import (
    AUTO_APPLY, APPLY_AGENT_PATH, APPLY_USE_CLI,
    LLM_API_KEY, LLM_PROVIDER, LLM_MODEL,
    APPLY_DELAY_SEC, MAX_JOBS_PER_RUN, APPLY_AGENT_TIMEOUT_SEC,
)
from hunter.filters import apply_filters
from hunter.models import Job
from hunter.services.apply_service import run_apply_agent_subprocess
from hunter.sources import ALL_SOURCES
from hunter.tracker import (
    get_known_urls, get_known_company_titles,
    dedup_key, normalize_url,
    add_failed, get_failed_jobs, remove_failed, is_known,
)
from hunter.telegram_bot import send_job_cards, send_text

logger = logging.getLogger(__name__)

_hunt_lock = asyncio.Lock()


async def run_hunt(
    context: ContextTypes.DEFAULT_TYPE,
    source_names: list[str] | None = None,
) -> None:
    """Entry point for scheduled and manual hunts.

    Args:
        source_names: if given, only run sources whose .name is in this list.
                      None (default) runs all registered sources.
    """
    if _hunt_lock.locked():
        logger.info("[Hunt] Skipped — previous hunt/auto-apply still running")
        await send_text(context, "⏭ Hunt skipped — auto-apply still processing.")
        return

    async with _hunt_lock:
        await _run_hunt_impl(context, source_names=source_names)


def _check_apply_ready() -> str | None:
    """Return None if apply_agent can run, or an error string.

    In API mode: check that LLM_API_KEY is set.
    In CLI mode: check that claude CLI is available.
    """
    if APPLY_USE_CLI or not LLM_API_KEY:
        # CLI mode — verify claude is accessible
        try:
            r = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=15,
            )
            if r.returncode != 0 or "not logged in" in (r.stdout + r.stderr).lower():
                return r.stderr or r.stdout or "unknown error"
            return None
        except FileNotFoundError:
            return "claude CLI not found in PATH"
        except subprocess.TimeoutExpired:
            return "claude --version timed out"
    else:
        # API mode — just need a key
        if not LLM_API_KEY:
            return f"LLM_API_KEY not set (provider={LLM_PROVIDER})"
        return None


async def _run_hunt_impl(
    context: ContextTypes.DEFAULT_TYPE,
    source_names: list[str] | None = None,
) -> None:
    """
    Full hunt cycle:
      1. Fetch jobs from selected sources (all by default, subset when staggered)
      2. Apply keyword/level/location filters
      3. Deduplicate against tracker.xlsx
      4. AUTO_APPLY=true  → generate docs (with delay between jobs)
         AUTO_APPLY=false → send Telegram cards with Apply/Skip buttons
    """
    try:
        from hunter import to_send as _to_send
        sync_result = await asyncio.to_thread(_to_send.sync_and_rebuild)
        if sync_result["synced"]:
            await send_text(
                context,
                f"📬 Synced <b>{sync_result['synced']}</b> Sent mark(s) from to_send.xlsx → tracker.xlsx",
            )
    except Exception as _e:
        logger.warning("[Hunt] to_send sync failed: %s", _e)

    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    logger.info(f"[Hunt] Starting at {ts} sources={source_names or 'all'}")
    mode = "CLI" if (APPLY_USE_CLI or not LLM_API_KEY) else f"API ({LLM_MODEL})"

    # Select sources for this run
    active_sources = (
        [s for s in ALL_SOURCES if s.name in source_names]
        if source_names
        else ALL_SOURCES
    )

    # ── Step 1: Fetch ────────────────────────────────────────────────────────
    all_jobs: list[Job] = []
    fetch_stats: dict[str, int | str] = {}
    for source in active_sources:
        try:
            jobs = await asyncio.to_thread(source.search)
            all_jobs.extend(jobs)
            fetch_stats[source.name] = len(jobs)
            logger.info(f"[Hunt] {source.name}: {len(jobs)} raw jobs")
        except Exception as e:
            fetch_stats[source.name] = f"ERR: {e}"
            logger.error(f"[Hunt] {source.name} error: {e}")

    fetch_lines = "\n".join(
        f"  {name}: <b>{cnt}</b>" if isinstance(cnt, int) else f"  {name}: {cnt}"
        for name, cnt in fetch_stats.items()
    )
    total_raw = sum(v for v in fetch_stats.values() if isinstance(v, int))

    # ── Step 2: Filter ───────────────────────────────────────────────────────
    filtered = apply_filters(all_jobs)
    filtered_out = len(all_jobs) - len(filtered)
    logger.info(f"[Hunt] After filter: {len(filtered)} jobs")

    # ── Step 3: Dedup (URL + company+title) ──────────────────────────────────
    # sent-company filter is intentionally disabled: a company may have multiple
    # open roles and we don't want to block all of them just because one was sent.
    known_urls = await asyncio.to_thread(get_known_urls)
    known_ct = await asyncio.to_thread(get_known_company_titles)

    seen_urls_this_run: set[str] = set()
    seen_ct_this_run: set[str] = set()
    new_jobs: list[Job] = []
    dup_url = 0
    dup_ct = 0
    for j in filtered:
        norm = normalize_url(j.url)
        if norm in known_urls or norm in seen_urls_this_run:
            dup_url += 1
            continue
        key = dedup_key(j.company, j.title)
        if key in known_ct or key in seen_ct_this_run:
            logger.info(f"[Hunt] Dup company+title: {j.company} / {j.title}")
            dup_ct += 1
            continue
        seen_urls_this_run.add(norm)
        seen_ct_this_run.add(key)
        new_jobs.append(j)

    skipped_total = dup_url + dup_ct
    logger.info(f"[Hunt] New: {len(new_jobs)} (dup_url={dup_url}, dup_ct={dup_ct})")

    # ── Send detailed report ─────────────────────────────────────────────────
    report = (
        f"🔍 <b>Hunt {ts}</b>\n"
        f"Mode: {mode}\n\n"
        f"<b>--- Fetch ---</b>\n"
        f"{fetch_lines}\n"
        f"  Total: <b>{total_raw}</b> raw\n\n"
        f"<b>--- Filter ---</b>\n"
        f"  {total_raw} raw -> <b>{len(filtered)}</b> passed ({filtered_out} filtered out)\n\n"
        f"<b>--- Dedup ---</b>\n"
        f"  {len(filtered)} passed -> <b>{len(new_jobs)}</b> new\n"
        f"  Skipped: {dup_url} by URL, {dup_ct} by company+title"
    )
    await send_text(context, report)

    # ── Step 4: Act ──────────────────────────────────────────────────────────
    if not new_jobs:
        return

    if AUTO_APPLY:
        auth_error = await asyncio.to_thread(_check_apply_ready)
        if auth_error:
            await send_text(
                context,
                f"🔐 <b>Apply not ready ({mode})</b>\n\n"
                f"<pre>{auth_error[:300]}</pre>\n\n"
                f"Fix the issue and restart the bot.",
            )
            return

        capped = new_jobs[:MAX_JOBS_PER_RUN]
        skipped_count = len(new_jobs) - len(capped)

        if skipped_count:
            await send_text(
                context,
                f"⚠️ Capped to {MAX_JOBS_PER_RUN} (skipped {skipped_count})",
            )
        await _auto_apply_all(context, capped)

        # Retry previously failed jobs
        await _retry_failed(context)
    else:
        await send_job_cards(context, new_jobs)


# ── Auto-apply pipeline ──────────────────────────────────────────────────────

async def _auto_apply_all(context: ContextTypes.DEFAULT_TYPE, jobs: list[Job]) -> None:
    """Process all jobs sequentially with configurable delay between them."""
    total = len(jobs)
    ok, failed, manual_n, consecutive_fails = 0, 0, 0, 0

    for i, job in enumerate(jobs, 1):
        await send_text(
            context,
            f"⏳ [{i}/{total}] <b>{job.company}</b> — {job.title}\n"
            f"📍 {job.location}\n"
            f"🔗 {job.url}",
        )

        outcome = await _run_apply_agent(job)

        if outcome == "ok":
            ok += 1
            consecutive_fails = 0
            await send_text(context, f"✅ [{i}/{total}] Done: {job.company} — {job.title}")
        elif outcome == "manual":
            manual_n += 1
            consecutive_fails = 0
            await send_text(
                context,
                f"📋 [{i}/{total}] <b>JobLeads — MANUAL</b>: {job.company} — {job.title}\n"
                "См. сообщение выше: допиши <code>job_posting.txt</code> и снова Apply по той же ссылке.\n"
                "<i>tracker.xlsx обновлён, дедуп по URL включён.</i>",
            )
        else:
            failed += 1
            consecutive_fails += 1
            await asyncio.to_thread(add_failed, job)
            await send_text(context, f"❌ [{i}/{total}] Failed: {job.company} — {job.title}")

        if consecutive_fails >= 3:
            remaining = total - i
            await send_text(
                context,
                f"🛑 3 consecutive failures — stopping batch.\n"
                f"Skipped {remaining} remaining jobs.",
            )
            break

        # Delay between jobs (skip after last one)
        if i < total and APPLY_DELAY_SEC > 0:
            await asyncio.sleep(APPLY_DELAY_SEC)

    await send_text(
        context,
        f"🏁 <b>Auto-apply complete</b>\n"
        f"✅ Success: {ok}\n"
        f"📋 MANUAL (JobLeads): {manual_n}\n"
        f"❌ Failed: {failed}\n"
        f"Total: {total}",
    )


async def _retry_failed(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry jobs marked as FAIL in tracker. Remove FAIL row on success."""
    failed_jobs = await asyncio.to_thread(get_failed_jobs)
    if not failed_jobs:
        return

    # Cap retries per run (use same limit as regular hunt)
    capped = failed_jobs[:MAX_JOBS_PER_RUN]
    await send_text(
        context,
        f"🔄 <b>Retrying {len(capped)} previously failed jobs...</b>",
    )

    ok = 0
    manual = 0
    for i, job in enumerate(capped, 1):
        await send_text(
            context,
            f"🔄 [{i}/{len(capped)}] Retry: {job.company} - {job.title}",
        )

        outcome = await _run_apply_agent(job)
        if outcome == "ok":
            ok += 1
            await asyncio.to_thread(remove_failed, job.url)
            await send_text(context, f"✅ Retry OK: {job.company} - {job.title}")
        elif outcome == "manual":
            manual += 1
            await send_text(
                context,
                f"📋 Retry → MANUAL: {job.company} — {job.title}\n"
                "(JobLeads: см. сообщение apply_agent про job_posting.txt)",
            )
        else:
            logger.info(f"[retry] Still failing: {job.company} - {job.title}")

        if APPLY_DELAY_SEC > 0:
            await asyncio.sleep(APPLY_DELAY_SEC)

    still_failed = len(capped) - ok - manual
    await send_text(
        context,
        f"🔄 <b>Retry done</b>: ✅ {ok} fixed / 📋 {manual} manual / ❌ {still_failed} still failing",
    )


async def _run_apply_agent(job: Job):
    """Run apply_agent.py via service wrapper. Returns ``ok`` | ``manual`` | ``fail``."""
    return await run_apply_agent_subprocess(
        job=job,
        timeout_sec=APPLY_AGENT_TIMEOUT_SEC,
        apply_agent_path=APPLY_AGENT_PATH,
        python_executable=sys.executable,
    )
