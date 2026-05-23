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
    EXPIRED_CHECK_FETCH_TIMEOUT,
)
from hunter.expired_check import is_job_expired, is_expired_by_html
from hunter.tracker import apply_sent_updates, iter_unsent_rows as _iter_unsent

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 5

# Domains where a lightweight raw-HTML check is more reliable than the full
# fetch_job_text pipeline (Playwright/cloudscraper failures → false "alive").
_QUICK_CHECK_DOMAINS = ("pracuj.pl", "linkedin.com")

_QUICK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}
_QUICK_TIMEOUT = 20


def _quick_html_expired(url: str, domain: str) -> bool | None:
    """Fetch raw HTML for known domains and scan for expiry markers.

    Returns True (expired), False (clearly alive) or None (can't determine).
    Only called for domains in _QUICK_CHECK_DOMAINS.
    """
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, timeout=_QUICK_TIMEOUT)
        html = resp.text if resp.status_code == 200 else ""
    except Exception:
        try:
            resp = requests.get(url, headers=_QUICK_HEADERS, timeout=_QUICK_TIMEOUT)
            html = resp.text if resp.status_code == 200 else ""
        except Exception:
            return None

    if not html:
        return None

    # If Cloudflare served a challenge page (200 but not real content),
    # return None so we fall through to the full pipeline or mark as skipped.
    if _is_cloudflare_challenge(html):
        logger.debug("[expired_marker] Cloudflare challenge detected for %s", url)
        return None

    if is_expired_by_html(html, domain):
        return True

    # LinkedIn: if we got a login/auth wall instead of the real page,
    # we can't determine expiry — return None so the caller skips gracefully.
    if "linkedin.com" in domain:
        html_lower = html.lower()
        login_wall = (
            "authwall" in html_lower
            or "join linkedin" in html_lower
            or ("sign in" in html_lower and "job description" not in html_lower)
        )
        if login_wall:
            return None  # can't check — treat as skipped

    return None  # no expiry signal found, but not conclusively alive either


def _is_cloudflare_challenge(html: str) -> bool:
    """Return True if the HTML looks like a Cloudflare challenge/bot-check page."""
    if len(html) < 1500:  # real job pages are much larger
        lower = html.lower()
        return (
            "just a moment" in lower
            or "enable javascript and cookies" in lower
            or "_cf_chl" in lower
            or "cf-browser-verification" in lower
        )
    lower = html.lower()
    return (
        "just a moment" in lower
        and ("cloudflare" in lower or "_cf_chl" in lower)
    )


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
                result = await asyncio.wait_for(
                    asyncio.to_thread(fetch_job_text, url),
                    timeout=EXPIRED_CHECK_FETCH_TIMEOUT,
                )
        # Rate-limit delay runs outside semaphores so slots are freed immediately.
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

        url = item["url"]
        dom = _domain(url)

        # Fast path: lightweight raw-HTML check for Pracuj / LinkedIn.
        # Avoids full cloudscraper/Playwright fetch when the archived marker
        # is visible directly in the HTML (or when a login wall blocks detection).
        if any(d in dom for d in _QUICK_CHECK_DOMAINS):
            quick = await asyncio.to_thread(_quick_html_expired, url, dom)
            if quick is True:
                done += 1
                expired_count += 1
                if progress_cb and done % PROGRESS_EVERY == 0:
                    await progress_cb(f"⏳ {done}/{total} checked — expired: {expired_count}")
                return {**item, "status": "expired", "reason": "html-marker"}
            if quick is None and "linkedin.com" in dom:
                # Login wall — can't determine expiry without a session.
                done += 1
                if progress_cb and done % PROGRESS_EVERY == 0:
                    await progress_cb(f"⏳ {done}/{total} checked — expired: {expired_count}")
                return {**item, "status": "skipped", "reason": "linkedin-login-wall"}

        try:
            text = await limiter.fetch(url, global_sem)
            status = "expired" if is_job_expired(text) else "alive"
        except asyncio.TimeoutError:
            status = "error"
            item = {**item, "error": f"timeout after {EXPIRED_CHECK_FETCH_TIMEOUT}s"}
            logger.warning("[expired_marker] timeout: %s", item["url"])
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (404, 410):
                status = "expired"
                item = {**item, "reason": str(code)}
            else:
                status = "error"
                item = {**item, "error": str(e)}
        except Exception as e:
            is_jobleads_cf = False
            try:
                from job_fetch.jobleads import JobLeadsCloudflareError
                is_jobleads_cf = isinstance(e, JobLeadsCloudflareError)
            except ImportError:
                pass
            if is_jobleads_cf:
                status = "alive"
                logger.debug("[expired_marker] jobleads skip: %s", item["url"])
            else:
                status = "error"
                item = {**item, "error": str(e)}

        done += 1
        if status == "expired":
            expired_count += 1
        if progress_cb and done % PROGRESS_EVERY == 0:
            await progress_cb(f"⏳ {done}/{total} checked — expired: {expired_count}")
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
