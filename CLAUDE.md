# CLAUDE.md — Project Context for AI Agents

This file is the single source of truth for any agent working on this codebase.
Read it fully before making changes. Update it when you learn something new.

---

## What This Project Is

**Job Hunter Bot** — an autonomous system that:
1. Scrapes 17 Polish/European/global IT job boards for Senior Frontend (Angular) vacancies
2. Filters by location, seniority, stack, language requirements
3. Deduplicates against tracker.xlsx (URL + company+title)
4. Sends new jobs to Telegram for review (Apply/Skip buttons)
5. On approval (or automatically), generates a tailored CV + cover letter via LLM
6. Tracks everything in `tracker.xlsx`, mirrors live to Google Sheets
7. Uploads application docs to Google Drive; sends folder link via Telegram

**Owner:** Ihar Petrasheuski, Senior Frontend Developer, Angular, 10+ years. Wroclaw, Poland. Seeking Angular/React/JS roles, remote or hybrid-Wroclaw.

**Tech stack:** Python 3.11+, python-telegram-bot (async), Anthropic/OpenAI API, openpyxl, python-docx, LibreOffice headless, requests, cloudscraper, Playwright (optional).

---

## Architecture Overview

```
hunter.py                   Entry point. Validates config, builds Telegram app, starts polling.
                            |
                            v
hunter/telegram_bot.py      Telegram Application (~1380 lines).
                            Handlers: /start /hunt /force /status /schedule /unsent
                              /sync_sent /process_manual /check_expired
                              /gsheets_status /gsheets_resync
                            URL messages, paste flow, Apply/Skip callbacks.
                            Staggered JobQueue schedule per source.
                            LinkedIn batch processing.
                            |
                            v  run_hunt(context, source_names?)
hunter/main.py              Core hunt loop:
                            1. FETCH  -> sources/*.search() -> list[Job]
                            2. FILTER -> filters.apply_filters_with_stats()
                            3. DEDUP  -> tracker (URL + company+title)
                            4. ACT   -> AUTO_APPLY: apply_agent.py (subprocess)
                                         MANUAL:    Telegram cards with buttons
                            Also: _retry_failed(), to_send sync before each hunt.
                            |
         +------------------+--------------------+
         v                  v                    v
hunter/sources/        hunter/tracker.py     apply_agent.py (1297 lines)
  17 sources             tracker.xlsx r/w       |
  (see list below)       dedup logic         job_fetch/       -> fetch job text
                         SKIP/FAIL/MANUAL      22 fetchers
                         add_applied()         html_fallback.py
                                                |
                                                v
hunter/services/                             llm_client.py   -> call LLM API
  apply_service.py      subprocess wrapper      |
  tracker_service.py    high-level tracker      v
                                             generate_docs.py -> DOCX/PDF + tracker
                                                |
                                                v
hunter/gsheets_sync.py  mirror_new_row()  -> Google Sheets (best-effort)
hunter/gsheets_client.py                     Sheets API v4 wrapper
hunter/gdrive_sync.py   upload_application_folder() -> Google Drive (best-effort)
hunter/gdrive_client.py                      Drive API v3 wrapper
hunter/tracker_cache.py                      In-memory cache (asyncio.Lock)
                                             dedup, stats, conflict matrix
```

### Data Flow

```
Job Boards --scrape--> list[Job] --filter--> list[Job] --dedup--> list[Job] (new)
  --> apply_agent.py:
        job_fetch.fetch_job_text(url)      # full job posting text
        expired_check.is_job_expired()     # skip if offer expired
        llm_client.call_llm()              # -> JSON (resume, cover letter, about me)
        cover letter self-review loop      # up to 3 LLM rounds
        generate_docs.py(content.json)     # -> DOCX + PDF + tracker.xlsx row
  --> Telegram notification + PDF/DOCX files
```

### Schedule

Base times: 08:00, 13:00, 19:00 (Europe/Warsaw).
Each source offset by `SCHEDULE_SOURCE_OFFSET_MIN` (default 40 min).
With 17 sources, a full cycle spans ~11 hours from the base time.

---

## Job Sources (17 active)

