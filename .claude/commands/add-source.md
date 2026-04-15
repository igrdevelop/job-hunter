# Add a new job source

This command documents the full recipe for integrating a new job board into the hunter system.

## Overview

Each job source consists of two parts:
1. **Listing scraper** (`hunter/sources/{site}.py`) — discovers job URLs during scheduled hunts
2. **Detail fetcher** (`job_fetch/{site}.py`) — downloads full job text when an application is processed

## Step 1 — Investigate the target site

Before writing code, determine the data extraction strategy:

| Strategy | When to use | Example |
|---|---|---|
| Public JSON API | Site exposes `/api/...` endpoints | JustJoin, NoFluffJobs |
| `__NEXT_DATA__` (Next.js SSR) | `<script id="__NEXT_DATA__">` in page HTML | Pracuj.pl, theprotocol.it |
| RSS feed | Site publishes a public RSS/Atom feed | Solid.Jobs |
| BeautifulSoup DOM | Static HTML, no JS rendering needed | Bulldogjob |
| Playwright headless browser | Vue/React SPA with no public API or SSR | Inhire.io |

Check for:
- `__NEXT_DATA__` script tag in HTML source
- `/rss`, `/feed`, `/sitemap.xml`
- Network tab in DevTools → Fetch/XHR → look for JSON responses with job arrays
- Cloudflare protection → use `cloudscraper` instead of `requests`

## Step 2 — Create `hunter/sources/{site}.py`

```python
"""
{site} source — one-line description.

Strategy: [how data is fetched]
Listing URLs: [list of URLs searched]
"""

import logging
import re
from typing import Optional

# Use cloudscraper if the site has Cloudflare, otherwise use requests
import cloudscraper  # or: import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://{site}"
LISTING_URLS = [
    f"{BASE}/jobs?technology=frontend&location=wroclaw",
    f"{BASE}/jobs?technology=angular&location=wroclaw",
    f"{BASE}/jobs?technology=frontend&remote=true",
]
TIMEOUT = 25

_scraper = cloudscraper.create_scraper()  # or: (omit for requests)


class {Site}Source(BaseSource):
    name = "{site}"   # must match domain fragment used in job_fetch/__init__.py routing

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for listing_url in LISTING_URLS:
            try:
                raw_jobs = self._fetch_listing(listing_url)
                logger.info(f"[{site}] {listing_url} -> {len(raw_jobs)} raw")
                for raw in raw_jobs:
                    job = self._parse(raw)
                    if not job or job.url in seen_urls:
                        continue
                    if not self._is_relevant(raw, job):
                        continue
                    seen_urls.add(job.url)
                    jobs.append(job)
            except Exception as e:
                logger.warning(f"[{site}] listing failed, skipping {listing_url}: {e}")

        logger.info(f"[{site}] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = _scraper.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[{site}] HTTP failed for {url}: {e}")
            return []

        # Strategy A — __NEXT_DATA__
        # return self._extract_next_data(resp.text)

        # Strategy B — JSON API response
        # return resp.json().get("offers", [])

        # Strategy C — RSS feed
        # return self._parse_rss(resp.text)

        # Strategy D — BeautifulSoup DOM
        # return self._extract_bs4(resp.text)

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()
        for pat in FILTER.get("exclude_patterns", []):
            if re.search(pat, title, re.I):
                return False
        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        text = (raw.get("_text") or "").lower()
        return any(kw in title + " " + text for kw in keywords)

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or raw.get("jobTitle") or "").strip()
        if not title:
            return None
        url = raw.get("url") or raw.get("offerUrl") or ""
        if not url:
            return None
        return Job(
            title=title,
            company=(raw.get("company") or raw.get("companyName") or "Unknown").strip(),
            location=(raw.get("location") or "Unknown").strip(),
            salary=raw.get("salary") or None,
            url=url,
            source=self.name,
            raw=raw,
        )
```

