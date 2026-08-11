---
description: Integrate a new job board into the hunter — listing scraper, detail fetcher, config toggle, every registration point, tests and CLAUDE.md rows.
argument-hint: <site name or listing URL>
---

# Add a new job source

This command documents the full recipe for integrating a new job board into the hunter system.

## Overview

One class in `hunter/sources/{site}.py` owns **both** halves of the job:

1. `search()` — discovers job URLs during scheduled hunts
2. `matches_url()` + `fetch_text()` — claims a URL and downloads the full job text
   when an application is processed

There is no separate `job_fetch/` package — it was merged into the sources in the
Phase 3 refactor (2026-05-26). `hunter.sources.fetch_job_text(url)` is the single
dispatcher: it walks `_fetch_roster()` and hands the URL to the first source whose
`matches_url` claims it, falling back to the generic HTML extractor.

**The five registration points** (a source missing any of them is half-wired —
this is exactly what the `project-invariants-review` agent checks):

| # | Where | What |
|---|---|---|
| 1 | `hunter/sources/{site}.py` | the source class itself |
| 2 | `hunter/config.py` | `{SITE}_ENABLED` toggle |
| 3 | `hunter/sources/__init__.py` → import block + `ALL_SOURCES` | hunt-cycle registration (gated by the toggle) |
| 4 | `hunter/sources/__init__.py` → `_fetch_roster()` | detail-fetch dispatch (NOT gated by the toggle — a disabled source still owns its domain's URLs) |
| 5 | `CLAUDE.md` | a row in **both** the "Job Sources" and "Scraper Health Notes" tables |

Plus, not strictly required but expected: a commented toggle line in `.env.example`
and a case in `tests/test_sources_dispatcher.py`.

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
    name = "{site}"   # used in logs, source_health rows and Job.source

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

## Step 3 — Add detail-page extraction to the SAME class

`BaseSource` already provides both methods: `matches_url()` returns `False` by
default (the source claims nothing) and `fetch_text()` falls back to the generic
HTML extractor. Override them on the class you wrote in Step 2 — there is no
second file.

```python
    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "{site}.com" in host

    def fetch_text(self, url: str) -> str:
        """Return the full posting as plain text. Raise on failure — the caller
        decides recovery. If the site serves a synthetic marker for a deleted
        posting, return it verbatim so expired_check can turn it into a clean
        $0 EXPIRED skip instead of a FAIL row."""
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        if resp.status_code == 404:
            return "This job posting has expired."
        # Structured data first (JSON API / JSON-LD / __NEXT_DATA__), then:
        from hunter.sources.html_fallback import fetch_html
        return fetch_html(url)
```

Match on the **hostname**, not on a substring of the whole URL — a substring test
lets another board's link that merely mentions your domain get claimed by the
wrong fetcher (see `tests/test_scout_relay_url_domain_collision.py`).

Complete examples: `hunter/sources/justjoin.py` (candidate API), 
`hunter/sources/thesmartjobs.py` (detail API, 404 → EXPIRED marker), 
`hunter/sources/theprotocol.py` (dehydratedState + BeautifulSoup fallback).

## Step 4 — Add enable flag to `hunter/config.py`

```python
# ── {Site} source config ──────────────────────────────────────────────────────
# Set to "false" if the site is unreliable or requires special setup (e.g. Playwright)
{SITE}_ENABLED: bool = os.getenv("{SITE}_ENABLED", "true").lower() in ("true", "1", "yes")
```

## Step 5 — Register for the hunt cycle (`hunter/sources/__init__.py`)

Two edits in this file, both required:

```python
# 1. the config import block at the top of the file
from hunter.config import (
    ...,
    {SITE}_ENABLED,
)

# 2. the ALL_SOURCES registry
if {SITE}_ENABLED:
    from hunter.sources.{site} import {Site}Source

    ALL_SOURCES.append({Site}Source())
```

The source's position in `ALL_SOURCES` determines its schedule slot (offset by `SCHEDULE_SOURCE_OFFSET_MIN` per source).

## Step 6 — Register for detail-fetch dispatch (same file, `_fetch_roster()`)

```python
def _fetch_roster() -> list:
    ...
    from hunter.sources.{site} import {Site}Source

    _FETCH_ROSTER = [
        ...,
        {Site}Source(),
    ]
```

This roster is deliberately **independent of `{SITE}_ENABLED`**: a source excluded
from the hunt cycle must still be able to extract text from its own URLs, because
`apply_agent`, `expired_marker`, the repost gate and the Gmail enricher all reach
URLs the hunt never produced. Forgetting this step is the classic half-wired
source — listings appear, then every apply for them silently degrades to the
generic HTML fallback.

## Step 7 — Test

```bash
# Listing scraper standalone
python -c "
from hunter.sources.{site} import {Site}Source
jobs = {Site}Source().search()
for j in jobs[:5]:
    print(j.title, '|', j.company, '|', j.url)
print(f'Total: {len(jobs)}')
"

# Detail fetcher through the real dispatcher (proves Step 6 landed)
python -c "
from hunter.sources import fetch_job_text
text = fetch_job_text('https://{site}.com/some-job-url')
print(len(text), 'chars'); print(text[:500])
"

# Regression suite
pytest tests/test_sources_dispatcher.py tests/test_base_source_fetch_text.py -q
```

A dispatch test belongs in `tests/test_sources_dispatcher.py`: assert that a URL
of yours resolves to your source and — just as important — that a neighbouring
board's URL does not.

## Step 8 — Document it (same commit)

1. **CLAUDE.md → "Job Sources" table**: source name, module, strategy, notes.
2. **CLAUDE.md → "Scraper Health Notes" table**: today's date, `OK`, and what you
   actually verified live (job count, a sample title). This table is a log of
   verification, not of intent — do not add a row for code you have not run.
3. **CLAUDE.md → source-toggle list** (under Key Configuration): add
   `{SITE}_ENABLED` and its default.
4. **`.env.example`**: a commented toggle line next to its neighbours.
5. **Agent Work Log**: a dated row (5 most recent in CLAUDE.md, full history in
   `docs/AGENT_LOG.md`) — include the live yield you measured, so a future dry
   spell can be told apart from a source that was never productive.

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
