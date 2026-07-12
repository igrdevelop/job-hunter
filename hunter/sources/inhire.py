"""
inhire.io source — Polish IT job board.

Strategy:
  app.inhire.io is a SPA (currently React-based, no public API).
  We use Playwright to render the page headlessly and scrape job cards
  directly from the DOM using stable CSS class selectors.

  Job card DOM structure (as of 2025-04):
    div.inh-offer
      a.inh-offer__bottom[href="/praca/{slug}-job-arbeit-{id}"]
        h3.inh-offer-data__name          → title
        h4.inh-offer-data__company-name  → company
        .inh-offer-data__salary .inh-text--bold  → salary
        .inh-offer-data__location .inh-text      → location

  Vuex extraction is attempted first for forward-compatibility, but falls back
  to DOM scraping (primary strategy since the Vue instance was removed).

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
import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

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
# networkidle takes longer than domcontentloaded — use a generous timeout
PAGE_LOAD_TIMEOUT = 60_000
# Selector that indicates the listing has rendered (updated 2025-04: new DOM structure)
CARD_SELECTOR = "a.inh-offer__bottom"

# ── Detail-page fetching helpers (ported from job_fetch/inhire.py) ──────────
DETAIL_TIMEOUT = 25


def _strip_html_detail(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section|strong|em|span)[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_jp_ld(jp: dict) -> str:
    parts: list[str] = []
    parts.append(f"Job Title: {jp.get('title', 'N/A')}")
    org = jp.get("hiringOrganization") or {}
    if isinstance(org, dict):
        parts.append(f"Company: {org.get('name', 'N/A')}")
    loc = jp.get("jobLocation")
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        city = addr.get("addressLocality", "")
        country = addr.get("addressCountry", "")
        loc_str = ", ".join(filter(None, [city, country]))
        if loc_str:
            parts.append(f"Location: {loc_str}")
    elif isinstance(loc, list):
        cities = [
            (lc.get("address") or {}).get("addressLocality", "")
            for lc in loc
            if isinstance(lc, dict)
        ]
        if any(cities):
            parts.append(f"Location: {', '.join(c for c in cities if c)}")
    salary = jp.get("baseSalary") or {}
    if isinstance(salary, dict):
        value = salary.get("value") or {}
        if isinstance(value, dict):
            lo = value.get("minValue")
            hi = value.get("maxValue")
            currency = salary.get("currency", "PLN")
            if lo or hi:
                parts.append(f"Salary: {lo or '?'}-{hi or '?'} {currency}")
    desc = jp.get("description", "")
    if desc:
        parts.append(f"\n--- Job Description ---\n{_strip_html_detail(desc)}")
    result = "\n".join(parts)
    return result if len(result) > 50 else ""


def _try_json_ld(html: str) -> str:
    matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S,
    )
    for raw in matches:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") == "JobPosting":
                return _format_jp_ld(item)
    return ""


def _try_bs4_detail(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    h1 = soup.find("h1")
    if h1:
        parts.append(f"Job Title: {h1.get_text(strip=True)}")
    else:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            parts.append(f"Job Title: {og_title['content']}")
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        parts.append(f"Summary: {og_desc['content']}")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.find("div", {"role": "main"})
    content = main or soup.body
    if content:
        text = content.get_text(separator="\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            parts.append("\n--- Page Content ---\n" + "\n".join(lines[:200]))
    result = "\n".join(parts)
    return result if len(result) > 100 else ""


def _format_vuex_offer(offer: dict) -> str:
    parts: list[str] = []
    title = offer.get("name") or offer.get("title") or offer.get("jobTitle") or ""
    if title:
        parts.append(f"Job Title: {title}")
    company = offer.get("company") or offer.get("companyName") or offer.get("employer") or ""
    if isinstance(company, dict):
        company = company.get("name", "")
    if company:
        parts.append(f"Company: {company}")
    location = offer.get("location") or offer.get("city") or ""
    if isinstance(location, dict):
        location = location.get("name") or location.get("city") or ""
    if location:
        parts.append(f"Location: {location}")
    salary = offer.get("salary") or ""
    if isinstance(salary, dict):
        lo = salary.get("from") or salary.get("min") or ""
        hi = salary.get("to") or salary.get("max") or ""
        currency = salary.get("currency", "PLN")
        salary = f"{lo}-{hi} {currency}" if (lo or hi) else ""
    if salary:
        parts.append(f"Salary: {salary}")
    desc = offer.get("description") or offer.get("requirements") or ""
    if desc:
        parts.append(f"\n--- Job Description ---\n{_strip_html_detail(str(desc))}")
    return "\n".join(parts)


def _try_playwright_detail(url: str) -> str:
    """Use headless Chromium to render the SPA and extract job text."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("[inhire] playwright not installed, skipping headless fetch")
        return ""

    async def _run() -> str:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                offer = await page.evaluate(
                    """() => {
                        try {
                            const appEl = document.getElementById('app');
                            const va = appEl.__vue_app__;
                            if (va) {
                                const store = va.config.globalProperties.$store;
                                if (store && store.state && store.state.offers) {
                                    return store.state.offers.offer || store.state.offers.currentOffer || null;
                                }
                            }
                            const v2 = appEl.__vue__;
                            if (v2 && v2.$store && v2.$store.state && v2.$store.state.offers) {
                                return v2.$store.state.offers.offer || v2.$store.state.offers.currentOffer || null;
                            }
                        } catch(e) {}
                        return null;
                    }"""
                )
                if offer and isinstance(offer, dict):
                    return _format_vuex_offer(offer)
                text = await page.inner_text("body")
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                return "\n".join(lines[:300]) if lines else ""
            finally:
                await browser.close()

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.warning(f"[inhire] Playwright job fetch failed: {e}")
        return ""


