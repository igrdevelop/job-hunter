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
    AUTO_APPLY,
    APPLY_AGENT_PATH,
    APPLY_USE_CLI,
    LLM_API_KEY,
    LLM_PROVIDER,
    LLM_MODEL,
    APPLY_DELAY_SEC,
    MAX_JOBS_PER_RUN,
    APPLY_AGENT_TIMEOUT_SEC,
    GMAIL_MAX_RESULTS,
)
from hunter.filters import apply_filters_with_stats, classify_job
from hunter.gmail_report import build_gmail_report, JobOutcome
from hunter.models import Job
from hunter.services.apply_service import run_apply_agent_subprocess
from hunter.sources import ALL_SOURCES
from hunter.tracker import (
    get_known_urls,
    get_known_company_titles,
    dedup_key,
    normalize_url,
    add_failed,
    get_failed_jobs,
    remove_failed,
    increment_fail_count,
    is_in_cooldown,
    MAX_FAIL_RETRIES,
)
from hunter.telegram_bot import send_job_cards, send_text

logger = logging.getLogger(__name__)

_hunt_lock = asyncio.Lock()

# Stop a batch after this many failures in a row. Protects a rate-limited or
# down host (e.g. pracuj.pl returning 429) from being hammered job after job.
_CONSECUTIVE_FAIL_LIMIT = 3


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
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
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
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    logger.info(f"[Hunt] Starting at {ts} sources={source_names or 'all'}")
    mode = "CLI" if (APPLY_USE_CLI or not LLM_API_KEY) else f"API ({LLM_MODEL})"

    # Select sources for this run
    active_sources = (
        [s for s in ALL_SOURCES if s.name in source_names] if source_names else ALL_SOURCES
    )

    # ── Step 1: Fetch ────────────────────────────────────────────────────────
    all_jobs: list[Job] = []
    fetch_stats: dict[str, int | str] = {}
    gmail_source = None  # captured for its per-email diagnostics (last_email_log)
    broken_sources: list[str] = []  # sources that just crossed the breakage threshold
    for source in active_sources:
        try:
            jobs = await asyncio.to_thread(source.search)
            all_jobs.extend(jobs)
            fetch_stats[source.name] = len(jobs)
            logger.info(f"[Hunt] {source.name}: {len(jobs)} raw jobs")
            _record_source_health(source.name, len(jobs), ok=True, broken=broken_sources)
        except Exception as e:
            fetch_stats[source.name] = f"ERR: {e}"
            logger.error(f"[Hunt] {source.name} error: {e}")
            _record_source_health(source.name, 0, ok=False, error=str(e), broken=broken_sources)

        # Gmail: capture the source for its per-email diagnostics, and upload the
        # log snapshot to Drive right after the scan so the Drive copy reflects the
        # full email pipeline trace (who sent, which URLs were extracted, enrichment
        # results) while the rest of the hunt continues.  Fire-and-forget via
        # create_task — doesn't block fetching.
        if source.name == "gmail":
            gmail_source = source
            asyncio.get_event_loop().create_task(
                _upload_log_to_drive(),
                name="gmail_log_upload",
            )

    fetch_lines = "\n".join(
        f"  {name}: <b>{cnt}</b>" if isinstance(cnt, int) else f"  {name}: {cnt}"
        for name, cnt in fetch_stats.items()
    )
    total_raw = sum(v for v in fetch_stats.values() if isinstance(v, int))

    # Alert once when a previously-working source goes dry (likely broken scraper).
    if broken_sources:
        await send_text(
            context,
            "⚠️ <b>Scraper(s) may be broken</b>\n"
            + "\n".join(f"  • <b>{n}</b> — 0 jobs for several runs" for n in broken_sources)
            + "\n\nRun <code>/health</code> for details, or the scraper-health-checker agent.",
        )

    # ── Step 2: Filter ───────────────────────────────────────────────────────
    filtered, filter_reasons = apply_filters_with_stats(all_jobs)
    filtered_out = len(all_jobs) - len(filtered)
    logger.info(f"[Hunt] After filter: {len(filtered)} jobs")

    # Per-email report bookkeeping: record the fate of every Gmail-sourced job.
    # Filtered-out gmail jobs are tagged here with their exact filter reason
    # (classify_job is the same per-job core apply_filters used above); taken /
    # deduplicated ones are tagged in the dedup loop below.
    gmail_outcomes: list[JobOutcome] = []
    filtered_ids = {id(j) for j in filtered}
    for j in all_jobs:
        if j.source.startswith("gmail_") and id(j) not in filtered_ids:
            gmail_outcomes.append(JobOutcome.from_job(j, "filtered", classify_job(j)))

    # ── Step 3: Dedup (URL + company+title) ──────────────────────────────────
    # sent-company filter is intentionally disabled: a company may have multiple
    # open roles and we don't want to block all of them just because one was sent.
    try:
        known_urls = await asyncio.to_thread(get_known_urls)
        known_ct = await asyncio.to_thread(get_known_company_titles)
    except Exception as e:
        logger.exception("[Hunt] Failed to read tracker DB for dedup")
        hint = str(e)[:400]
        await send_text(
            context,
            f"❌ <b>Failed to read tracker DB</b> (dedup before hunt).\n\n<pre>{hint}</pre>",
        )
        return

    seen_urls_this_run: set[str] = set()
    seen_ct_this_run: set[str] = set()
    new_jobs: list[Job] = []
    dup_url = 0
    dup_ct = 0
    dup_cooldown = 0
    for j in filtered:
        is_gmail = j.source.startswith("gmail_")
        norm = normalize_url(j.url)
        if norm in known_urls or norm in seen_urls_this_run:
            dup_url += 1
            if is_gmail:
                gmail_outcomes.append(JobOutcome.from_job(j, "dup_url"))
            continue
        key = dedup_key(j.company, j.title)
        if key in known_ct or key in seen_ct_this_run:
            logger.info(f"[Hunt] Dup company+title: {j.company} / {j.title}")
            dup_ct += 1
            if is_gmail:
                gmail_outcomes.append(JobOutcome.from_job(j, "dup_ct"))
            continue
        # Fuzzy title dedup — catches Gmail-enriched variants such as
        # "Remote Angular Developer" vs stored "Angular Developer" (same company).
        # Only called when URL and exact CT checks both miss (hot path unaffected).
        try:
            from hunter.tracker_cache import cache as _cache

            if await _cache.is_fuzzy_ct(j.company, j.title):
                logger.info(f"[Hunt] Fuzzy dup company+title: {j.company} / {j.title}")
                dup_ct += 1
                if is_gmail:
                    gmail_outcomes.append(JobOutcome.from_job(j, "dup_ct"))
                continue
        except Exception as _fe:
            logger.debug("[Hunt] fuzzy CT check failed: %s", _fe)
        if await asyncio.to_thread(is_in_cooldown, j.company, j.title):
            logger.info(f"[Hunt] Cooldown: {j.company} / {j.title}")
            dup_cooldown += 1
            if is_gmail:
                gmail_outcomes.append(JobOutcome.from_job(j, "cooldown"))
            continue
        seen_urls_this_run.add(norm)
        seen_ct_this_run.add(key)
        new_jobs.append(j)
        if is_gmail:
            gmail_outcomes.append(JobOutcome.from_job(j, "taken"))

    logger.info(
        f"[Hunt] New: {len(new_jobs)} (dup_url={dup_url}, dup_ct={dup_ct}, cooldown={dup_cooldown})"
    )

    # ── Send detailed report ─────────────────────────────────────────────────
    report = (
        f"🔍 <b>Hunt {ts}</b>\n"
        f"Mode: {mode}\n\n"
        f"<b>--- Fetch ---</b>\n"
        f"{fetch_lines}\n"
        f"  Total: <b>{total_raw}</b> raw\n\n"
        f"<b>--- Filter ---</b>\n"
        f"  {total_raw} raw -> <b>{len(filtered)}</b> passed ({filtered_out} filtered out)\n"
        + ("".join(f"  ✂️ {cnt} by {reason}\n" for reason, cnt in filter_reasons.items() if cnt > 0))
        + "\n"
        f"<b>--- Dedup ---</b>\n"
        f"  {len(filtered)} passed -> <b>{len(new_jobs)}</b> new\n"
        f"  Skipped: {dup_url} by URL, {dup_ct} by company+title"
    )
    await send_text(context, report)

    # ── Per-email Gmail report (separate message(s) to avoid 4096 truncation) ──
    if gmail_source is not None:
        chunks = build_gmail_report(
            getattr(gmail_source, "last_email_log", []),
            getattr(gmail_source, "last_capped", False),
            GMAIL_MAX_RESULTS,
            gmail_outcomes,
        )
        for chunk in chunks:
            await send_text(context, chunk)

    # ── Step 4: Act ──────────────────────────────────────────────────────────
    if not new_jobs:
        return

    # manual_only sources (e.g. linkedin_scout_relay — a regex-heuristic match,
    # not a structured job-board listing) ALWAYS get a Telegram Apply/Skip
    # card, even under AUTO_APPLY — a human should confirm before any LLM
    # spend. Partition before the AUTO_APPLY branch so both paths honor it.
    _sources_by_name = {s.name: s for s in active_sources}
    manual_only_jobs: list[Job] = []
    auto_eligible_jobs: list[Job] = []
    for j in new_jobs:
        if getattr(_sources_by_name.get(j.source), "manual_only", False):
            manual_only_jobs.append(j)
        else:
            auto_eligible_jobs.append(j)

    if manual_only_jobs:
        await send_job_cards(context, manual_only_jobs)

    if not auto_eligible_jobs:
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

        capped = auto_eligible_jobs[:MAX_JOBS_PER_RUN]
        skipped_count = len(auto_eligible_jobs) - len(capped)

        if skipped_count:
            await send_text(
                context,
                f"⚠️ Capped to {MAX_JOBS_PER_RUN} (skipped {skipped_count})",
            )
        await _auto_apply_all(context, capped)

        # Retry previously failed jobs
        await _retry_failed(context)
    else:
        await send_job_cards(context, auto_eligible_jobs)


