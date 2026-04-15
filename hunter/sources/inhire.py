"""
inhire.io source — Polish IT job board.

Strategy:
  app.inhire.io is a Vue.js SPA with no public JSON API. Job data is loaded
  from an internal backend (port 9000, not accessible from the public internet).
  We use Playwright to render the page in a headless browser and read the Vuex
  store directly after the listing has loaded.

  Requires:
    pip install playwright
    python -m playwright install chromium

  If playwright is not installed, search() logs a warning and returns [].

Listing URLs:
  https://app.inhire.io/oferty-pracy/frontend_developer
  https://app.inhire.io/oferty-pracy/javascript
  https://app.inhire.io/oferty-pracy/full_stack_developer
"""

import asyncio
import logging
import re
from typing import Optional

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://app.inhire.io"

# Category pages — Inhire uses category slugs as URL segments
LISTING_URLS = [
    f"{BASE}/oferty-pracy/frontend_developer",
    f"{BASE}/oferty-pracy/javascript",
    f"{BASE}/oferty-pracy/full_stack_developer",
]

# How long to wait for job cards to appear (ms)
PAGE_LOAD_TIMEOUT = 30_000
# Selector that indicates the listing has rendered
CARD_SELECTOR = "a[href*='oferty-pracy'][href*=',oferta,'], a[href*='job-offers'][href*=',oferta,']"