Key rules:
- `search()` must return `list[Job]` — no filtering or dedup (done centrally in `hunter/main.py`)
- Every listing URL fetch must be wrapped in `try/except` so one failure doesn't block others
- Set `source=self.name` on every `Job` object
- Use `FILTER` from `hunter/config.py` for `_is_relevant()` checks

See existing examples:
- `hunter/sources/pracuj.py` — `__NEXT_DATA__` + React Query dehydratedState + cloudscraper
- `hunter/sources/solidjobs.py` — RSS feed parsing
- `hunter/sources/theprotocol.py` — `__NEXT_DATA__` with BeautifulSoup DOM fallback
- `hunter/sources/nofluffjobs.py` — JSON API with pagination
- `hunter/sources/inhire.py` — Playwright headless browser (when no public API exists)

## Step 3 — Create `job_fetch/{site}.py`

This fetcher is called lazily when `apply_agent.py` processes a job URL. Return the full job description as plain text for the LLM.

```python
"""Fetch a single {site} job offer by URL -> plain text."""
import logging
import re
import cloudscraper  # or requests

logger = logging.getLogger(__name__)
TIMEOUT = 25
_scraper = cloudscraper.create_scraper()


def fetch_{site}(url: str) -> str:
    try:
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"[{site}] HTTP failed ({e}), using html_fallback")
        from job_fetch.html_fallback import fetch_html
        return fetch_html(url)

    # Try JSON-LD first, then BeautifulSoup, then html_fallback
    from job_fetch.html_fallback import fetch_html
    return fetch_html(url)
```

See `job_fetch/theprotocol.py` for a complete example with JSON-LD + BeautifulSoup fallback.

## Step 4 — Add enable flag to `hunter/config.py`

```python
# ── {Site} source config ──────────────────────────────────────────────────────
# Set to "false" if the site is unreliable or requires special setup (e.g. Playwright)
{SITE}_ENABLED: bool = os.getenv("{SITE}_ENABLED", "true").lower() in ("true", "1", "yes")
```

## Step 5 — Register in `hunter/sources/__init__.py`

```python
if {SITE}_ENABLED:
    from hunter.sources.{site} import {Site}Source
    ALL_SOURCES.append({Site}Source())
```

The source's position in `ALL_SOURCES` determines its schedule slot (offset by `SCHEDULE_SOURCE_OFFSET_MIN` per source).

## Step 6 — Add routing to `job_fetch/__init__.py`

```python
if "{site}.com" in domain:
    logger.info(f"[job_fetch] {Site} detected: {url}")
    from job_fetch.{site} import fetch_{site}
    return fetch_{site}(url)
```

Add before the final `fetch_html` fallback line.

## Step 7 — Test

```bash
# Test listing scraper standalone
python -c "
from hunter.sources.{site} import {Site}Source
jobs = {Site}Source().search()
for j in jobs[:5]:
    print(j.title, '|', j.company, '|', j.url)
print(f'Total: {len(jobs)}')
"

# Test detail fetcher
python -c "
from job_fetch import fetch_job_text
text = fetch_job_text('https://{site}.com/some-job-url')
print(text[:500])
"
```

## Notes on Playwright (headless browser)

Use Playwright when the site is a client-side SPA with no public API or RSS feed.

Install once:
```bash
pip install playwright
python -m playwright install chromium
```

In the source, access the Vuex/Redux store after page load:
```python
import asyncio
from playwright.async_api import async_playwright

async def _fetch_with_playwright(url: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        offers = await page.evaluate("""() => {
            try {
                const app = document.getElementById('app').__vue_app__;
                const store = app.config.globalProperties.$store;
                return store.state.offers.allOffersList || [];
            } catch(e) { return []; }
        }""")
        await browser.close()
        return offers

# Call from sync search():
def search(self) -> list[Job]:
    return asyncio.run(self._async_search())
```

See `hunter/sources/inhire.py` for a full working example.