# ── Scraper health ────────────────────────────────────────────────────────────


def _record_source_health(
    source_name: str,
    yield_count: int,
    *,
    ok: bool,
    error: str = "",
    broken: list[str],
) -> None:
    """Record one source run and append to `broken` if it just crossed the
    breakage threshold. Best-effort — telemetry must never break a hunt."""
    try:
        from hunter.config import SOURCE_HEALTH_ENABLED

        if not SOURCE_HEALTH_ENABLED:
            return
        from hunter import source_health

        source_health.record_run(source_name, yield_count, ok=ok, error=error)
        if source_health.newly_broken(source_name):
            broken.append(source_name)
    except Exception as e:  # noqa: BLE001
        logger.debug("[Hunt] source health record failed for %s: %s", source_name, e)


# ── Auto-apply pipeline ──────────────────────────────────────────────────────


async def _sync_to_sheets(url: str) -> None:
    """Mirror a just-applied row to Google Sheets (best-effort)."""
    try:
        from hunter.tracker_cache import cache
        from hunter import gsheets_sync

        await cache.load_from_db()
        row = await cache.get_row_by_url(url)
        if row:
            await gsheets_sync.mirror_new_row(row)
    except Exception as _e:
        logger.warning("[auto_apply] gsheets mirror failed for %s: %s", url, _e)


