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
from hunter.tracker import get_known_urls, get_known_company_titles, dedup_key, add_failed
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

    # Step 1 — fetch
    all_jobs = []
    for source in ALL_SOURCES:
        try:
            jobs = await asyncio.to_thread(source.search)
            all_jobs.extend(jobs)
            logger.info(f"[Hunt] {source.name}: {len(jobs)} raw jobs")
        except Exception as e:
            logger.error(f"[Hunt] {source.name} error: {e}")

    # Step 2 — filter
    filtered = apply_filters(all_jobs)
    logger.info(f"[Hunt] After filter: {len(filtered)} jobs")

    # Step 3 — dedup (URL + company+title)
    known_urls = await asyncio.to_thread(get_known_urls)
    known_ct = await asyncio.to_thread(get_known_company_titles)

    seen_ct_this_run: set[str] = set()
    new_jobs: list[Job] = []
    skipped_dup = 0
    for j in filtered:
        if j.url in known_urls:
            continue
        key = dedup_key(j.company, j.title)
        if key in known_ct or key in seen_ct_this_run:
            logger.info(f"[Hunt] Skipping dup company+title: {j.company} / {j.title}")
            skipped_dup += 1
            continue
        seen_ct_this_run.add(key)
        new_jobs.append(j)
    logger.info(f"[Hunt] New: {len(new_jobs)} jobs (skipped {skipped_dup} company+title dups)")

    # Step 4 — act
    if not new_jobs:
        await send_text(context, f"🔍 Hunt {ts}\nНет новых вакансий.")
        return

    if AUTO_APPLY:
        auth_error = await asyncio.to_thread(_check_apply_ready)
        if auth_error:
            mode = "CLI" if (APPLY_USE_CLI or not LLM_API_KEY) else "API"
            await send_text(
                context,
                f"🔐 <b>Apply not ready ({mode} mode)</b>\n\n"
                f"<pre>{auth_error[:300]}</pre>\n\n"
                f"Fix the issue and restart the bot.",
            )
            return

        # Cap jobs per run
        capped = new_jobs[:MAX_JOBS_PER_RUN]
        skipped_count = len(new_jobs) - len(capped)

        mode = "CLI" if (APPLY_USE_CLI or not LLM_API_KEY) else f"API ({LLM_MODEL})"
        await send_text(
            context,
            f"🤖 <b>AUTO-APPLY</b>: {len(capped)} jobs ({ts})\n"
            f"Mode: {mode}\n"
            f"Sources: {', '.join(s.name for s in ALL_SOURCES)}\n"
            + (f"⚠️ Capped from {len(new_jobs)} (MAX_JOBS_PER_RUN={MAX_JOBS_PER_RUN})\n" if skipped_count else "")
            + f"Delay: {APPLY_DELAY_SEC}s between jobs",
        )
        await _auto_apply_all(context, capped)
    else:
        await send_text(
            context,
            f"🎯 Найдено <b>{len(new_jobs)}</b> новых вакансий ({ts})\n"
            f"Источники: {', '.join(s.name for s in ALL_SOURCES)}",
        )
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
