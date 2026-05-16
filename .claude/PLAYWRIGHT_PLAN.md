# Playwright Installation Plan

## Problem

Pracuj.pl (and potentially theprotocol.it) returns HTTP 403 from the VPS because
Cloudflare blocks datacenter IPs at the network level. `cloudscraper` only handles
JS-challenge bypasses — it cannot overcome IP-reputation blocks.

Playwright with a real Chromium browser is much harder for Cloudflare to fingerprint
as a bot, making it the most reliable fix without a proxy.

## Goals

1. **Install Playwright + Chromium** in Docker
2. **Migrate Pracuj.pl** from `cloudscraper` → Playwright (primary fix)
3. **Migrate theprotocol.it** from `cloudscraper` → Playwright (same issue, same fix)
4. **Inhire.io** — already Playwright-ready, will just start working automatically
5. **LinkedIn job_fetch** — already has Playwright path, needs session file on VPS

## Docker size impact

- Current image: ~800MB
- Adding Playwright + Chromium: +400–500MB
- Expected final size: ~1.2–1.3GB

Acceptable trade-off: Pracuj has historically provided 10–15 jobs per run.

---

## Files to Modify

### `requirements.txt`

Uncomment `playwright`. Remove `playwright-stealth` (optional, skip for now).

```
# Before:
# playwright  # install separately: pip install playwright && playwright install chromium
# playwright-stealth

# After:
playwright
```

### `Dockerfile`

Add Chromium install **after** `pip install`. Use `--with-deps` to pull system libs
(libglib2.0, libnss, etc.) that Chromium needs on Debian slim.

```dockerfile
# Before:
RUN pip install --no-cache-dir -r requirements.txt

# After:
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps
```

> Note: `playwright install chromium --with-deps` is preferred over
> `playwright install --with-deps` (which installs all 3 browsers = +1GB).

### `hunter/sources/pracuj.py`

Replace `cloudscraper` fetch with Playwright headless fetch.

**New strategy:**
1. Launch Chromium headless (shared browser instance per search run)
2. Navigate to listing URL, wait for `__NEXT_DATA__` to be present in DOM
3. Extract `window.__NEXT_DATA__` via `page.evaluate()`
4. Parse the JSON — same logic as current `_extract_next_data()`
5. Close browser after all 3 listing URLs are fetched

**Key implementation details:**
- Use `sync_playwright` (PracujSource.search() is sync, called from a thread)
- Single browser instance per `search()` call — open once, fetch all 3 pages, close
- Timeout: 30s per page (Cloudflare challenge can take a few seconds to resolve)
- Wait condition: `networkidle` or presence of `#__NEXT_DATA__` script tag
- On ImportError → fall back to `cloudscraper` with a warning (backward compat)

**Graceful degradation:**
```python
def search(self) -> list[Job]:
    try:
        import playwright  # noqa
        return self._search_playwright()
    except ImportError:
        logger.warning("[Pracuj] playwright not installed, falling back to cloudscraper")
        return self._search_cloudscraper()
```

### `hunter/sources/theprotocol.py`

Same migration as Pracuj. theprotocol.it is also a Next.js SPA behind Cloudflare.

- Use `sync_playwright`
- Wait for `networkidle`
- Extract `window.__NEXT_DATA__` via JS eval
- Same graceful fallback to `cloudscraper`

---

## Shared Playwright Helper (optional but recommended)

If both Pracuj and theprotocol need the same "launch browser → fetch page HTML →
close" pattern, extract a helper to avoid duplication:

### `hunter/playwright_helper.py` (NEW, optional)

```python
"""Shared headless fetch helper for Cloudflare-protected sites."""

from contextlib import contextmanager
from typing import Iterator

BROWSER_TIMEOUT = 30_000  # ms


@contextmanager
def chromium_page(url: str, wait_until: str = "networkidle") -> Iterator[str]:
    """Context manager: yields page HTML after Playwright load. Raises on error."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page.goto(url, wait_until=wait_until, timeout=BROWSER_TIMEOUT)
            yield page
        finally:
            browser.close()
```

