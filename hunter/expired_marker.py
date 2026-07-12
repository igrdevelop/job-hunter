"""
hunter/expired_marker.py — Check unsent tracker rows for expired job offers.

Reads unsent rows directly from tracker.xlsx.
Marks expired rows in both tracker.xlsx and the in-memory cache.

Used by /check_expired (Telegram) and the daily scheduled check.
"""

import asyncio
import logging
from typing import Callable, Awaitable

import requests

from hunter.config import (
    EXPIRED_CHECK_CONCURRENCY,
    EXPIRED_CHECK_DOMAIN_LIMIT,
    EXPIRED_CHECK_DOMAIN_DELAY,
    EXPIRED_CHECK_FETCH_TIMEOUT,
    PRACUJ_HOST_CONCURRENCY,
    PRACUJ_HOST_DELAY_SEC,
)
from hunter.expired_check import is_job_expired, is_expired_by_html
from hunter.rate_limiter import DomainLimiter, domain_of
from hunter.tracker import apply_sent_updates, iter_unsent_rows as _iter_unsent

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 5

# Domains where a lightweight raw-HTML check is attempted before the full
# fetch_job_text pipeline.
# NOTE: pracuj.pl is intentionally excluded — its Cloudflare protection blocks
# fresh cloudscraper sessions (HTTP 403), while fetch_pracuj reuses a persistent
# module-level scraper that has accumulated session cookies. The full pipeline
# already handles pracuj.pl archived detection reliably via _extract_archived_notice
# and the isActive:false __NEXT_DATA__ check, so a quick pre-check adds nothing.
_QUICK_CHECK_DOMAINS = ("linkedin.com",)

_QUICK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}
_QUICK_TIMEOUT = 20


def _fetch_quick_html(url: str) -> tuple[str, int]:
    """Fetch raw HTML for a URL, stripping tracking params first.

    Returns (html, status_code). html is "" on non-200 or error.
    Tries cloudscraper first, falls back to plain requests.
    """
    from hunter.sources.html_fallback import clean_url

    fetch_url = clean_url(url)

    try:
        import cloudscraper

        scraper = cloudscraper.create_scraper()
        resp = scraper.get(fetch_url, timeout=_QUICK_TIMEOUT)
        logger.debug(
            "[expired_marker] quick fetch %s → HTTP %s, %d bytes",
            fetch_url,
            resp.status_code,
            len(resp.text),
        )
        return (resp.text if resp.status_code == 200 else ""), resp.status_code
    except Exception as cs_err:
        logger.debug("[expired_marker] cloudscraper failed (%s), trying plain requests", cs_err)

    try:
        resp = requests.get(fetch_url, headers=_QUICK_HEADERS, timeout=_QUICK_TIMEOUT)
        logger.debug(
            "[expired_marker] plain-requests fetch %s → HTTP %s, %d bytes",
            fetch_url,
            resp.status_code,
            len(resp.text),
        )
        return (resp.text if resp.status_code == 200 else ""), resp.status_code
    except Exception as req_err:
        logger.debug("[expired_marker] plain-requests also failed: %s", req_err)
        return "", 0


def _check_html_expired(html: str, domain: str, url: str = "") -> bool | None:
    """Check already-fetched HTML for expiry signals.

    Returns True (expired), None (can't determine / login wall / CF challenge).
    Separated from fetching so callers can reuse HTML without a second request.
    """
    if not html:
        return None

    if _is_cloudflare_challenge(html):
        logger.debug("[expired_marker] Cloudflare challenge detected for %s", url or domain)
        return None

    if is_expired_by_html(html, domain):
        return True

    # LinkedIn: login wall → can't determine expiry without a session.
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


def _quick_html_expired(url: str, domain: str) -> bool | None:
    """Fetch raw HTML for known domains and scan for expiry markers.

    Returns True (expired) or None (can't determine).
    Only called for domains in _QUICK_CHECK_DOMAINS.
    """
    html, _status = _fetch_quick_html(url)
    return _check_html_expired(html, domain, url=url)


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
    return "just a moment" in lower and ("cloudflare" in lower or "_cf_chl" in lower)


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

    from hunter.sources import fetch_job_text

    global_sem = asyncio.Semaphore(EXPIRED_CHECK_CONCURRENCY)
    limiter = DomainLimiter(
        EXPIRED_CHECK_DOMAIN_LIMIT,
        EXPIRED_CHECK_DOMAIN_DELAY,
        overrides={"pracuj.pl": (PRACUJ_HOST_CONCURRENCY, PRACUJ_HOST_DELAY_SEC)},
    )

    async def _check_one(item: dict) -> dict:
        nonlocal done, expired_count

        url = item["url"]

        # LinkedIn Scout relay rows carry a synthetic dedup-key URL with no
        # fetchable posting behind it (hunter.sources.linkedin_scout_relay's
        # fetch_text always raises by design) — skip outright instead of
        # burning a request that's guaranteed to error.
        from hunter.validation import SCOUT_POSTS_URL_MARKER, _LEGACY_SCOUT_POSTS_URL_MARKER

        if SCOUT_POSTS_URL_MARKER in url or _LEGACY_SCOUT_POSTS_URL_MARKER in url:
            done += 1
            if progress_cb and done % PROGRESS_EVERY == 0:
                await progress_cb(f"⏳ {done}/{total} checked — expired: {expired_count}")
            return {**item, "status": "skipped", "reason": "scout-no-fetchable-url"}

        dom = domain_of(url)

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
            text = await limiter.fetch(
                url, global_sem, fetch_job_text, timeout=EXPIRED_CHECK_FETCH_TIMEOUT
            )
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
                from hunter.sources.jobleads import JobLeadsCloudflareError

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
