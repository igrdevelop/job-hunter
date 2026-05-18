# Job Hunter Bot — Project Analysis & Refactoring Roadmap
**Date:** 2026-05-18  
**Codebase:** 21,333 lines Python · 40 test files · 18 job sources · 24 job fetchers

---

## 1. Executive Summary

The bot is functional and feature-complete: it scrapes 18 job boards, deduplicates, generates tailored CVs with multi-pass LLM quality gates, syncs to Google Sheets/Drive, and integrates with Telegram. The core pipeline works well.

The main structural risks are:
- **telegram_bot.py** (1 443 lines) and **apply_agent.py** (1 434 lines) are hard to test and maintain as monoliths
- **tracker.py** re-opens the Excel workbook 18+ times per operation — TrackerCache exists but is underutilised
- Test coverage misses the two most critical modules (telegram_bot, apply_agent)
- Config has no validation layer — invalid env vars fail silently at runtime

---

## 2. Codebase Map by Size

| File | Lines | Risk |
|------|-------|------|
| `hunter/telegram_bot.py` | 1 443 | HIGH — untested monolith |
| `apply_agent.py` | 1 434 | HIGH — untested dual pipeline |
| `hunter/tracker.py` | 1 052 | MEDIUM — workbook thrashing |
| `hunter/gsheets_sync.py` | 479 | LOW |
| `hunter/sources/pracuj.py` | 438 | LOW |
| `hunter/sources/theprotocol.py` | 429 | LOW |
| `hunter/main.py` | 385 | LOW |
| `generate_docs.py` | 378 | LOW |
| `hunter/filters.py` | 293 | LOW |
| `hunter/config.py` | 252 | MEDIUM — no validation |

---

## 3. Findings by Area

### 3.1 apply_agent.py — Dual Pipeline Monolith

The file contains two full pipelines (API mode and CLI mode) plus shared utilities,
all in one 1 434-line file.

**Structure:**

```
Lines   1 – 106   Imports, constants, Telegram helpers (notify, send_telegram_documents)
Lines 107 – 184   Telegram document upload (send_telegram_documents)
Lines 188 – 442   Cover letter quality gates (7 checks, 48 regex patterns)
Lines 443 – 578   ATS independence check + 95% rewrite loop
Lines 579 – 733   Shared utilities (build_prompts, parse_content_json, etc.)
Lines 735 –1 015  API pipeline: main_api()
Lines 1 016–1 082 Shared folder utilities (_find_new_folder)
Lines 1 084–1 283 CLI pipeline: main_cli()
Lines 1 284–1 434 Entry point: main() + fallback chain
```

**Issues:**

1. **Cover letter review duplicated** — appears in both `main_api()` (line 917) and
   `main_cli()` (line 1 235). Shared logic, two maintenance points.

2. **Folder detection logic duplicated** — `_find_new_folder()` contains two branches
   (new-style vs legacy structure) that could be unified.

3. **Telegram helpers** (`notify`, `send_telegram_documents`) belong in
   `hunter/telegram_bot.py` or a dedicated `hunter/notify.py` — not in the apply agent.

4. **No unit tests** — the 7-gate cover letter checker, ATS rewrite loop, and React-only
   skip logic are only exercised via end-to-end runs.

**Proposed split:**

```
apply_agent.py            Thin CLI entry point + fallback chain (< 60 lines)
hunter/apply_api.py       API pipeline
hunter/apply_cli.py       CLI pipeline
hunter/apply_shared.py    cover_letter_review(), ats_check_loop(), build_prompts()
hunter/notify.py          notify(), send_telegram_documents()
```

---

### 3.2 hunter/telegram_bot.py — Handler Monolith

21 handlers, 5 scheduled jobs, `build_application()`, and all inline bot state in one file.

**Handlers:** `/start`, `/hunt`, `/force`, `/process_manual`, `/status`, `/schedule`,
`/unsent`, `/sync_sent`, `/check_expired`, `/about_me`, `/gsheets_status`,
`/gsheets_resync`, `/gsheets_push_missing`, `/gdrive_upload_missing`,
button_callback (Apply/Skip), URL message handler, LinkedIn batch handler.

**In-memory state (breaks on restart):**
- `_pending_jobs: dict[str, Job]` — jobs awaiting Apply/Skip button press
- `_active_apply_urls: set[str]` — jobs currently generating docs

**Issues:**

1. **Zero test coverage** — handlers can't be tested without starting the full bot.
   The `build_application()` function is 250 lines of schedule setup with no tests.

2. **All 21 handlers in one file** — adding or debugging a single command requires
   navigating 1 443 lines.

3. **`_pending_jobs` lost on restart** — jobs sent to Telegram before a restart have
   non-functional Apply/Skip buttons. The dict should persist to disk or the
   tracker should be queried on button press.