Both Pracuj and theprotocol call:
```python
with chromium_page(url) as page:
    html = page.content()
    # ... existing _extract_next_data(html) logic unchanged
```

**Benefit:** `_extract_next_data()`, `_parse()`, `_build_url()` — all unchanged.
Only the HTTP fetch layer swaps from `cloudscraper.get()` to `page.content()`.

---

## LinkedIn session setup on VPS (manual step, post-deploy)

After Playwright is installed in Docker, LinkedIn job_fetch can use a real session.

```bash
# On VPS, exec into container:
docker exec -it job-hunter bash

# Run the login tool (opens browser — but headless on VPS, won't work directly)
# Instead: run locally, copy the session file to VPS
```

**Correct flow:**
1. Run `python tools/linkedin_login.py` **locally** (opens real browser window)
2. Log in to LinkedIn
3. Copy the generated `linkedin_storage_state.json` to VPS
4. Add to `.env` on VPS: `LINKEDIN_STORAGE_STATE=/path/to/linkedin_storage_state.json`
5. Restart container

This is optional — LinkedIn job_fetch already falls back to HTML without session.

---

## Testing

### Local (before push)

```bash
# Install playwright locally first:
pip install playwright
playwright install chromium

# Smoke test Pracuj:
python -c "
from hunter.sources.pracuj import PracujSource
jobs = PracujSource().search()
print(f'Pracuj: {len(jobs)} jobs')
for j in jobs[:3]:
    print(f'  {j.title} | {j.company} | {j.location}')
"

# Smoke test theprotocol:
python -c "
from hunter.sources.theprotocol import TheProtocolSource
jobs = TheProtocolSource().search()
print(f'theprotocol: {len(jobs)} jobs')
"

# Full test suite:
pytest tests/ -q
```

### After deploy (on VPS)

```bash
# Check image size:
docker images ghcr.io/igrdevelop/job-hunter:latest

# Smoke test inside container:
docker exec job-hunter python -c "
from hunter.sources.pracuj import PracujSource
jobs = PracujSource().search()
print(f'{len(jobs)} jobs from Pracuj')
"

# Or just run a manual hunt:
# /hunt pracuj  (from Telegram)
```

---

## Implementation Order

1. `requirements.txt` — uncomment `playwright`
2. `Dockerfile` — add `RUN playwright install chromium --with-deps`
3. `hunter/playwright_helper.py` — shared `chromium_page()` context manager
4. `hunter/sources/pracuj.py` — migrate fetch to Playwright with cloudscraper fallback
5. `hunter/sources/theprotocol.py` — same migration
6. `python -m compileall hunter/ -q`
7. `pytest tests/ -q` (existing tests should pass unchanged)
8. Local smoke test: Pracuj + theprotocol return > 0 jobs
9. Commit + push → PR → merge → CI builds new image → deploy

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Docker build time +3–5 min | High | One-time cost |
| Image size +500MB | Certain | Accepted trade-off |
| Playwright on VPS still blocked | Low | Chromium is much harder to fingerprint |
| `networkidle` timeout on slow pages | Medium | Fallback to `domcontentloaded` + explicit wait for `#__NEXT_DATA__` |
| Chromium crashes in low-memory VPS | Low | Add `--no-sandbox` flag if needed |

---

## Notes for Agent

- Do NOT change `_extract_next_data()`, `_parse()`, `_build_url()` — only swap the HTTP layer
- Keep `cloudscraper` as fallback (don't remove the import or `_scraper` instance)
- Inhire already uses `async_playwright` — Pracuj/theprotocol should use `sync_playwright` (they're sync sources)
- `playwright install chromium --with-deps` installs Chromium only, not Firefox or WebKit
- If VPS has <1GB RAM: add `args=['--no-sandbox', '--disable-dev-shm-usage']` to `browser.launch()`
