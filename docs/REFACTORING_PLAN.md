# Job Hunter Bot — Refactoring & Update Plan
**Created:** 2026-05-18  
**Status:** Approved, not started  
**Branch:** `docs/project-analysis-2026-05`

> This document is the executable roadmap. Mark tasks `[x]` as work is completed.
> See [`PROJECT_ANALYSIS_2026_05.md`](PROJECT_ANALYSIS_2026_05.md) for the full analysis behind each decision.

---

## Summary

| Phase | What | Risk | Effort |
|-------|------|------|--------|
| **0** | Config validation + `_parse_bool` | ZERO | 0.5d |
| **1.1** | Source import guard | LOW | 1h |
| **1.2** | Domain matching fix | LOW | 30m |
| **1.3** | Persistent `_pending_jobs` | LOW | 3h |
| **1.4** | TrackerCache dedup in `main.py` | LOW | 2h |
| **1.5** | `/stats` command | LOW | 3h |
| **1.6** | Structured logging (JSON) | LOW | 2h |
| **2** | Split `apply_agent.py` | MEDIUM | 4d |
| **3** | Split `telegram_bot.py` + healthcheck | MEDIUM | 4d |
| **4** | SQLite tracker | HIGH impact | 6d |
| **5** | Packaging + dependency audit | LOW | 2d |

**Total: ~22 dev-days.** Phases 0–1 ship in < 1 week with no architectural changes.

---

## Phase 0 — Config Validation

**Goal:** Fail fast on misconfiguration; unified bool parsing.

### Files
- `hunter/config.py`
- `hunter.py`

### Tasks
- [ ] **0.1** Add `_parse_bool(name, default)` helper to `config.py`; migrate all 18+ bool flags
  ```python
  def _parse_bool(name: str, default: bool) -> bool:
      val = os.getenv(name, "true" if default else "false").lower().strip()
      return val in ("true", "1", "yes")
  ```
  Flags to migrate: `TELEGRAM_SEND_DOCS`, `AUTO_APPLY`, `APPLY_USE_CLI`, `GENERATE_PL_RESUME`,
  `TRACKER_BACKUP_ENABLED`, and all 16 `*_ENABLED` source flags.

- [ ] **0.2** Add `validate_config()` at the bottom of `config.py`:
  ```python
  def validate_config() -> None:
      import sys
      errors = []
      if not TELEGRAM_BOT_TOKEN:
          errors.append("TELEGRAM_BOT_TOKEN is not set")
      if not TELEGRAM_CHAT_ID:
          errors.append("TELEGRAM_CHAT_ID is not set")
      if SCHEDULE_SOURCE_OFFSET_MIN < 0:
          errors.append("SCHEDULE_SOURCE_OFFSET_MIN must be >= 0")
      if MAX_JOBS_PER_RUN < 1:
          errors.append("MAX_JOBS_PER_RUN must be >= 1")
      if errors:
          sys.exit("Config errors:\n" + "\n".join(f"  - {e}" for e in errors))
  ```

- [ ] **0.3** In `hunter.py` `main()`: replace `_check_config()` with
  `from hunter.config import validate_config; validate_config()`, then delete `_check_config()`.

### Acceptance criteria
- `python hunter.py` with no `.env` prints a clear error list and exits 1
- All bool flags accept `"true"`, `"1"`, `"yes"` (case-insensitive)
- `pytest tests/ -x` passes

---

## Phase 1 — Quick Wins

Four independent tasks; each can be committed separately.

### 1.1 — Source import guard

**File:** `hunter/sources/__init__.py` (lines 28–90)

- [ ] Wrap all 16 conditional source imports in `try/except`:
  ```python
  if config.LINKEDIN_ENABLED:
      try:
          from .linkedin import LinkedInSource
          ALL_SOURCES.append(LinkedInSource())
      except Exception as exc:
          import logging
          logging.getLogger(__name__).warning("LinkedIn disabled (import error): %s", exc)
  ```
  Apply to: LinkedIn, Bulldogjob, Pracuj, TheProtocol, SolidJobs, Inhire, JobLeads,
  Arbeitnow, Remotive, RemoteOK, Himalayas, 4dayweek, WeWorkRemotely, RemoteLeaf,
  ATS Aggregator, Gmail.

### 1.2 — Domain matching fix

**File:** `job_fetch/__init__.py` (lines 44–144, 20 domain checks)

