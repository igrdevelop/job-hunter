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
    APPLY_DELAY_SEC, MAX_JOBS_PER_RUN,
)
from hunter.filters import apply_filters
from hunter.models import Job
from hunter.sources import ALL_SOURCES
from hunter.tracker import (
    get_known_urls, get_known_company_titles, get_sent_companies,
    dedup_key, normalize_url, normalize_company, company_matches_sent,
    add_failed, get_failed_jobs, remove_failed, is_known,
)
from hunter.telegram_bot import send_job_cards, send_text

logger = logging.getLogger(__name__)

_hunt_lock = asyncio.Lock()


async def run_hunt(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for scheduled and manual hunts. Acquires lock to prevent overlap."""
    if _hunt_lock.locked():
        logger.info("[Hunt] Skipped — previous hunt/auto-apply still running")
        await send_text(context, "⏭ Hunt skipped — auto-apply still processing.")
        return

    async with _hunt_lock:
        await _run_hunt_impl(context)


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


async def _run_hunt_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Full hunt cycle:
      1. Fetch jobs from all registered sources
      2. Apply keyword/level/location filters
      3. Deduplicate against tracker.xlsx
      4. AUTO_APPLY=true  → generate docs (with delay between jobs)
         AUTO_APPLY=false → send Telegram cards with Apply/Skip buttons
    """
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    logger.info(f"[Hunt] Starting at {ts}")
    mode = "CLI" if (APPLY_USE_CLI or not LLM_API_KEY) else f"API ({LLM_MODEL})"

    # ── Step 1: Fetch ────────────────────────────────────────────────────────
    all_jobs: list[Job] = []
    fetch_stats: dict[str, int | str] = {}
    for source in ALL_SOURCES:
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

    # ── Step 3: Dedup (URL + company+title + sent-company) ───────────────────
    known_urls = await asyncio.to_thread(get_known_urls)
    known_ct = await asyncio.to_thread(get_known_company_titles)
    sent_companies = await asyncio.to_thread(get_sent_companies)

    seen_urls_this_run: set[str] = set()
    seen_ct_this_run: set[str] = set()
    new_jobs: list[Job] = []
    dup_url = 0
    dup_ct = 0
    dup_sent_company = 0
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
        comp_key = normalize_company(j.company)
        if company_matches_sent(comp_key, sent_companies):
            logger.info(f"[Hunt] Dup sent-company: {j.company} / {j.title}")
            dup_sent_company += 1
            continue
        seen_urls_this_run.add(norm)
        seen_ct_this_run.add(key)
        new_jobs.append(j)

    skipped_total = dup_url + dup_ct + dup_sent_company
    logger.info(f"[Hunt] New: {len(new_jobs)} (dup_url={dup_url}, dup_ct={dup_ct}, dup_sent={dup_sent_company})")

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
        f"  Skipped: {dup_url} by URL, {dup_ct} by company+title, {dup_sent_company} by sent-company"
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
    ok, failed, consecutive_fails = 0, 0, 0

    for i, job in enumerate(jobs, 1):
        await send_text(
            context,
            f"⏳ [{i}/{total}] <b>{job.company}</b> — {job.title}\n"
            f"📍 {job.location}\n"
            f"🔗 {job.url}",
        )

        success = await _run_apply_agent(job)

        if success:
            ok += 1
            consecutive_fails = 0
            await send_text(context, f"✅ [{i}/{total}] Done: {job.company} — {job.title}")
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
    for i, job in enumerate(capped, 1):
        await send_text(
            context,
            f"🔄 [{i}/{len(capped)}] Retry: {job.company} - {job.title}",
        )

        success = await _run_apply_agent(job)
        if success:
            ok += 1
            await asyncio.to_thread(remove_failed, job.url)
            await send_text(context, f"✅ Retry OK: {job.company} - {job.title}")
        else:
            logger.info(f"[retry] Still failing: {job.company} - {job.title}")

        if APPLY_DELAY_SEC > 0:
            await asyncio.sleep(APPLY_DELAY_SEC)

    still_failed = len(capped) - ok
    await send_text(
        context,
        f"🔄 <b>Retry done</b>: ✅ {ok} fixed / ❌ {still_failed} still failing",
    )


async def _run_apply_agent(job: Job) -> bool:
    """Run apply_agent.py as async subprocess, return True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(APPLY_AGENT_PATH),
            job.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                f"[auto-apply] FAIL {job.company}: {stderr.decode(errors='replace')[-500:]}"
            )
            return False

        logger.info(f"[auto-apply] OK {job.company} — {job.title}")
        return True

    except asyncio.TimeoutError:
        logger.error(f"[auto-apply] TIMEOUT for {job.url}")
        return False
    except Exception as e:
        logger.error(f"[auto-apply] exception for {job.url}: {e}")
        return False