4. **Schedule magic strings** — times "08:00", "13:00", "19:00" are hardcoded
   in `build_application()` (lines 1 220–1 424). Should come from config.

**Proposed split:**

```
hunter/telegram_bot.py           Thin dispatcher: build_application(), send_job_card()
hunter/commands/hunt.py          /hunt, /force handlers
hunter/commands/status.py        /status, /schedule, /unsent handlers
hunter/commands/tracker.py       /sync_sent, /check_expired handlers
hunter/commands/google.py        /gsheets_*, /gdrive_* handlers
hunter/commands/apply.py         Apply/Skip callbacks, URL message handler
hunter/app.py                    build_application(), schedule setup
```

---

### 3.3 hunter/tracker.py — Workbook Thrashing

**18 functions that call `openpyxl.load_workbook()`**, each re-reading the entire
Excel file from disk. `TrackerCache` already exists at `hunter/tracker_cache.py`
(asyncio.Lock, O(1) dedup + stats) but is only used for dedup and unsent counts — not for
lookup operations.

**Impact:** Each hunt iteration calls `is_known(url, company, title)` which triggers
two full workbook loads. With 18 sources × 25 jobs each × 3 hunt cycles/day = ~1 350
unnecessary workbook reads per day.

**Most expensive callers:**

| Function | Called from | Workbook opens |
|----------|-------------|----------------|
| `get_known_urls()` | is_known() | every dedup check |
| `get_known_company_titles()` | is_known() | every dedup check |
| `get_failed_jobs()` | main.py retry | per hunt cycle |
| `add_applied()` | generate_docs | per successful apply |

**Quick win:** Extend `TrackerCache` to hold known URLs and company+title sets;
invalidate on every write. This eliminates the read path without restructuring writes.

**Long-term (Phase 5 in CLAUDE.md):** Replace Excel with SQLite. Atomic writes,
no PermissionError on concurrent access, indexed queries.

---

### 3.4 hunter/config.py — No Validation Layer

**Issues:**

1. **No required-var check** — if `TELEGRAM_BOT_TOKEN` is missing, the bot starts and
   fails later at the first API call with a cryptic error. Should raise `SystemExit`
   at startup.

2. **Inconsistent bool parsing** — two patterns used:
   ```python
   # Pattern A (lines 11–14):
   os.getenv("VAR", "true").lower() in ("true", "1", "yes")
   # Pattern B (lines 57–58):
   os.getenv("VAR", "false") == "true"
   ```
   Should be one `_parse_bool(name, default)` helper covering all 18 boolean flags.

3. **No range checks** — `SCHEDULE_SOURCE_OFFSET_MIN` can be 0 or negative,
   causing sources to pile up at the same minute. `MAX_JOBS_PER_RUN=0` silently
   disables all job processing.

4. **Hardcoded schedule times** — "08:00", "13:00", "19:00" duplicated between
   `config.py` and `telegram_bot.py`. Single source of truth should be in config.

**Proposed additions:**

```python
def _parse_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, str(default)).lower().strip()
    return val in ("true", "1", "yes")

def validate_config() -> None:
    """Call at startup. Raises SystemExit on fatal misconfiguration."""
    missing = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(v)]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")
    if SCHEDULE_SOURCE_OFFSET_MIN < 0:
        sys.exit("SCHEDULE_SOURCE_OFFSET_MIN must be >= 0")
```

---

### 3.5 hunter/sources/__init__.py — Import Fragility

All 18 conditional source imports happen at module load time. Any import error
(missing dependency, syntax error in a source file) crashes the entire bot, even if
that source is disabled.

**Current pattern:**
```python
if config.LINKEDIN_ENABLED:
    from .linkedin import LinkedIn
    ALL_SOURCES.append(LinkedIn())
```

**Problem:** If `linkedin.py` has a syntax error, the whole `__init__.py` fails —
even if `LINKEDIN_ENABLED=false`.

**Fix:** Wrap conditional imports in try/except:
```python
if config.LINKEDIN_ENABLED:
    try:
        from .linkedin import LinkedIn
        ALL_SOURCES.append(LinkedIn())
    except Exception as e:
        logger.warning("LinkedIn source disabled (import error): %s", e)
```

---

### 3.6 job_fetch/__init__.py — Domain Matching Bug

Domain dispatch uses string containment (`if "linkedin.com" in domain`), which
can match crafted subdomains like `linkedin.com.attacker.com`.

**Current (line 68):**
```python
if "linkedin.com" in domain:
    return fetch_linkedin(url)
```

**Safer:**
```python
if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
    return fetch_linkedin(url)
```