- [ ] Replace `"domain" in domain` substring checks with exact matches:
  ```python
  # Before:
  if "linkedin.com" in domain:
  # After:
  if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
  ```
  Recruitee already uses `endswith` — leave unchanged.
  Apply to all remaining 20 domain strings.

### 1.3 — Persistent `_pending_jobs`

**File:** `hunter/telegram_bot.py`

**Problem:** `_pending_jobs` (line 56) is in-memory → Apply/Skip buttons become dead after bot restart.

- [ ] Check `hunter/models.py` — confirm `Job` is a dataclass with JSON-serialisable fields
- [ ] Add constant: `PENDING_JOBS_FILE = PROJECT_DIR / "pending_jobs.json"`
- [ ] Add `_save_pending()` — serialise dict to JSON on disk
- [ ] Add `_load_pending() -> dict` — deserialise on startup; handle missing/corrupt file gracefully
- [ ] Call `_load_pending()` in `_post_init()` (startup hook)
- [ ] Call `_save_pending()` after every mutation:
  - `send_job_cards()` (after adding to dict)
  - `_handle_skip()` (after `.pop()`)
  - `_handle_apply()` (after `.pop()`)
- [ ] Add `pending_jobs.json` to `.gitignore`

### 1.4 — TrackerCache dedup in `hunter/main.py`

**Problem:** Lines 138–139 call `get_known_urls()` + `get_known_company_titles()` via
`asyncio.to_thread` — each re-opens the Excel file. `TrackerCache` already has
`is_known_url()` (tracker_cache.py:170) and `is_known_ct()` (tracker_cache.py:175).

- [ ] In `hunter/main.py`, replace the dedup block (lines ~136–171):
  ```python
  # Remove:
  known_urls = await asyncio.to_thread(get_known_urls)
  known_cts  = await asyncio.to_thread(get_known_company_titles)

  # Replace loop condition with:
  from hunter.tracker_cache import cache as tracker_cache
  if await tracker_cache.is_known_url(normalize_url(job.url)):
      continue
  if await tracker_cache.is_known_ct(job.company, job.title):
      continue
  ```
- [ ] Remove unused imports `get_known_urls`, `get_known_company_titles` from `hunter/main.py`

### 1.5 — `/stats` command

**File:** `hunter/telegram_bot.py`

- [ ] Add `async def cmd_stats(update, context)`:
  - Call `read_all_tracker_rows()` (tracker.py:1005) via `asyncio.to_thread`
  - Filter rows to last 30 days by Date column
  - Count by `ats_pct` field: numeric → APPLIED, SKIP, REACT, EXPIRED, FAIL
  - For APPLIED rows: compute average ATS %
  - Build and send formatted message:
    ```
    📊 Job Hunt Stats (last 30 days)
      Processed:   143
      Applied:      18  (ATS avg: 91%)
      Skipped:      12
      React-only:    8
      Expired:       6
      Failed:        3

    Top companies: Allegro (2), Revolut (1), N26 (1)
    ```
- [ ] Register in `build_application()`: `CommandHandler("stats", cmd_stats)`
- [ ] Add `/stats` to `/start` help text

### 1.6 — Structured logging

**Files:** `requirements.txt`, `hunter/config.py`, new `hunter/logging_setup.py`, `hunter.py`

- [ ] Add `python-json-logger` to `requirements.txt`
- [ ] Add to `config.py`: `LOG_FORMAT: str = os.getenv("LOG_FORMAT", "text")`
- [ ] Create `hunter/logging_setup.py`:
  ```python
  import logging
  from hunter.config import LOG_FORMAT

  def setup_logging() -> None:
      if LOG_FORMAT == "json":
          from pythonjsonlogger import jsonlogger
          handler = logging.StreamHandler()
          handler.setFormatter(jsonlogger.JsonFormatter(
              "%(asctime)s %(name)s %(levelname)s %(message)s"
          ))
          logging.root.handlers = [handler]
      logging.basicConfig(level=logging.INFO)
  ```
- [ ] Call `setup_logging()` at the top of `hunter.py` `main()`, before `validate_config()`

---

## Phase 2 — Split `apply_agent.py`

**Goal:** Break the 1 434-line monolith into testable units.  
**Risk:** Medium — imports change, subprocess calls intact. Run full suite after every file extraction.

### Target structure
```
apply_agent.py              < 80 lines — args + main() dispatch only
hunter/notify.py            notify() + send_telegram_documents()
hunter/apply_shared.py      cover_letter review loop + ATS check loop + shared helpers
hunter/apply_api.py         main_api() — API pipeline
hunter/apply_cli.py         main_cli() + _find_new_folder() — CLI pipeline
```