| Source | Module | Strategy | Notes |
|--------|--------|----------|-------|
| JustJoin.it | justjoin.py | SSR HTML slugs + JSON detail API | Polish market leader |
| NoFluffJobs | nofluffjobs.py | POST JSON search API | No auth |
| LinkedIn | linkedin.py | Guest HTML search API | 2 pages x 25 per keyword |
| Bulldogjob | bulldogjob.py | `__NEXT_DATA__` JSON | |
| Pracuj.pl | pracuj.py | cloudscraper + `__NEXT_DATA__` | Cloudflare-protected |
| theprotocol.it | theprotocol.py | cloudscraper + dehydratedState | Cloudflare-protected |
| SolidJobs | solidjobs.py | RSS feed | |
| Arbeitnow | arbeitnow.py | JSON API | EU/remote |
| Remotive | remotive.py | JSON API | Remote only |
| RemoteOK | remoteok.py | JSON API | Remote only |
| Himalayas | himalayas.py | JSON API | Remote only |
| 4dayweek.io | fourdayweek.py | JSON API v2 | |
| WeWorkRemotely | weworkremotely.py | RSS feed | |
| RemoteLeaf | remoteleaf.py | HTML listing parser | Paginated |
| Inhire.io | inhire.py | Playwright + Vuex store | Requires Playwright |
| JobLeads | jobleads.py | HTML scraper | Cloudflare issues; MANUAL flow |
| ATS Aggregator | ats_aggregator.py | Per-company ATS APIs | Workable/Greenhouse/Lever/Recruitee/Ashby |
| Gmail | gmail.py | Gmail API email alerts | Parses LinkedIn/NoFluff/JustJoin/Pracuj alerts |

---

## Repository Layout

```
apply_agent.py              Core apply pipeline: fetch job -> LLM -> content.json -> generate docs
generate_docs.py            DOCX/PDF generation from content.json (python-docx + LibreOffice)
hunter.py                   Entry point: starts Telegram bot + scheduler
llm_client.py               LLM wrapper: Anthropic + OpenAI with retry + JSON parsing

hunter/
  config.py                 ALL config: env vars, filters, schedule, paths, source toggles
  models.py                 Job dataclass
  filters.py                Central filter: keywords, level, location, patterns, React-only, German
  main.py                   Hunt loop: fetch -> filter -> dedup -> act
  telegram_bot.py           Telegram bot: all handlers, schedule, callbacks (1266 lines)
  tracker.py                tracker.xlsx CRUD: dedup, skip, fail, applied, manual (~980 lines)
  tracker_cache.py          In-memory tracker cache (asyncio.Lock, O(1) dedup + stats)
  tracker_backup.py         Timestamped daily snapshots of tracker.xlsx
  expired_check.py          Expired job detection (regex patterns)
  expired_marker.py         Parallel expired check for unsent rows; writes EXPIRED to tracker
  gsheets_sync.py           High-level Sheets mirror (push/pull/resync/bootstrap)
  gsheets_client.py         Low-level Sheets API v4 wrapper
  gdrive_sync.py            High-level Drive upload (upload_application_folder)
  gdrive_client.py          Low-level Drive API v3 wrapper
  gmail_client.py           Gmail API wrapper
  gmail_parsers.py          Parse job alert emails from various boards
  services/
    apply_service.py        Subprocess wrapper for apply_agent + generate_docs cmd builder
    tracker_service.py      High-level: should_skip_url(), record_successful_apply()
  sources/                  17 scrapers (see table above)
    base.py                 BaseSource ABC: search() -> list[Job]
    __init__.py             ALL_SOURCES registry (conditional imports by ENABLED flags)
  ats/                      ATS provider adapters
    base.py                 ATSProvider ABC: fetch(slug, company_name) -> list[Job]
    workable.py / greenhouse.py / lever.py / recruitee.py / ashby.py
  ats_companies.json        Company list for ATS aggregator

job_fetch/                  Per-site detail text fetchers (22 files)
  __init__.py               Dispatcher: domain -> fetcher
  html_fallback.py          Generic HTML -> text (BeautifulSoup)
  ats_workable.py / ats_greenhouse.py / ...  ATS detail fetchers

prompts/
  system_prompt.md          LLM instructions for resume/CL generation
  candidate_profile.md      Candidate data (single source of truth for personal info)

tests/                      37+ test files, ~3200 lines (pytest)
tools/                      Utilities: backup, dedup, gmail auth, gsheets auth, LinkedIn login

tracker.xlsx                Main data store (never commit)
gsheets_state.json          Active spreadsheet ID (auto-generated; mount in Docker)
gsheets_credentials.json    OAuth2 client secrets (never commit)
gsheets_token.json          OAuth2 token (never commit; auto-refreshed)
backups/                    Daily snapshots (gitignored)
Applications/               Generated documents (gitignored)
```