async def _upload_to_drive(url: str) -> None:
    """Upload application folder to Google Drive immediately after apply (best-effort)."""
    try:
        from hunter.config import GDRIVE_ENABLED

        if not GDRIVE_ENABLED:
            return
        from hunter.tracker import get_folder_by_url
        from hunter.config import PROJECT_DIR
        from hunter import gdrive_sync

        folder_str = await asyncio.to_thread(get_folder_by_url, url)
        if folder_str:
            await gdrive_sync.upload_application_folder(PROJECT_DIR / folder_str, job_url=url)
    except Exception as _e:
        logger.warning("[auto_apply] gdrive upload failed for %s: %s", url, _e)


async def _upload_log_to_drive() -> None:
    """Upload hunter_errors.log to Drive immediately after gmail scan (best-effort).

    Called right after GmailSource.search() returns so the Drive copy reflects
    the full gmail pipeline trace (emails processed, URLs extracted, enrichment
    results) while the rest of the hunt is still running.
    """
    try:
        from hunter.config import GDRIVE_ENABLED, PROJECT_DIR

        if not GDRIVE_ENABLED:
            return
        from hunter import gdrive_sync

        await gdrive_sync.upload_log_file(PROJECT_DIR / "logs" / "hunter_errors.log")
        logger.debug("[gmail] log snapshot uploaded to Drive")
    except Exception as _e:
        logger.debug("[gmail] log upload to Drive failed: %s", _e)