### Migration map

| Extract | From lines | To |
|---------|------------|-----|
| `notify()` | 107–122 | `hunter/notify.py` |
| `send_telegram_documents()` | 139–183 | `hunter/notify.py` |
| `_review_cover_letter()` | 326–441 | `hunter/apply_shared.py` |
| `_cover_letter_review_loop()` | 582–610 | `hunter/apply_shared.py` |
| `_ats_check_loop()` | 503–579 | `hunter/apply_shared.py` |
| Shared helpers (build_prompts, parse_content_json, etc.) | 579–733 | `hunter/apply_shared.py` |
| `main_api()` | 735–1015 | `hunter/apply_api.py` |
| `_find_new_folder()` + CLI utils | 1016–1082 | `hunter/apply_cli.py` |
| `main_cli()` | 1084–1283 | `hunter/apply_cli.py` |
| `main()` + `parse_apply_cli_argv()` | 1312–1434 | `apply_agent.py` (kept) |

### Tasks
- [ ] **2.1** Create `hunter/notify.py`; move `notify()` + `send_telegram_documents()`; update imports in `apply_agent.py`; run `python -m compileall .`
- [ ] **2.2** Create `hunter/apply_shared.py`; move shared functions + review loops; run `pytest tests/ -x`
- [ ] **2.3** Create `hunter/apply_api.py`; move `main_api()`; import from `apply_shared` + `notify`; run `pytest tests/ -x`
- [ ] **2.4** Create `hunter/apply_cli.py`; move `main_cli()` + folder helpers; run `pytest tests/ -x`
- [ ] **2.5** Reduce `apply_agent.py` to entry point only (< 80 lines)
- [ ] **2.6** Add `tests/test_apply_shared.py` — test each of the 7 cover letter quality gates and ATS loop with mocked LLM responses (at minimum: word count gate, opener ban gate, numeric metric gate, ATS threshold trigger)

---

## Phase 3 — Split `telegram_bot.py` + Healthcheck

**Goal:** Break the 1 443-line monolith into command-domain modules; add healthcheck.  
**Risk:** Medium — all handlers refactored, shared state must be imported correctly.

### Target structure
```
hunter/telegram_bot.py          send_job_cards(), send_job_card(), _make_keyboard() — pure send API
hunter/app.py                   build_application() + schedule setup
hunter/commands/__init__.py     (empty)
hunter/commands/hunt.py         cmd_hunt, cmd_force, cmd_process_manual
hunter/commands/status.py       cmd_start, cmd_status, cmd_schedule, cmd_unsent, cmd_stats
hunter/commands/tracker_cmd.py  cmd_sync_sent, cmd_check_expired
hunter/commands/google.py       cmd_gsheets_status, cmd_gsheets_resync,
                                cmd_gsheets_push_missing, cmd_gdrive_upload_missing
hunter/commands/apply_cb.py     button_callback, _handle_apply, _handle_skip, url_message_handler
```

### Shared state rule
`_pending_jobs` and `_save_pending` stay in `hunter/telegram_bot.py`.
`apply_cb.py` imports them:
```python
from hunter.telegram_bot import _pending_jobs, _save_pending
```

### Tasks
- [ ] **3.1** Create `hunter/commands/__init__.py` (empty)
- [ ] **3.2** Extract `commands/status.py` (lowest risk); verify bot starts; run `pytest tests/ -x`
- [ ] **3.3** Extract `commands/tracker_cmd.py`; verify; run tests
- [ ] **3.4** Extract `commands/google.py`; verify; run tests
- [ ] **3.5** Extract `commands/hunt.py`; verify; run tests
- [ ] **3.6** Extract `commands/apply_cb.py` (highest risk — touches `_pending_jobs`); verify buttons work; run tests
- [ ] **3.7** Create `hunter/app.py`; move `build_application()` + schedule setup; update `hunter.py` import
- [ ] **3.8** Reduce `hunter/telegram_bot.py` to send API only (< 100 lines)

### Healthcheck (add in 3.7 alongside `app.py`)
- [ ] **3.9** Add `HEALTHCHECK_PORT: int = int(os.getenv("HEALTHCHECK_PORT", "0"))` to `config.py`
- [ ] **3.10** Add `start_healthcheck(port)` in `hunter/app.py` using `aiohttp.web`:
  ```python
  GET /healthz → {"status": "ok", "uptime_sec": N, "last_hunt": {...}}
  ```