---

## Key Configuration (`hunter/config.py` + `.env`)

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHAT_ID` | — | Required |
| `AUTO_APPLY` | `false` | Auto-generate docs without manual button press |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `LLM_MODEL` | `claude-3-5-haiku-20241022` | Model for API mode |
| `LLM_API_KEY` | — | API key for LLM provider |
| `APPLY_USE_CLI` | `false` | Use Claude CLI (Pro subscription) instead of API |
| `MAX_JOBS_PER_RUN` | `10` | Cap per hunt cycle |
| `APPLY_DELAY_SEC` | `30` | Pause between auto-apply jobs |
| `APPLY_AGENT_TIMEOUT_SEC` | `900` | Subprocess timeout (15 min) |
| `TELEGRAM_SEND_DOCS` | `true` | Send PDF/DOCX via Telegram after apply |
| `TRACKER_BACKUP_ENABLED` | `true` | Daily backups via JobQueue |
| `GSHEETS_ENABLED` | `false` | Enable Google Sheets mirror |
| `GSHEETS_TRACKER_ID` | — | Spreadsheet ID (set after first run or auto-created) |
| `GSHEETS_REFRESH_INTERVAL_MIN` | `30` | Sheets → Excel pull interval |
| `GDRIVE_ENABLED` | `false` | Upload application docs to Google Drive after apply |
| `GDRIVE_ROOT_FOLDER_ID` | — | Optional: existing Drive folder ID (auto-creates "Job Hunter" if empty) |
| `GDRIVE_ROOT_FOLDER_NAME` | `Job Hunter` | Name of auto-created root folder on Drive |

Source toggles (all default `true` except `GMAIL_ENABLED=false`):
`LINKEDIN_ENABLED`, `BULLDOGJOB_ENABLED`, `PRACUJ_ENABLED`, `THEPROTOCOL_ENABLED`,
`SOLIDJOBS_ENABLED`, `INHIRE_ENABLED`, `JOBLEADS_ENABLED`, `ARBEITNOW_ENABLED`,
`REMOTIVE_ENABLED`, `REMOTEOK_ENABLED`, `HIMALAYAS_ENABLED`, `FOURDAYWEEK_ENABLED`,
`WEWORKREMOTELY_ENABLED`, `REMOTELEAF_ENABLED`, `ATS_AGGREGATOR_ENABLED`, `GMAIL_ENABLED`.

---

## Pipeline Flow

### Hunt cycle (`hunter/main.py`)
1. Each source calls `source.search()` -> `list[Job]`
3. `filters.apply_filters_with_stats()` — keywords, level, location, patterns, React-only, German language
4. Dedup: URL (`normalize_url`) + company+title key (`dedup_key`)
5. New jobs -> Telegram cards with Apply/Skip buttons
6. If `AUTO_APPLY=true` -> auto-apply pipeline + retry FAILed jobs

### Apply pipeline (`apply_agent.py`)
1. `job_fetch.fetch_job_text(url)` — fetch full job description
2. Save `job_posting.txt` to output folder
3. `expired_check.is_job_expired(text)` — skip if expired
4. LLM call: `candidate_profile.md` + `system_prompt.md` + job text -> `content.json`
5. Cover letter self-review loop (up to 3 LLM rounds)
6. Output folder: `Applications/{today}/{CompanyName}/`
7. `generate_docs.py` -> DOCX + PDF (LibreOffice headless)
8. `tracker_service.record_successful_apply()` -> tracker.xlsx row
9. `gsheets_sync.mirror_new_row()` -> Google Sheets (best-effort)
10. Telegram notification + file upload

### Doc generation modes
- **Short** (default): PDF only, EN CV only (3 files)
- **Full** (`--full`): DOCX + PDF, EN + PL CV, About_Me .txt (10 files)
- **Force** (`--force`): skip dedup, bypass React-only skip

---

## tracker.xlsx Schema

| Col | Name | Description |
|-----|------|-------------|
| 1 | Date | Application date |
| 2 | Company | Company name |
| 3 | Job Title | Position title |
| 4 | Stack | Tech stack (from LLM) |
| 5 | ATS % | Match score, or: SKIP / FAIL / MANUAL / EXPIRED / — |
| 6 | URL | Canonical job URL (dedup key) |
| 7 | Folder | Path to Applications/ subfolder |
| 8 | Sent | Date sent, or blank/dash |
| 9 | Re-application | `+` flag |
| 10 | To Learn | Skills gap |
| 11 | ID | Short UUID (8-char hex) — Google Sheets sync key |

**Column index constants** in `hunter/tracker.py` — update both code and this doc if schema changes.

---

## Google Sheets — Sending Workflow

Replaces `to_send.xlsx`. tracker.xlsx rows are mirrored live to a Google Sheets spreadsheet.

### Setup (one-time)
1. `python tools/gsheets_auth.py` — OAuth2 consent → writes `gsheets_token.json`
2. Set `GSHEETS_ENABLED=true` in `.env`
3. On first bot start: spreadsheet created automatically; bot sends you the URL + ID

### Runtime flow
1. Successful apply / skip → `gsheets_sync.mirror_new_row(row)` appends to Sheets
2. EXPIRED stamp → `gsheets_sync.mirror_expired_batch()` updates Sent column
3. User edits Sent date / To Learn / Re-application in Sheets
4. `/sync_sent` → `pull_full_snapshot()` → conflict matrix → tracker.xlsx updated
5. Automatic pull every `GSHEETS_REFRESH_INTERVAL_MIN` (default 30 min)
6. `/unsent` shows count from in-memory cache (O(1), no Excel read)
7. `/gsheets_status` — integration health; `/gsheets_resync` — push dirty rows

### Conflict matrix (Sent column)
- Bot wrote EXPIRED, Sheets is empty → keep EXPIRED (Sheets will be fixed by resync)
- Sheets has date / was edited → trust Sheets
- To Learn, Re-application → always trust Sheets (user edits there)

---

## Adding a New Job Source

See `.claude/commands/add-source.md` for full guide.

1. `hunter/sources/yoursite.py` — subclass `BaseSource`, implement `search() -> list[Job]`
2. `job_fetch/yoursite.py` — implement `fetch_yoursite(url) -> str`
3. `YOURSITE_ENABLED` toggle in `hunter/config.py`
4. Register in `hunter/sources/__init__.py` + `job_fetch/__init__.py`

---

## Google Sheets Setup (one-time per deployment)

```bash
# 1. Get OAuth2 credentials from Google Cloud Console
#    API & Services → Credentials → Create OAuth2 client (Desktop app)
#    Download JSON → save as gsheets_credentials.json in project root