async def _auto_apply_all(context: ContextTypes.DEFAULT_TYPE, jobs: list[Job]) -> None:
    """Process all jobs sequentially with configurable delay between them."""
    total = len(jobs)
    ok, failed, manual_n, consecutive_fails = 0, 0, 0, 0

    for i, job in enumerate(jobs, 1):
        text = (
            f"⏳ [{i}/{total}] <b>{job.company}</b> — {job.title}\n📍 {job.location}\n🔗 {job.url}"
        )
        permalink = job.raw.get("permalink")
        if permalink:
            text += f"\n🔗 Post: {permalink}"
        await send_text(context, text)

        outcome = await _run_apply_agent(job)

        if outcome == "ok":
            ok += 1
            consecutive_fails = 0
            await _sync_to_sheets(job.url)
            await _upload_to_drive(job.url)
            done_text = f"✅ [{i}/{total}] Done: {job.company} — {job.title}"
            if permalink:
                done_text += f"\n🔗 Post: {permalink}"
            await send_text(context, done_text)
        elif outcome == "manual":
            manual_n += 1
            consecutive_fails = 0
            await send_text(
                context,
                f"📋 [{i}/{total}] <b>JobLeads — MANUAL</b>: {job.company} — {job.title}\n"
                "See message above: fill in <code>job_posting.txt</code> and Apply again with the same URL.\n"
                "<i>Tracker updated, URL dedup active.</i>",
            )
        else:
            failed += 1
            consecutive_fails += 1
            await asyncio.to_thread(add_failed, job)
            if outcome == "rate_limited":
                await send_text(
                    context,
                    f"⏳ [{i}/{total}] Rate-limited (429): {job.company} — {job.title} "
                    "— will retry later.",
                )
            else:
                await send_text(context, f"❌ [{i}/{total}] Failed: {job.company} — {job.title}")

        if consecutive_fails >= _CONSECUTIVE_FAIL_LIMIT:
            remaining = total - i
            await send_text(
                context,
                f"🛑 {_CONSECUTIVE_FAIL_LIMIT} consecutive failures — stopping batch.\n"
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
    consecutive_fails = 0
    processed = 0
    for i, job in enumerate(capped, 1):
        processed = i
        await send_text(
            context,
            f"🔄 [{i}/{len(capped)}] Retry: {job.company} - {job.title}",
        )

        outcome = await _run_apply_agent(job)
        if outcome == "ok":
            ok += 1
            consecutive_fails = 0
            await asyncio.to_thread(remove_failed, job.url)
            await _sync_to_sheets(job.url)
            await _upload_to_drive(job.url)
            await send_text(context, f"✅ Retry OK: {job.company} - {job.title}")
        elif outcome == "manual":
            manual += 1
            consecutive_fails = 0
            await send_text(
                context,
                f"📋 Retry → MANUAL: {job.company} — {job.title}\n"
                "(JobLeads: see apply_agent message about job_posting.txt)",
            )
        elif outcome == "rate_limited":
            # Transient 429 — count it for the breaker but do NOT escalate the
            # permanent fail counter; the offer itself is likely fine.
            consecutive_fails += 1
            logger.warning(
                "[retry] Rate-limited (429), not escalating: %s - %s",
                job.company,
                job.title,
            )
            await send_text(
                context,
                f"⏳ Retry rate-limited (429): {job.company} — {job.title} "
                "— will retry next cycle.",
            )
        else:
            consecutive_fails += 1
            new_count = await asyncio.to_thread(increment_fail_count, job.url)
            if new_count >= MAX_FAIL_RETRIES:
                logger.warning(
                    "[retry] Giving up on %s - %s after %d failures",
                    job.company,
                    job.title,
                    new_count,
                )
                await send_text(
                    context,
                    f"🚫 <b>Giving up</b> on {job.company} — {job.title} "
                    f"(failed {new_count}× — won't retry again).",
                )
            else:
                logger.info(
                    "[retry] Still failing (%d/%d): %s - %s",
                    new_count,
                    MAX_FAIL_RETRIES,
                    job.company,
                    job.title,
                )

        # Stop hammering a rate-limited/down host after N failures in a row.
        if consecutive_fails >= _CONSECUTIVE_FAIL_LIMIT:
            remaining = len(capped) - i
            await send_text(
                context,
                f"🛑 {_CONSECUTIVE_FAIL_LIMIT} consecutive failures — stopping retries.\n"
                f"Skipped {remaining} remaining.",
            )
            break

        if APPLY_DELAY_SEC > 0:
            await asyncio.sleep(APPLY_DELAY_SEC)

    still_failed = processed - ok - manual
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
