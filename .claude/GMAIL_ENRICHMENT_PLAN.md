# Gmail URL Enrichment — Implementation Plan

## Problem

`gmail_parsers.py` extracts job URLs from alert emails and wraps them in placeholder `Job` objects:
- `title` = email subject (`"Jobs for you"`, `"10 new jobs matching Angular"`)
- `company` = `"[source]"` (e.g. `"[justjoin]"`)
- `location` = `""`
- `salary` = `None`

These stubs pass dedup (URL-based) but **fail the title-keyword filter** — a job titled `"Jobs for you"` matches none of `["angular", "frontend", "javascript", "typescript"]`. Result: valid jobs silently dropped before they reach Telegram.

## Solution

After Gmail parsers produce stub jobs, fetch real metadata from each URL in parallel. Replace stub fields with real `title`, `company`, `location`, `salary`. Dedup and filters then operate on correct data.

---

## Files to Create

### `hunter/gmail_enricher.py` (NEW)

Main module. Exports one public function: `enrich_jobs(jobs) -> list[Job]`.

```
hunter/gmail_enricher.py
```

**Structure:**

```python
"""
Gmail Job Enricher — fetches real title/company/location/salary
from job URLs extracted from alert emails.

Per-source strategies:
  JustJoin    → GET /api/candidate-api/offers/{slug}  (structured JSON, same API as the source)
  NoFluffJobs → GET https://nofluffjobs.com/api/posting/{slug}  (structured JSON)
  Bulldogjob  → HTML page (__NEXT_DATA__ Apollo), parsed via job_fetch.bulldogjob
  Pracuj      → __NEXT_DATA__ JSON, parsed via job_fetch.pracuj
  LinkedIn    → Playwright + saved session; risky in batch — skip if Playwright unavailable

Fallback: if any enricher raises, the original stub Job is kept unchanged.
Dedup still works because URL is the canonical key, regardless of stub title.
"""
```

#### `_enrich_justjoin(job: Job) -> Job`

- Extract slug via `re.search(r"/(?:job-offer|offers)/([a-z0-9-]+)", job.url)`
- `GET https://justjoin.it/api/candidate-api/offers/{slug}` with same headers as `job_fetch/justjoin.py`
- Parse response JSON (identical shape to listing API):
  - `title` = `data["title"]`
  - `company` = `data["companyName"]`
  - `location` = same logic as `JustJoinSource._parse_location` (city + workplaceType)
  - `salary` = `JustJoinSource._parse_salary(data)` (reuse static method)
- Return new `Job(title, company, location, salary, url=job.url, source=job.source, raw=data)`
- On any error → return original `job`

#### `_enrich_nofluffjobs(job: Job) -> Job`

- Extract slug via `re.search(r"/job/([a-zA-Z0-9_-]+)", job.url)`
- `GET https://nofluffjobs.com/api/posting/{slug}` — same headers as `job_fetch/nofluffjobs.py`
- Parse structured JSON (already reverse-engineered in `job_fetch/nofluffjobs.py`):
  - `title` = `data["title"]`
  - `company` = `data["name"]`
  - `location`:
    - `"Remote"` if `data.get("fullyRemote")`
    - else join `p["city"]` for `p` in `data["location"]["places"]`
  - `salary`:
    - from `data["essentials"]["salary"]`: `{from}–{to} {currency} {type}`
- Return enriched Job or original on error

#### `_enrich_via_text(job: Job) -> Job`

Generic fallback for Bulldogjob, Pracuj (and any future HTML-based source).

- Call `job_fetch.fetch_job_text(job.url)` — returns structured plain-text starting with `"Job Title: ..."`, `"Company: ..."`
- Parse with:
  ```python
  title   = re.search(r"Job Title:\s*(.+)", text)
  company = re.search(r"Company:\s*(.+)", text)
  location_m = re.search(r"Location:\s*(.+)", text)
  salary_m   = re.search(r"Salary:\s*(.+)", text)
  ```
- Only update fields that were successfully parsed AND non-empty
- Keep original stub fields for anything that fails to parse
- On any exception → return original `job`

> Note: LinkedIn uses Playwright and may not be available in Docker (Playwright not always installed).
> `_enrich_via_text` will catch the `RuntimeError("playwright not installed")` or
> `RuntimeError("LinkedIn storage_state not set")` and silently return the stub.
> LinkedIn stubs WILL still be deduped correctly by URL — they just won't pass keyword filter.
> Future improvement: add a lightweight LinkedIn guest API enricher (no Playwright needed).

#### `_enrich_one(job: Job) -> Job`

Dispatcher:

```python
def _enrich_one(job: Job) -> Job:
    domain = (urlparse(job.url).hostname or "").lower()
    try:
        if "justjoin.it" in domain:
            return _enrich_justjoin(job)
        if "nofluffjobs.com" in domain:
            return _enrich_nofluffjobs(job)
        if any(d in domain for d in ("bulldogjob.com", "bulldogjob.pl", "pracuj.pl")):
            return _enrich_via_text(job)
        if "linkedin.com" in domain:
            return _enrich_via_text(job)  # Playwright; silently skips if unavailable
    except Exception as e:
        logger.debug(f"[gmail_enricher] {domain}: {e}")
    return job
```

#### `enrich_jobs(jobs: list[Job]) -> list[Job]`

Public entry point. Runs enrichment in parallel using `ThreadPoolExecutor`.