# 2. Run OAuth flow (opens browser for consent)
python tools/gsheets_auth.py
# → writes gsheets_token.json

# 3. Enable in .env
GSHEETS_ENABLED=true

# 4. Start bot — spreadsheet is created automatically on first run
#    Bot sends you a Telegram message with the URL and .env snippet
#    Copy GSHEETS_TRACKER_ID=... to .env (optional — state file takes over after first run)

# Docker: mount gsheets_state.json so sheet_id survives container restarts
# (see docker-compose.yml)
```

## Git Workflow

- **Active branch:** `develop` — all changes go here
- `master` is production-stable (60+ commits behind develop)
- Always commit on `develop`, never force-push `master`

---

## Important Rules for Agents

- **Never commit** `.env`, `tracker.xlsx`, `Applications/`, `backups/`, `gmail_token.json`, `gsheets_token.json`, `gsheets_credentials.json`
- Always test syntax after edits: `python -m compileall .`
- Run `pytest tests/` after changes to tracker, filters, or sources
- Column index constants in `tracker.py` are hardcoded — update carefully
- Candidate profile single source of truth: `prompts/candidate_profile.md`
- LibreOffice path: `C:/Program Files/LibreOffice/program/soffice.exe` (in `generate_docs.py`)
- When changing tracker schema, bot behavior, or adding files — update CLAUDE.md in the same commit

---

## Known Issues and Technical Debt

### Structural

1. **telegram_bot.py is a ~1380-line monolith.** Contains 17+ handlers, build_application, schedule setup, LinkedIn batch, paste flow, expired check flow, force logic. Hard to navigate and test.

2. **job_fetch/ is a separate parallel package (22 files, 2475 lines).** Every site has a file in both `hunter/sources/` (search/listing) and `job_fetch/` (detail text fetch). URLs, headers, and domain knowledge are duplicated across packages.

3. **apply_agent.py is 1297 lines.** Contains two full pipelines (API + CLI mode), Telegram notification, folder management, LLM calling, cover letter review loop, paste flow, force mode, JobLeads MANUAL flow. Could be split.

### Code Quality

4. **No pyproject.toml / setup.py.** Project can't be installed as a package. No mypy/pyright config.

5. **Filters are 293 lines** with complex German-language detection regex spanning 40+ patterns. Works but hard to maintain.

6. **tracker.py is ~980 lines.** Multiple functions re-open and re-parse the entire Excel file per call. The in-memory `tracker_cache` solves dedup/stats O(1) but individual write functions still re-open the workbook.

---

## Refactoring Plan

### Phase 1 — Cleanup (LOW risk, immediate value)

- [x] **1.1** Delete stale docs: `PLAN.md`, `HUNTER_PLAN.md`, `EXPIRED_PLAN.md`, `PROJECT_REVIEW_AND_REFACTOR_PLAN.md`, `WEBSITE_PLAN.md`
- [x] **1.2** Delete debug artifacts: `_probe*.py`, `tracker_broken.xlsx`
- [x] **1.3** Add `__pycache__/` and `*.pyc` to `.gitignore`, remove tracked `__pycache__` dirs (was already done)
- [x] **1.4** Unify `_run_apply_agent` in `telegram_bot.py` to use `services/apply_service.py`

### Phase 2 — Split telegram_bot.py (MEDIUM risk)

- [ ] **2.1** Extract command handlers into `hunter/commands/` module (hunt, force, status, expired, etc.)
- [ ] **2.2** Extract `build_application()` + schedule setup into `hunter/app.py`
- [ ] **2.3** Keep `telegram_bot.py` as thin dispatcher + send_text/send_job_cards API

### Phase 3 — Merge job_fetch/ into sources/ (MEDIUM risk)

- [ ] **3.1** Add `fetch_text(url) -> str` method to `BaseSource` ABC
- [ ] **3.2** Move `job_fetch/*.py` logic into corresponding `hunter/sources/*.py`
- [ ] **3.3** Update `apply_agent.py` to call `source.fetch_text(url)` instead of `job_fetch.fetch_job_text()`
- [ ] **3.4** Delete `job_fetch/` package

### Phase 4 — Split apply_agent.py (MEDIUM risk)

- [ ] **4.1** Extract API pipeline into `hunter/apply_api.py`
- [ ] **4.2** Extract CLI pipeline into `hunter/apply_cli.py`
- [ ] **4.3** Make apply callable as import (not just subprocess)
- [ ] **4.4** Keep `apply_agent.py` as thin CLI entry point

### Phase 5 — SQLite tracker (HIGH impact, MEDIUM risk)

- [ ] **5.1** Create `hunter/db.py` with SQLite schema
- [ ] **5.2** Migrate tracker functions to SQLite (atomic writes, no PermissionError)
- [ ] **5.3** Add `/export` command for Excel export
- [ ] **5.4** Keep openpyxl only for doc generation formatting
- [ ] **5.5** gsheets_sync: replace tracker_cache with db queries

### Phase 6 — Project structure (after phases 1-5)

- [ ] **6.1** Add `pyproject.toml` with metadata and mypy config
- [ ] **6.2** Make project installable (`pip install -e .`)
- [ ] **6.3** Entry point: `python -m hunter` instead of `python hunter.py`

---

## Scraper Health Notes

> Agents: update this section when you verify or fix a scraper.

| Source | Last verified | Status | Notes |
|--------|--------------|--------|-------|
| JustJoin.it | 2026-04 | OK | SSR HTML + `/api/candidate-api/offers/{slug}` |
| NoFluffJobs | 2026-04 | OK | POST `/api/search/posting` |
| LinkedIn | 2026-04 | OK | Guest HTML search API |
| Bulldogjob | 2026-04 | OK | `__NEXT_DATA__` JSON |
| Pracuj.pl | 2026-04 | OK | cloudscraper + `__NEXT_DATA__` |
| theprotocol.it | 2026-04 | OK | cloudscraper + dehydratedState |
| SolidJobs | 2026-04 | OK | RSS feed |
| Arbeitnow | 2026-04 | OK | JSON API |
| Remotive | 2026-04 | OK | JSON API |
| RemoteOK | 2026-04 | OK | JSON API |
| Himalayas | 2026-04 | OK | JSON API |
| 4dayweek.io | 2026-04 | OK | JSON API v2 |
| WeWorkRemotely | 2026-04 | OK | RSS feed |
| RemoteLeaf | 2026-04 | OK | HTML listing |
| Inhire.io | 2026-04 | OK | Playwright + Vuex |
| JobLeads | 2026-04 | PARTIAL | Detail pages Cloudflare-blocked; MANUAL flow |
| ATS Aggregator | 2026-04 | OK | Workable/Greenhouse/Lever/Recruitee/Ashby |
| Gmail | 2026-05 | OK | Gmail API alerts |

---

## Previously Completed Refactoring

These items from `PROJECT_REVIEW_AND_REFACTOR_PLAN.md` are done:

- **TASK-01 (P0):** Subprocess timeout/kill — `asyncio.wait_for` + `proc.kill()` in `apply_service.py`
- **TASK-02 (P0):** Tracker writes centralized — `generate_docs.py` delegates to `tracker_service.record_successful_apply()`
- **TASK-03 (P1):** Hardcoded paths removed — all paths from `hunter.config`
- **TASK-04 (P1):** Config unified — `apply_agent.py` imports from `hunter.config`
- **TASK-05 (P2):** Tests added — 35 test files covering filters, tracker, sources, LLM parsing
- **Extra:** ATS 10-point scale interpretation, robust JSON parsing, status normalization, service layer

---

## Agent Work Log

> Agents: append a dated entry here after completing significant work.
> Format: `YYYY-MM-DD | agent | what was done`

| Date | Agent | Work |
|------|-------|------|
| 2026-04-16 | agent | P0-P2 refactoring tasks completed (timeout, tracker centralization, config unification, tests) |
| 2026-04-16 | agent | Source contract tests, prefilter helper, tracker status normalization |
| 2026-05-11 | agent | Tracker backups, Gmail source, hunt/apply hardening |
| 2026-05-13 | opus | Full develop-branch analysis, CLAUDE.md rewritten with current architecture + refactoring plan |
| 2026-05-13 | opus | Phase 1 complete: 1.1 stale docs removed (7526acb), 1.2 debug artifacts deleted, 1.3 pre-done, 1.4 apply_service unified (265d87e) |
| 2026-05-13 | composer | to_send: detect LibreOffice Calc lock (`.~lock.*#`); skip rebuild when editor holds file; Telegram/gitignore/docs aligned |
| 2026-05-14 | sonnet | Google Sheets integration complete (GSHEETS_PLAN.md, phases 1-7): gsheets_client, tracker_cache, drop to_send.xlsx (15 files), gsheets_sync (mirror/pull/resync/bootstrap), /gsheets_status /gsheets_resync commands, 5-min resync + 30-min pull schedules, state file for Docker restart safety, 51 new tests (351 total) |
| 2026-05-15 | sonnet | Google Drive upload (GDRIVE_PLAN.md): gdrive_client (Drive API v3 wrapper), gdrive_sync (lazy singleton, upload_application_folder), GDRIVE_* config, telegram_bot hook after apply (best-effort, 22 new tests, 373 total) |