This is a low-severity issue (job URLs come from trusted scrapers) but worth fixing.

---

### 3.7 Test Coverage Gaps

| Module | Lines | Test coverage |
|--------|-------|---------------|
| `hunter/telegram_bot.py` | 1 443 | **None** |
| `apply_agent.py` | 1 434 | **None** |
| `hunter/main.py` | 385 | **None** |
| `hunter/filters.py` | 293 | Partial (German lang, React stack) |
| `hunter/config.py` | 252 | 1 file (config_unification) |

The cover letter review loop (7 gates, 48 patterns), ATS rewrite loop, and all
Telegram command handlers have no automated tests.

**Highest-value tests to add:**

1. `tests/test_cover_letter_review.py` — test each of the 7 quality gates independently
2. `tests/test_ats_loop.py` — mock LLM responses, verify rewrite triggers at < 95%
3. `tests/test_main_hunt.py` — mock sources + tracker, verify dedup and filter chain
4. `tests/test_apply_agent.py` — test API pipeline with mocked LLM and job_fetch

---

## 4. New Features to Consider

### 4.1 Persistent `_pending_jobs` (Quick Win)

**Problem:** Apply/Skip buttons in Telegram become non-functional after bot restart.  
**Solution:** Persist `_pending_jobs` to a JSON file on shutdown; restore on startup.
Alternatively, on Apply button press, look up the job URL in the tracker MANUAL row
instead of in-memory dict.

**Effort:** ~2 hours. No schema changes required.

---

### 4.2 `/stats` Command

**Problem:** No visibility into bot performance over time.  
**Proposed output:**
```
📊 Stats (last 30 days)
  Hunted: 1 247 jobs
  Filtered: 891 (71%)
  Applied: 23
  Skipped: 12
  Expired: 8
  ATS avg: 91%
  Cover letter rewrites avg: 1.4
```
**Data source:** tracker.xlsx already has all this data.  
**Effort:** ~3 hours.

---

### 4.3 Per-Source Hunt Stats in `/status`

**Problem:** `/status` shows schedule but not whether sources returned results.  
**Add:** Last-run result per source (jobs found, errors, duration).  
**Store:** In-memory dict updated after each source run; cleared on bot restart.  
**Effort:** ~2 hours.

---

### 4.4 ATS Score Trend Tracking

**Problem:** ATS % is stored per application but never aggregated.  
**Proposal:** After each apply, update a rolling `ats_stats.json`:
```json
{
  "total_runs": 47,
  "avg_score": 91.2,
  "score_distribution": {"90-95": 18, "95-100": 22, "below_90": 7},
  "rewrite_rate": 0.34
}
```
**Effort:** ~1 hour.

---

### 4.5 Duplicate Offer Detection Across Boards

**Problem:** The same job is often posted on multiple boards (LinkedIn + NoFluffJobs + company ATS).
Current dedup is URL-based; company+title dedup catches some, but not all.

**Proposal:** Fuzzy title match (difflib or rapidfuzz) on (company, normalized_title).
Threshold ~85% similarity → treat as duplicate.

**Risk:** False positives (two genuinely different roles at the same company).
Should surface as "possible duplicate" in Telegram card rather than hard-skip.

**Effort:** ~4 hours.

---

### 4.6 Healthcheck Endpoint

**Problem:** No way to verify the bot is alive without sending a Telegram message.  
**Proposal:** Add a minimal HTTP endpoint (`/healthz`) served alongside the webhook.
Returns `{"status": "ok", "uptime": 3600, "last_hunt": "2026-05-18T13:40:00"}`.

**Effort:** ~2 hours. Can use `aiohttp` (already implicitly available via
`python-telegram-bot`).

---

### 4.7 Structured Logging

**Problem:** Logs are plain text (`print()` + `logger.info()`); no structured format
makes log aggregation in Docker/CloudWatch difficult.

**Proposal:** Add `python-json-logger` and output JSON lines in production
(`LOG_FORMAT=json` env var). Stays human-readable in dev.

**Effort:** ~2 hours.

---

### 4.8 Cover Letter Language Auto-Detection

**Problem:** Polish cover letter is always generated even for clearly English-language
postings and companies, adding ~30s LLM time.

**Proposal:** Detect job posting language with `langdetect` (or simple heuristic:
if > 80% English words → skip PL translation). Skip PL generation when not needed.

**Effort:** ~2 hours.

---

## 5. Refactoring Roadmap

Priority order based on risk/reward. Each phase is independent; they can be done
in any order after Phase 0.

### Phase 0 — Config Validation (1 day, LOW risk)