class InhireSource(BaseSource):
    name = "inhire"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "inhire.io" in host

    def fetch_text(self, url: str) -> str:
        """cloudscraper → JSON-LD → BS4 → Playwright → html_fallback cascade."""
        from hunter.sources.html_fallback import fetch_html

        try:
            import cloudscraper
        except ImportError:
            logger.warning("[inhire] cloudscraper not installed, using html_fallback")
            return fetch_html(url)

        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=DETAIL_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.warning(f"[inhire] HTTP fetch failed ({e}), trying html_fallback")
            return fetch_html(url)

        text = _try_json_ld(html)
        if text and len(text) > 150:
            return text

        text = _try_bs4_detail(html)
        if text and len(text) > 150:
            return text

        text = _try_playwright_detail(url)
        if text and len(text) > 150:
            return text

        logger.warning("[inhire] All strategies returned too little text, using html_fallback")
        return fetch_html(url)

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
            await page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)

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
        """Try to read the offers list directly from the Vue 3 / Vuex store.

        Tries several known state/getter paths in priority order so the
        scraper survives minor Vuex refactors on the inhire.io side.
        """
        return await page.evaluate("""() => {
            function tryPaths(store) {
                // state-based paths (most common)
                const statePaths = [
                    () => store.state.offers.allOffersList,
                    () => store.state.offers.list,
                    () => store.state.offers.offers,
                    () => store.state.offersList,
                    () => store.state.offers.items,
                ];
                for (const fn of statePaths) {
                    try {
                        const val = fn();
                        if (Array.isArray(val) && val.length > 0) return val;
                    } catch(e) {}
                }
                // getter-based paths (Vuex modules with getters)
                const getterPaths = [
                    'offers/allOffersList',
                    'offers/list',
                    'offers/offers',
                    'allOffersList',
                ];
                if (store.getters) {
                    for (const key of getterPaths) {
                        try {
                            const val = store.getters[key];
                            if (Array.isArray(val) && val.length > 0) return val;
                        } catch(e) {}
                    }
                }
                return [];
            }

            try {
                const appEl = document.getElementById('app');
                if (!appEl) return [];

                // Vue 3: __vue_app__
                const vueApp = appEl.__vue_app__;
                if (vueApp) {
                    const store = vueApp.config.globalProperties.$store;
                    if (store) {
                        const result = tryPaths(store);
                        if (result.length > 0) return result;
                    }
                }

                // Vue 2: __vue__
                const vue2 = appEl.__vue__;
                if (vue2 && vue2.$store) {
                    const result = tryPaths(vue2.$store);
                    if (result.length > 0) return result;
                }
            } catch(e) {}
            return [];
        }""")

    @staticmethod
    async def _extract_dom(page) -> list[dict]:
        """Extract job card data from rendered DOM using stable CSS selectors."""
        return await page.evaluate("""() => {
            const cards = Array.from(document.querySelectorAll('a.inh-offer__bottom'));
            const seen = new Set();
            const results = [];
            for (const a of cards) {
                const href = (a.getAttribute('href') || '').split('?')[0];
                if (!href || !href.includes('/praca/') || seen.has(href)) continue;
                seen.add(href);

                const titleEl   = a.querySelector('h3.inh-offer-data__name');
                const companyEl = a.querySelector('h4.inh-offer-data__company-name');
                const salaryEl  = a.querySelector('.inh-offer-data__salary .inh-text--bold');
                const locationEl = a.querySelector('.inh-offer-data__location .inh-text');

                const title   = titleEl   ? titleEl.innerText.trim()   : '';
                const company = companyEl ? companyEl.innerText.trim() : '';
                const salary  = salaryEl  ? salaryEl.innerText.trim()  : '';
                const location = locationEl ? locationEl.innerText.trim() : '';

                if (!title) continue;
                results.push({
                    url: href,
                    title: title,
                    company: company,
                    salary: salary,
                    location: location,
                    _text: a.innerText.trim(),
                });
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
        title = (raw.get("name") or raw.get("title") or raw.get("jobTitle") or "").strip()

        # DOM fallback: title might be empty; try to extract from _text
        if not title and raw.get("_text"):
            lines = [ln.strip() for ln in raw["_text"].split("\n") if ln.strip()]
            title = lines[0] if lines else ""

        if not title:
            return None

        url = self._build_url(raw)
        if not url:
            return None

        company_raw = (
            raw.get("company") or raw.get("companyName") or raw.get("employer") or "Unknown"
        )
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