```python
def enrich_jobs(jobs: list[Job]) -> list[Job]:
    """Enrich Gmail stub jobs with real metadata. Thread-parallel, best-effort."""
    if not jobs:
        return jobs
    enriched: dict[str, Job] = {}
    with ThreadPoolExecutor(max_workers=GMAIL_ENRICH_CONCURRENCY) as pool:
        future_to_url = {pool.submit(_enrich_one, job): job.url for job in jobs}
        for future in as_completed(future_to_url, timeout=GMAIL_ENRICH_TIMEOUT * 3):
            try:
                result = future.result(timeout=GMAIL_ENRICH_TIMEOUT)
                enriched[result.url] = result
            except Exception as e:
                url = future_to_url[future]
                logger.debug(f"[gmail_enricher] timeout/error for {url}: {e}")
                # keep original
                for job in jobs:
                    if job.url == url:
                        enriched[url] = job
                        break
    # preserve original ordering
    return [enriched.get(j.url, j) for j in jobs]
```

**Imports needed:**
```python
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from hunter.config import GMAIL_ENRICH_CONCURRENCY, GMAIL_ENRICH_TIMEOUT
from hunter.models import Job
from hunter.sources.justjoin import JustJoinSource
```

---

## Files to Modify

### `hunter/config.py`

Add to the Gmail source config section (after `GMAIL_ENABLED`):

```python
# Whether to fetch real title/company/location for each URL extracted from emails.
# Disable if you want faster (but filter-blind) Gmail processing.
GMAIL_ENRICH_ENABLED: bool = os.getenv("GMAIL_ENRICH_ENABLED", "true").lower() in ("true", "1", "yes")
# Max parallel HTTP requests during Gmail enrichment
GMAIL_ENRICH_CONCURRENCY: int = int(os.getenv("GMAIL_ENRICH_CONCURRENCY", "5"))
# Per-job HTTP timeout (seconds) for enrichment fetches
GMAIL_ENRICH_TIMEOUT: int = int(os.getenv("GMAIL_ENRICH_TIMEOUT", "15"))
```

### `hunter/sources/gmail.py`

Modify `_fetch_jobs` to call enricher after extracting jobs.

Before (end of method):
```python
        logger.info(f"[gmail] Extracted {len(jobs)} job URLs total")
        return jobs
```

After:
```python
        logger.info(f"[gmail] Extracted {len(jobs)} job URLs total")

        if jobs and GMAIL_ENRICH_ENABLED:
            from hunter.gmail_enricher import enrich_jobs
            jobs = enrich_jobs(jobs)
            logger.info(f"[gmail] After enrichment: {len(jobs)} jobs")

        return jobs
```

Add import at top of file:
```python
from hunter.config import GMAIL_ENRICH_ENABLED
```

### `hunter/gmail_parsers.py`

No changes needed to parsers themselves.

Update the module docstring to document the enrichment step:
```
title/company/location/salary are stubs — gmail_enricher.enrich_jobs() fills in
real values by fetching each job URL before the filter pipeline runs.
```

---

## What NOT to change

- `hunter/filters.py` — no changes; enriched jobs will naturally pass keyword/location filters
- `job_fetch/__init__.py` — `fetch_job_text` is called by `_enrich_via_text` as-is; no changes
- `hunter/main.py` — hunt loop already receives `list[Job]` from `GmailSource.search()`; enrichment happens inside the source

---

## Testing

### Manual smoke test (after implementation)

```bash
# Set GMAIL_ENABLED=true + GMAIL_ENRICH_ENABLED=true in .env, then:
docker exec job-hunter python3 -c "
from hunter.sources.gmail import GmailSource
jobs = GmailSource().search()
for j in jobs[:5]:
    print(j.title, '|', j.company, '|', j.location, '|', j.url)
"
```

Expected: titles like `"Senior Angular Developer"` instead of `"Jobs for you"`.

### Unit test additions (`tests/test_gmail_enricher.py`)

1. `test_enrich_justjoin_happy_path` — mock `requests.get`, verify title/company/location/salary populated
2. `test_enrich_nofluffjobs_happy_path` — same
3. `test_enrich_justjoin_api_error` — 404 response → original stub returned
4. `test_enrich_nofluffjobs_api_error` — network error → original stub returned
5. `test_enrich_jobs_preserves_order` — enriched list has same order as input
6. `test_enrich_jobs_unknown_domain` — unrecognized domain → stub returned unchanged
7. `test_enrich_disabled` — when `GMAIL_ENRICH_ENABLED=false`, `enrich_jobs` never called

---

## Performance

With `GMAIL_ENRICH_CONCURRENCY=5` and `GMAIL_ENRICH_TIMEOUT=15`:

- Typical Gmail alert has 5–15 job URLs
- 5 parallel workers finish 15 jobs in ~3 requests × 15s / 5 workers ≈ **9 seconds**
- Total Gmail source overhead: ~10–20s per run (acceptable vs. 40-min source offset)

If enrichment is too slow on the server, set `GMAIL_ENRICH_CONCURRENCY=3` or `GMAIL_ENRICH_TIMEOUT=10` in `.env`.

---

## Implementation Order

1. Add 3 config vars to `hunter/config.py`
2. Create `hunter/gmail_enricher.py` with `_enrich_justjoin`, `_enrich_nofluffjobs`, `_enrich_via_text`, `_enrich_one`, `enrich_jobs`
3. Modify `hunter/sources/gmail.py` — add `GMAIL_ENRICH_ENABLED` import + call `enrich_jobs` in `_fetch_jobs`
4. Update `hunter/gmail_parsers.py` docstring
5. Add `tests/test_gmail_enricher.py` (at least the happy-path and error-fallback cases)
6. `python -m compileall hunter/ -q`
7. `pytest tests/test_gmail_enricher.py -v`