- [ ] **3.11** Start in `_post_init()` when `HEALTHCHECK_PORT > 0`
- [ ] **3.12** Add to `docker-compose.yml`: `healthcheck: test: curl -sf http://localhost:8080/healthz`

---

## Phase 4 — SQLite Tracker

**Goal:** Replace Excel writes with SQLite. Eliminate PermissionError; enable fast indexed queries.  
**Risk:** High-impact structural change — full test suite + manual smoke required at each step.

### Schema (`hunter/db.py`)
```sql
CREATE TABLE IF NOT EXISTS jobs (
    id       TEXT PRIMARY KEY,
    date     TEXT,
    company  TEXT,
    title    TEXT,
    stack    TEXT,
    ats_pct  TEXT,
    url      TEXT UNIQUE,
    folder   TEXT,
    sent     TEXT,
    reapply  TEXT,
    to_learn TEXT,
    norm_url TEXT,
    ct_key   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_norm_url ON jobs(norm_url);
CREATE INDEX IF NOT EXISTS idx_jobs_ct_key   ON jobs(ct_key);
CREATE INDEX IF NOT EXISTS idx_jobs_date     ON jobs(date);
```

### Tasks
- [ ] **4.1** Create `hunter/db.py`: connection (sqlite3 + `threading.Lock`), `init_db()`, `migrate_from_excel()`
- [ ] **4.2** `migrate_from_excel()`: reads `tracker.xlsx` via `read_all_tracker_rows()`, inserts idempotently (skip existing IDs), logs row count
- [ ] **4.3** Replace write functions first (`add_applied`, `add_skipped`, `add_failed`, `add_react_skipped`) to write to both SQLite and Excel (dual-write mode for safety)
- [ ] **4.4** Replace read functions (`get_known_urls`, `get_failed_jobs`, `read_all_tracker_rows`, `lookup_url`) to query SQLite
- [ ] **4.5** Once dual-write stable for 1 week: drop Excel write path; keep `tracker.xlsx` as read-only backup
- [ ] **4.6** Remove `TrackerCache.is_known_url()` / `is_known_ct()` dedup methods (SQLite indexed lookup replaces them)
- [ ] **4.7** Add `/export` Telegram command → generates `tracker_export.xlsx` from SQLite via `openpyxl`; sends as Telegram document
- [ ] **4.8** Add `python -m hunter.db migrate` CLI entry point
- [ ] **4.9** Add `tests/test_db.py` — test init, migrate, CRUD, dedup queries, concurrent writes

---

## Phase 5 — Packaging & Dependency Audit

**Goal:** Clean entry point, mypy baseline, no dead dependencies.

### Tasks
- [ ] **5.1** Create `pyproject.toml`:
  ```toml
  [project]
  name = "job-hunter"
  version = "0.1.0"
  requires-python = ">=3.11"

  [project.scripts]
  job-hunter = "hunter.__main__:main"

  [tool.mypy]
  python_version = "3.11"
  ignore_missing_imports = true
  ```
- [ ] **5.2** Create `hunter/__main__.py`:
  ```python
  from hunter.app import build_application
  from hunter.config import validate_config

  def main():
      validate_config()
      app = build_application()
      app.run_polling(drop_pending_updates=True)
  ```
- [ ] **5.3** Replace `pytz` with `zoneinfo` (stdlib): find all `pytz.timezone(...)` usages, replace with `zoneinfo.ZoneInfo(...)`, remove `pytz` from `requirements.txt`
- [ ] **5.4** Confirm `scikit-learn` is used in ATS scorer — if yes, keep; otherwise remove from `requirements.txt`
- [ ] **5.5** Run `pip-audit` on pinned Google API packages; upgrade if CVEs found
- [ ] **5.6** Run `mypy hunter/config.py hunter/models.py` — fix any type errors found

---

## Verification Checklist (per phase)

Run after every phase before committing:

```bash
python -m compileall .           # no syntax errors
pytest tests/ -x                 # full suite passes (373 baseline tests)
python hunter.py &               # bot starts without errors
# Send /status, /stats, /hunt to bot in Telegram
kill %1
```

Phase 4 additional:
```bash
python -m hunter.db migrate      # row count matches tracker.xlsx
python hunter.py &               # bot starts and reads from SQLite
```

---

## Work Log

| Date | Agent | Phase | Notes |
|------|-------|-------|-------|
| 2026-05-18 | sonnet | — | Plan created from PROJECT_ANALYSIS_2026_05.md |