- [ ] Add `_parse_bool(name, default)` helper; migrate all 18 boolean flags
- [ ] Add `validate_config()` with required-var check and range checks
- [ ] Call `validate_config()` in `hunter.py` before starting bot
- [ ] Add schedule time constants to config (replace magic strings in telegram_bot.py)

**Why first:** Runtime failures from missing config are the most user-visible issue
and the fix has zero architectural impact.

---

### Phase 1 — Quick Wins (2–3 days, LOW risk)

- [ ] Wrap conditional source imports in try/except (sources/__init__.py)
- [ ] Fix domain matching in job_fetch/__init__.py (== instead of `in`)
- [ ] Persist `_pending_jobs` to JSON so Apply/Skip survive restart
- [ ] Extend TrackerCache to cover URL/company lookup (eliminate workbook read path)
- [ ] Add `/stats` command (30-day summary from tracker.xlsx)

---

### Phase 2 — Split apply_agent.py (3–5 days, MEDIUM risk)

- [ ] Extract `cover_letter_review()` and `ats_check_loop()` to `hunter/apply_shared.py`
- [ ] Move `notify()` and `send_telegram_documents()` to `hunter/notify.py`
- [ ] Extract API pipeline to `hunter/apply_api.py`
- [ ] Extract CLI pipeline to `hunter/apply_cli.py`
- [ ] Keep `apply_agent.py` as thin entry point (< 60 lines)
- [ ] Add unit tests for each extracted module

**Validates:** After split, add tests for cover letter gates, ATS loop, and React-only skip.

---

### Phase 3 — Split telegram_bot.py (3–5 days, MEDIUM risk)

- [ ] Extract command handlers to `hunter/commands/` (one file per domain)
- [ ] Extract `build_application()` and schedule setup to `hunter/app.py`
- [ ] Keep `telegram_bot.py` as thin dispatcher with `send_job_card()` API
- [ ] Add handler tests using `python-telegram-bot` test utilities

---

### Phase 4 — SQLite Tracker (5–7 days, HIGH impact)

Replace `tracker.xlsx` writes with SQLite; keep Excel only for export/import.

- [ ] Design schema (`jobs` table; same columns as tracker.xlsx)
- [ ] Implement `hunter/db.py` with connection pool + migrations
- [ ] Migrate all tracker.py write functions to SQLite
- [ ] Keep read functions pointing to SQLite (drop TrackerCache — SQLite is fast enough)
- [ ] Add `/export` Telegram command → generate xlsx from SQLite
- [ ] Keep `openpyxl` only for export and Google Sheets bootstrap

**Benefits:**
- Atomic writes; no PermissionError when xlsx is open in Excel
- `SELECT COUNT(*) WHERE status='APPLIED'` replaces full workbook scans
- Opens door to richer `/stats` queries

---

### Phase 5 — Project Packaging (1–2 days, LOW risk)

- [ ] Add `pyproject.toml` with metadata, entry points, mypy config
- [ ] `python -m hunter` entry point
- [ ] Type annotations in config.py and models.py
- [ ] `pip install -e .` works cleanly

---

## 6. Dependency Audit

| Package | Version | Issue |
|---------|---------|-------|
| `scikit-learn` | — | Listed in requirements but no usage found outside ATS scorer; verify if needed |
| `cloudscraper` | unpinned | Heavy dependency; consider `curl_cffi` as lighter alternative |
| `google-api-python-client` | `==2.131.0` | Pinned; check for security updates |
| `google-auth` | `==2.29.0` | Pinned; check for security updates |
| `playwright` | commented | Only needed for Inhire; document explicit install step |
| `pytz` | unpinned | Superseded by `zoneinfo` (stdlib, Python 3.9+) — can drop |

**Recommendation:** Run `pip-audit` to check for known CVEs in pinned packages.

---

## 7. Summary Table

| Issue | Severity | Effort | Phase |
|-------|----------|--------|-------|
| No config validation | HIGH | 0.5d | 0 |
| apply_agent.py monolith (no tests) | HIGH | 4d | 2 |
| telegram_bot.py monolith (no tests) | HIGH | 4d | 3 |
| _pending_jobs lost on restart | MEDIUM | 2h | 1 |
| Workbook thrashing (18 re-opens) | MEDIUM | 1d | 1 |
| Import fragility in sources/__init__ | MEDIUM | 1h | 1 |
| Domain matching bug (job_fetch) | LOW | 30m | 1 |
| Bool parsing inconsistency | LOW | 1h | 0 |
| pytz → zoneinfo | LOW | 30m | 5 |
| SQLite tracker | HIGH impact | 6d | 4 |
| Duplicate detection (fuzzy match) | NICE | 4h | — |
| Healthcheck endpoint | NICE | 2h | — |
| Structured logging | NICE | 2h | — |
| /stats command | NICE | 3h | 1 |
