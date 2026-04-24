---
name: debug-scraper
description: Diagnose and fix a broken job board scraper. Reads the source file, fetches live HTML from the job board, inspects __NEXT_DATA__ / dehydratedState / RSS structure, compares to the current parser, and proposes a minimal targeted fix. Use when a scraper returns 0 results or throws errors.
---

You are diagnosing a broken job board scraper in the Job Hunter Bot project.

**Source:** $ARGUMENTS

## Steps

1. **Read the scraper files**
   - `hunter/sources/<source>.py` — the search scraper
   - `job_fetch/<source>.py` — the detail fetcher (if it exists)

2. **Identify the scraping strategy** (look at the code):
   - JSON API → check endpoint URL and response key names
   - `__NEXT_DATA__` / `dehydratedState` → Next.js site, inspect JSON structure in HTML
   - RSS feed → check feed URL and XML tag names
   - `cloudscraper` + BeautifulSoup → check CSS selectors / class names
   - Playwright → check JS selectors and wait conditions

3. **Fetch a live sample**
   Use WebFetch to fetch one real URL from the source (use a known job listing or the search endpoint).
   For API scrapers: fetch the API endpoint directly.
   For HTML scrapers: fetch the main search page.

4. **Compare live structure to parser expectations**
   - For Next.js sites: find `<script id="__NEXT_DATA__">` or `dehydratedState` in the HTML, extract the JSON, and compare key paths to what the parser expects
   - For APIs: compare the actual JSON response shape to what the parser reads
   - For RSS: check tag names and namespaces
   - For HTML: check if CSS selectors / class names still exist in the DOM

5. **Identify the mismatch**
   Common causes:
   - Key renamed (e.g. `jobOffers` → `offers`)
   - New nesting level added
   - Pagination param changed
   - Class name changed (e.g. `job-title` → `listing-title`)
   - API endpoint URL changed
   - Cloudflare/bot protection added

6. **Propose and apply a minimal fix**
   - Change only the broken part — do not refactor unrelated code
   - Keep the same code style as the rest of the file
   - After editing, run: `python -m compileall hunter/sources/ job_fetch/ -q`

7. **Report**
   - What was broken
   - What was changed
   - Whether a quick manual test is recommended