class InhireSource(BaseSource):
    name = "inhire"

    def search(self) -> list[Job]:
        try:
            import playwright  # noqa: F401 — presence check
        except ImportError:
            logger.warning(
                "[inhire] Playwright is not installed. "
                "Run: pip install playwright && python -m playwright install chromium"
            )
            return []

        try:
            return asyncio.run(self._async_search())
        except Exception as e:
            logger.warning(f"[inhire] Playwright search failed: {e}")
            return []

    # -- Async entry point -----------------------------------------------------

    async def _async_search(self) -> list[Job]:
        from playwright.async_api import async_playwright

        seen_urls: set[str] = set()
        jobs: list[Job] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="pl-PL",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            for listing_url in LISTING_URLS:
                try:
                    raw_jobs = await self._fetch_page(context, listing_url)
                    logger.info(f"[inhire] {listing_url} -> {len(raw_jobs)} raw")
                    for raw in raw_jobs:
                        job = self._parse(raw)
                        if not job or job.url in seen_urls:
                            continue
                        if not self._is_relevant(raw, job):
                            continue
                        seen_urls.add(job.url)
                        jobs.append(job)
                except Exception as e:
                    logger.warning(f"[inhire] page failed, skipping {listing_url}: {e}")

            await browser.close()

        logger.info(f"[inhire] {len(jobs)} jobs after pre-filter")
        return jobs

    async def _fetch_page(self, context, url: str) -> list[dict]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

            # Strategy 1: wait for Vue Vuex store to populate offer list
            raw = await self._extract_vuex(page)
            if raw:
                logger.debug(f"[inhire] Vuex gave {len(raw)} items from {url}")
                return raw

            # Strategy 2: wait for job card links to appear in DOM
            try:
                await page.wait_for_selector(CARD_SELECTOR, timeout=10_000)
            except Exception:
                pass  # cards may not appear (0 results or different selector)

            raw = await self._extract_dom(page)
            if raw:
                logger.debug(f"[inhire] DOM gave {len(raw)} items from {url}")
                return raw

            logger.warning(f"[inhire] 0 items extracted from {url}")
            return []
        finally:
            await page.close()

    @staticmethod
    async def _extract_vuex(page) -> list[dict]:
        """Try to read allOffersList directly from the Vue 3 / Vuex store."""
        return await page.evaluate("""() => {
            try {
                const appEl = document.getElementById('app');
                if (!appEl) return [];

                // Vue 3: __vue_app__
                const vueApp = appEl.__vue_app__;
                if (vueApp) {
                    const store = vueApp.config.globalProperties.$store;
                    if (store && store.state && store.state.offers) {
                        return store.state.offers.allOffersList || [];
                    }
                }

                // Vue 2: __vue__
                const vue2 = appEl.__vue__;
                if (vue2) {
                    const store = vue2.$store;
                    if (store && store.state && store.state.offers) {
                        return store.state.offers.allOffersList || [];
                    }
                }
            } catch(e) {}
            return [];
        }""")

    @staticmethod
    async def _extract_dom(page) -> list[dict]:
        """Fallback: extract job card data from rendered DOM links."""
        return await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            const offerLinks = links.filter(a =>
                a.href.includes(',oferta,') &&
                (a.href.includes('oferty-pracy') || a.href.includes('job-offers'))
            );
            const seen = new Set();
            const results = [];
            for (const a of offerLinks) {
                const href = a.href.split('?')[0];
                if (seen.has(href)) continue;
                seen.add(href);
                const text = a.innerText.trim();
                if (!text || text.length < 5) continue;
                results.push({ url: href, _text: text, title: '', company: '' });
            }
            return results;
        }""")

    # -- Pre-filter ------------------------------------------------------------

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()
        for pat in FILTER.get("exclude_patterns", []):
            if re.search(pat, title, re.I):
                return False
        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        text = (raw.get("_text") or "").lower()
        return any(kw in title + " " + text for kw in keywords)

    # -- Parser ----------------------------------------------------------------

    def _parse(self, raw: dict) -> Optional[Job]:
        # Vuex store format (Vue app internal state)
        title = (
            raw.get("name") or raw.get("title") or raw.get("jobTitle") or ""
        ).strip()

        # DOM fallback: title might be empty; try to extract from _text
        if not title and raw.get("_text"):
            lines = [ln.strip() for ln in raw["_text"].split("\n") if ln.strip()]
            title = lines[0] if lines else ""

        if not title:
            return None

        url = self._build_url(raw)
        if not url:
            return None

        company_raw = raw.get("company") or raw.get("companyName") or raw.get("employer") or "Unknown"
        company = (
            company_raw.get("name", "Unknown")
            if isinstance(company_raw, dict)
            else str(company_raw)
        ).strip()

        location = self._parse_location(raw)
        salary = self._parse_salary(raw)

        return Job(
            title=title,
            company=company or "Unknown",
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )

    @staticmethod
    def _build_url(raw: dict) -> str:
        for key in ("offerUrl", "url", "offerAbsoluteUri", "href"):
            val = raw.get(key, "")
            if val:
                if val.startswith("http"):
                    return val.split("?")[0]
                if val.startswith("/"):
                    return BASE + val.split("?")[0]
        return ""

    @staticmethod
    def _parse_location(raw: dict) -> str:
        loc = raw.get("location") or raw.get("city") or ""
        if isinstance(loc, dict):
            loc = loc.get("name") or loc.get("city") or ""
        loc = str(loc).strip()

        remote = raw.get("remote") or raw.get("fullyRemote") or raw.get("isRemote") or False
        hybrid = raw.get("hybrid") or raw.get("isHybrid") or False

        if loc and remote:
            return f"{loc} (Remote)"
        if loc and hybrid:
            return f"{loc} (Hybrid)"
        if loc:
            return loc
        if remote:
            return "Remote"
        return "Unknown"

    @staticmethod
    def _parse_salary(raw: dict) -> Optional[str]:
        sal = raw.get("salary") or raw.get("salaryText") or raw.get("salaryDisplayText") or ""
        if isinstance(sal, str) and sal.strip():
            return sal.strip()
        if isinstance(sal, dict):
            lo = sal.get("from") or sal.get("min")
            hi = sal.get("to") or sal.get("max")
            currency = sal.get("currency", "PLN")
            if lo or hi:
                return f"{lo or '?'}-{hi or '?'} {currency}"
        return None
