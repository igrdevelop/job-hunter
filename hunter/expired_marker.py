"""
hunter/expired_marker.py — Check unsent tracker rows for expired job offers.

Reads unsent rows directly from tracker.xlsx.
Marks expired rows in both tracker.xlsx and the in-memory cache.

Used by /check_expired (Telegram) and the daily scheduled check.
"""

import asyncio
import logging
from typing import Callable, Awaitable
from urllib.parse import urlparse

import requests

from hunter.config import (
    EXPIRED_CHECK_CONCURRENCY,
    EXPIRED_CHECK_DOMAIN_LIMIT,
    EXPIRED_CHECK_DOMAIN_DELAY,
)
from hunter.expired_check import is_job_expired
from hunter.tracker import apply_sent_updates, iter_unsent_rows as _iter_unsent

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 5


# ── Per-domain rate limiting ──────────────────────────────────────────────────

def _domain(url: str) -> str:
    return urlparse(url).hostname or url


class _DomainLimiter:
    def __init__(self, domain_limit: int, domain_delay: float) -> None:
        self._limit = domain_limit
        self._delay = domain_delay
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._delays: dict[str, asyncio.Lock] = {}

    def _get(self, dom: str):
        if dom not in self._sems:
            self._sems[dom] = asyncio.Semaphore(self._limit)
            self._delays[dom] = asyncio.Lock()
        return self._sems[dom], self._delays[dom]

    async def fetch(self, url: str, global_sem: asyncio.Semaphore) -> str:
        from job_fetch import fetch_job_text
        dom = _domain(url)
        dom_sem, dom_lock = self._get(dom)
        async with global_sem:
            async with dom_sem:
                result = await asyncio.to_thread(fetch_job_text, url)
                async with dom_lock:
                    await asyncio.sleep(self._delay)
                return result


# ── Core check ────────────────────────────────────────────────────────────────

async def run_check(
    progress_cb: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """
    Fetch each unsent tracker row URL, detect expiry, write EXPIRED to tracker.

    Returns:
        {
            "total":   int,
            "alive":   int,
            "expired": list[dict],   # {id, company, title, url}
            "errors":  list[dict],   # {id, company, title, error}
            "skipped": list[dict],
            "marked":  int,
        }
    """
    rows = await asyncio.to_thread(_iter_unsent)
    to_check = [r for r in rows if r.get("url")]

    total = len(to_check)
    done = 0
    expired_count = 0

    global_sem = asyncio.Semaphore(EXPIRED_CHECK_CONCURRENCY)
    limiter = _DomainLimiter(EXPIRED_CHECK_DOMAIN_LIMIT, EXPIRED_CHECK_DOMAIN_DELAY)

    async def _check_one(item: dict) -> dict:
        nonlocal done, expired_count

        if "jobleads.com" in item["url"]:
            done += 1
            if progress_cb and done % PROGRESS_EVERY == 0:
                await progress_cb(f"⏳ {done}/{total} проверено — ⏭ истекло: {expired_count}")
            return {**item, "status": "skipped"}

        try:
            text = await limiter.fetch(item["url"], global_sem)
            status = "expired" if is_job_expired(text) else "alive"
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                status = "expired"
                item = {**item, "reason": "404"}
            else:
                status = "error"
                item = {**item, "error": str(e)}
        except Exception as e:
            try:
                from job_fetch.jobleads import JobLeadsCloudflareError
                if isinstance(e, JobLeadsCloudflareError):
                    status = "alive"
                    logger.debug("[expired_marker] jobleads skip: %s", item["url"])
                else:
                    raise
            except ImportError:
                status = "error"
                item = {**item, "error": str(e)}

        done += 1
        if status == "expired":
            expired_count += 1
        if progress_cb and done % PROGRESS_EVERY == 0:
            await progress_cb(f"⏳ {done}/{total} проверено — ⏭ истекло: {expired_count}")
        return {**item, "status": status}

    results = await asyncio.gather(*[_check_one(item) for item in to_check])

    expired: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []
    alive = 0
    updates: dict[str, str] = {}

    for res in results:
        if res["status"] == "expired":
            expired.append(res)
            logger.info("[expired_marker] EXPIRED: %s — %s", res["company"], res["title"])
            if res.get("id"):
                updates[res["id"]] = "EXPIRED"
        elif res["status"] == "skipped":
            skipped.append(res)
        elif res["status"] == "alive":
            alive += 1
        else:
            errors.append(res)
            logger.warning("[expired_marker] error %s: %s", res["url"], res.get("error", ""))

    # Write EXPIRED to tracker.xlsx for all expired rows at once
    if updates:
        written = await asyncio.to_thread(apply_sent_updates, updates)
        logger.info("[expired_marker] Wrote EXPIRED to %d tracker rows", written)

        # Update in-memory cache too (best-effort)
        try:
            from hunter.tracker_cache import cache
            for row_id in updates:
                await cache.update_sent(row_id, "EXPIRED")
        except Exception as e:
            logger.warning("[expired_marker] cache update failed: %s", e)

        # Mirror EXPIRED stamps to Google Sheets (best-effort)
        try:
            from hunter import gsheets_sync
            await gsheets_sync.mirror_expired_batch(set(updates.keys()))
        except Exception as e:
            logger.warning("[expired_marker] gsheets mirror failed: %s", e)

    return {
        "total": total,
        "alive": alive,
        "expired": expired,
        "errors": errors,
        "skipped": skipped,
        "marked": len(expired),
    }
