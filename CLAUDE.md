# CLAUDE.md — Project Context for AI Agents

This file is the single source of truth for any agent working on this codebase.
Read it fully before making changes. Update it when you learn something new.

---

## What This Project Is

**Job Hunter Bot** — an autonomous system that:
1. Scrapes 21 Polish/European/global IT job boards for Senior Frontend (Angular) vacancies
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
hunter/sources/        hunter/tracker.py     apply_agent.py (thin CLI entry)
  21 sources             tracker.db r/w         |
  (see list below)       dedup logic         apply_api / apply_cli -> run pipeline
                         SKIP/FAIL/MANUAL      apply_shared.py       (shared helpers)
                         add_applied()         sources.fetch_job_text() -> job text
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
With 21 sources, a full cycle spans ~12 hours from the base time.

---

## Job Sources (21 active)

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
| Working Nomads | workingnomads.py | Elasticsearch `/jobsapi/_search` | Remote, worldwide |
| Jobspresso | jobspresso.py | RSS feed (`?feed=job_feed`) | Remote; ~10 latest only |
| Built In | builtin.py | cloudscraper + BeautifulSoup DOM | US/remote tech; Cloudflare |
| JustRemote | justremote.py | JSON API (Heroku backend) | Remote; ~10 newest dev only |
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
  telegram_bot.py           Thin dispatcher shim (~200 lines): imports all handlers, owns _post_init + build_application
  tracker.py                tracker.db (SQLite) CRUD: dedup, skip, fail, applied, manual (~1250 lines)
  tracker_cache.py          In-memory tracker cache (asyncio.Lock, O(1) dedup + stats)
  tracker_backup.py         Timestamped daily snapshots of tracker.xlsx
  lang_guard.py             Language routing + contamination guard: detect_posting_language()
                            (PL/EN by token density) + Polish-in-English / English-in-Polish
                            detection (diacritics + lexicon + suffix + bilingual gloss). Feeds
                            the apply enforce-gate (enforce_language_separation in apply_shared)
  resume_sanitizer.py       Strip LLM artifacts/foreign-language leakage from generated resume text
  content_qa.py             Post-generation QA checks on content.json (warns on quality issues)
  expired_check.py          Expired job detection (regex patterns)
  expired_marker.py         Parallel expired check for unsent rows; writes EXPIRED to tracker
  rate_limiter.py           Per-domain async concurrency + delay limiter (DomainLimiter);
                            shared by expired_marker and gmail_enricher to avoid HTTP 429
  gsheets_sync.py           High-level Sheets mirror (push/pull/resync/bootstrap)
  gsheets_client.py         Low-level Sheets API v4 wrapper
  gdrive_sync.py            High-level Drive upload (upload_application_folder)
  gdrive_client.py          Low-level Drive API v3 wrapper
  gmail_client.py           Gmail API wrapper
  gmail_parsers.py          Parse job alert emails from various boards
  sent_parse.py             Parse the messy Sent column into a real date (parse_sent_date/classify)
  sent_normalizer.py        Build/write the clean "Applied Date" Sheets column L from Sent
  bot/
    state.py                Shared mutable state (_pending_jobs, _active_apply_urls, _force_waiting)
    keyboards.py            _make_keyboard() — InlineKeyboardMarkup factory
    notifications.py        send_text(), send_job_cards(), _tg_notify()
    paste.py                _looks_like_paste(), _extract_url(), URL_RE
    formatters.py           _build_schedule_text(), _format_check_responses_report(), _format_daily_summary()
    apply_runner.py         _run_apply_agent(), _run_linkedin_batch(), _handle_paste()
  commands/                 One file per Telegram command handler
    start.py                /start
    schedule.py             /schedule
    unsent.py               /unsent
    status.py               /status
    sync_sent.py            /sync_sent
    hunt.py                 /hunt + parse_hunt_source_args
    force.py                /force + _force_cleanup + _force_run
    process_manual.py       /process_manual
    about_me.py             /about_me
    check_expired.py        /check_expired
    debug_url.py            /debug_url
    gsheets.py              /gsheets_status + /gsheets_push_missing + /gsheets_push_sent
    gdrive.py               /gdrive_upload_missing
    check_responses.py      /check_responses
    normalize.py            /normalize — rebuild Sheets column L (Applied Date) from Sent
    url_message.py          URL/text message handler + button_callback + _handle_apply + _handle_skip
  schedules/                One file per JobQueue callback
    hunt.py                 scheduled_hunt
    check_expired.py        scheduled_check_expired
    tracker_backup.py       scheduled_tracker_backup
    gdrive.py               scheduled_gdrive_upload_missing
    gsheets.py              scheduled_gsheets_resync + scheduled_gsheets_pull
    pending_report.py       scheduled_pending_report
    email_responses.py      scheduled_check_email_responses
    daily_summary.py        scheduled_daily_summary
    normalize_sent.py       scheduled_normalize_sent (daily 00:20, refreshes Sheets column L)
    __init__.py             register(app, tz) — wires all callbacks into the Application
  services/
    apply_service.py        Subprocess wrapper for apply_agent + generate_docs cmd builder
    tracker_service.py      High-level: should_skip_url(), record_successful_apply()
  sources/                  21 scrapers (see table above) + per-site detail-page fetchers
    base.py                 BaseSource ABC: search() / matches_url() / fetch_text()
    __init__.py             ALL_SOURCES registry + fetch_job_text() URL dispatcher
    html_fallback.py        Generic HTML -> text fallback + clean_url() helper
    text_utils.py           Shared helpers: strip_html() (HTML fragment -> plain text),
                            REMOTE_ANY + ensure_remote_token() (guarantee a "remote" token
                            survives the central location whitelist). Used by the JSON/RSS
                            sources; each keeps its own _format_location wrapper that delegates.
  ats/                      ATS provider adapters
    base.py                 ATSProvider ABC: fetch(slug, company_name) -> list[Job]
    workable.py / greenhouse.py / lever.py / recruitee.py / ashby.py
  ats_companies.json        Company list for ATS aggregator

prompts/
  generation_rules.md           LLM instructions for resume/CL generation (was system_prompt.md)
  candidate_profile.md          Candidate data (single source of truth for personal info)
  base_cv_angular.md            Pre-polished bullets for Angular track
  base_cv_react.md              Pre-polished bullets for React / JS track
  base_cv_ai.md                 Pre-polished bullets for AI-first track
  base_cv_fullstack_angular_nest.md  Pre-polished bullets for Angular + NestJS track
  base_cv_fullstack_react_next.md    Pre-polished bullets for React + Next.js track
  examples/                     Cover letter examples, About Me texts, candidate CV DOCX

tests/                      37+ test files, ~3200 lines (pytest)
tests/fixtures/sample_jobs/ Real job postings per track (angular/react/ai/fullstack_*) for preview
tools/                      Utilities: backup, dedup, gmail auth, gsheets auth, LinkedIn login
tools/preview_apply.py      Run apply pipeline against sample fixtures via CLI subscription
tools/dedup_sheet.py        One-time cleanup of duplicate rows in the Sheets tracker (--apply to delete)
tools/normalize_sent.py     Write clean "Applied Date" into Sheets column L from Sent (--apply to write)
tools/stats_sheet.py        Read-only stats over the Sheets Sent column (--write-tab for a Stats tab)

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
| `APPLICATIONS_DIR` | `Applications/` | Output folder override (useful for preview/testing) |
| `CV_GDPR_CLAUSE` | `both` | GDPR/RODO consent clause at CV bottom: `both` (PL+EN), `pl` (PL CV only), `none` |
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
| `GMAIL_ENRICH_CONCURRENCY` | `5` | Global cap on parallel enrichment fetches (all hosts) |
| `GMAIL_ENRICH_DOMAIN_LIMIT` | `2` | Default per-host concurrent enrichment fetches |
| `GMAIL_ENRICH_DOMAIN_DELAY` | `0.0` | Default per-host delay (sec) between enrichment fetches |
| `PRACUJ_HOST_CONCURRENCY` | `2` | pracuj.pl per-host concurrency override (Cloudflare 429) |
| `PRACUJ_HOST_DELAY_SEC` | `1.0` | pracuj.pl per-host delay (sec) override |

Source toggles (all default `true` except `GMAIL_ENABLED=false`):
`LINKEDIN_ENABLED`, `BULLDOGJOB_ENABLED`, `PRACUJ_ENABLED`, `THEPROTOCOL_ENABLED`,
`SOLIDJOBS_ENABLED`, `INHIRE_ENABLED`, `JOBLEADS_ENABLED`, `ARBEITNOW_ENABLED`,
`REMOTIVE_ENABLED`, `WORKINGNOMADS_ENABLED`, `JOBSPRESSO_ENABLED`, `BUILTIN_ENABLED`,
`JUSTREMOTE_ENABLED`, `REMOTEOK_ENABLED`, `HIMALAYAS_ENABLED`, `FOURDAYWEEK_ENABLED`,
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
4. LLM call: `candidate_profile.md` + `generation_rules.md` + job text -> `content.json`
5. Cover letter self-review loop (up to 3 LLM rounds)
5b. **Language enforce-gate** (`apply_shared.enforce_language_separation`, runs in BOTH
   the API and CLI pipelines): after sanitize/compliance-scrub, scan every `_en` field for
   Polish and every `_pl` field for English prose (`hunter.lang_guard`). On contamination,
   repair by *translating from the clean opposite-language counterpart* (a Polish posting
   makes the ATS loop inject Polish into `resume_en`; the clean `resume_pl` is translated
   back to EN — no re-fabrication, role-count guarded, then up to 2 in-place cleanup passes).
   If strong Polish survives in an `_en` field, **block delivery** (no broken doc is sent:
   API → `sys.exit(0)`; CLI → delete generated docs + return). Posting language is detected
   deterministically (`detect_posting_language`) and written to `content["primary_lang"]` to
   drive delivery routing. The detector allowlists Polish **place names** (Wrocław, Kraków…)
   so the candidate's city is never mistaken for contamination. In the CLI pipeline the gate
   runs as a post-process: read the CLI-written `content.json` → enforce → rewrite +
   regenerate docs (or block).
6. Output folder: `Applications/{today}/{CompanyName}/`
7. `generate_docs.py` -> DOCX + PDF (LibreOffice headless)
8. `tracker_service.record_successful_apply()` -> tracker.xlsx row
9. `gsheets_sync.mirror_new_row()` -> Google Sheets (best-effort)
10. Telegram notification + file upload

### Doc generation modes
- **Short** (default): PDF only, EN CV — **plus the PL CV when the posting is Polish**
  (`content["primary_lang"] == "PL"`), so a Polish employer receives the clean Polish CV
- **Full** (`--full`): DOCX + PDF, EN + PL CV, About_Me .txt (10 files)
- **Force** (`--force`): skip dedup, bypass React-only skip

A GDPR/RODO consent clause is auto-appended as the **last body paragraph** of the CV
(small italic grey text, in the document body so ATS parsers read it — NOT a footer).
Static legal text in `generate_docs.py` (`GDPR_CLAUSE_PL` / `GDPR_CLAUSE_EN`), never
LLM-generated. PL CV gets the Polish clause, EN CV the English one. Controlled by
`CV_GDPR_CLAUSE` (`both` / `pl` / `none`). Do NOT add this to prompts/profile.

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
| 12 | Drive URL | Google Drive folder URL after upload (local-only, not synced to Sheets) |

**Column index constants** in `hunter/tracker.py` — update both code and this doc if schema changes.

---

## Google Sheets — Sending Workflow

Replaces `to_send.xlsx`. tracker.xlsx rows are mirrored live to a Google Sheets spreadsheet.

> **Sheet column L "Applied Date" (Sheet-only, not in tracker.db).** The bot syncs only
> columns A–K (`gsheets_client.COLUMNS`). The `Sent` column (H) doubles as a free-text
> scratchpad (dates *and* notes like "выгасла"/"повторка"). `hunter.sent_normalizer`
> parses a real application date out of `Sent` and writes it into the untouched column L,
> so a Stats tab can `COUNT`/`QUERY` clean dates. Refreshed daily (00:20) and on demand
> via `/normalize`. Never written by the normal A–K push/pull. Local `tracker.db` is not
> involved.

### Setup (one-time)
1. `python tools/gsheets_auth.py` — OAuth2 consent → writes `gsheets_token.json`
2. Set `GSHEETS_ENABLED=true` in `.env`
3. On first bot start: spreadsheet created automatically; bot sends you the URL + ID

### Runtime flow
1. Successful apply / skip → `gsheets_sync.mirror_new_row(row)` appends to Sheets
2. EXPIRED stamp → `gsheets_sync.mirror_expired_batch()` updates Sent column
3. User edits Sent date / To Learn / Re-application in Sheets
4. `/sync_sent` → `pull_full_snapshot()` → insert missing rows + conflict matrix → tracker.db updated
5. Automatic pull every `GSHEETS_REFRESH_INTERVAL_MIN` (default 30 min)
6. `/unsent` shows count from in-memory cache (O(1), no Excel read)
7. `/gsheets_status` — integration health; `/gsheets_resync` — push dirty rows

### Pull = insert + update + reconcile (dedup self-heal)
`pull_full_snapshot()` does three things, in order:
1. `tracker.insert_pulled_rows()` — inserts Sheet rows absent from `tracker.db`
   (matched by neither `ID` nor `url_norm`; blank-ID rows skipped). This self-heals
   dedup after a fresh/empty DB (container restart, broken volume mount) so the bot
   doesn't re-process live vacancies. Also runs once at startup in `_post_init`.
2. `_apply_pull_delta_db()` — conflict matrix for `Sent`/`To Learn`/`Re-application`
   on rows matched by `ID` (existing rows are never overwritten by the insert step).
3. `_reconcile_deleted_rows()` — rows that exist in `tracker.db` with a **blank Sent**
   but whose `ID` is gone from the Sheet (user/`dedup_sheet.py` deleted them) are
   stamped `Sent='EXPIRED'` via `tracker.mark_orphans_expired()` (clears `sheets_dirty`
   + stale `sheets_row`, keeps the row for dedup, never overwrites an existing Sent).
   **Safety:** (a) skipped if the Sheets read returns < `_RECONCILE_MIN_RATIO` (0.8) of
   the DB's ID-bearing rows, so a partial/failed read can't mass-EXPIRE live vacancies;
   (b) `mark_orphans_expired` only touches rows with `sheets_row IS NOT NULL` (i.e. that
   were *mirrored* before) — a row that was **never pushed** (e.g. Sheets token down at
   apply time, `sheets_row` still NULL) is absent from the Sheet because it was never
   mirrored, not because it was deleted, so it is left live. Without (b) a failed mirror
   looked identical to a user deletion and got falsely EXPIRED on the next pull.
   Closes the gap where deletions in Sheets never propagated to the DB (orphans
   polluted the `/unsent` count forever).

After a pull that changed anything (`updated`/`inserted`/`reconciled` > 0),
`scheduled_gsheets_pull` calls `cache.load_from_db()` so `/unsent`, `/status` and
dedup reflect the new state without a bot restart.

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
- Run `ruff check .` before committing — CI gates on it (config in `pyproject.toml`,
  scoped to `hunter/` + entry scripts; `tests/`/`tools/` excluded for now)
- Run `pytest tests/` after changes to tracker, filters, or sources
- Column index constants in `tracker.py` are hardcoded — update carefully
- Candidate profile single source of truth: `prompts/candidate_profile.md`
- LibreOffice path: `C:/Program Files/LibreOffice/program/soffice.exe` (in `generate_docs.py`)
- When changing tracker schema, bot behavior, or adding files — update CLAUDE.md in the same commit

---

## Known Issues and Technical Debt

### Structural

1. ~~**telegram_bot.py is a ~1380-line monolith.**~~ ✅ Resolved (Phase 1–7 refactor, 2026-05-26): split into `bot/` (6 modules), `commands/` (15 files), `schedules/` (9 files). `telegram_bot.py` is now a ~200-line import shim that re-exports everything for backward compat.

2. ~~**job_fetch/ is a separate parallel package (22 files, 2475 lines).**~~ ✅ Resolved (Phase 3 refactor, 2026-05-26): each source now owns its detail-page extraction (`matches_url` + `fetch_text` on `BaseSource`). `hunter.sources.fetch_job_text(url)` dispatches to the matching source. `job_fetch/` deleted.

3. **apply_agent.py is 1297 lines.** Contains two full pipelines (API + CLI mode), Telegram notification, folder management, LLM calling, cover letter review loop, paste flow, force mode, JobLeads MANUAL flow. Could be split.

### Infrastructure

4. ~~**Playwright not installed in Docker — Inhire source always returns [].**~~ ✅ Resolved: `playwright` is active in `requirements.txt` and the `Dockerfile` runs `playwright install chromium --with-deps` (adds ~500MB to image, ~seconds/page at runtime). Inhire is live (verified 2026-06-08: 25 jobs incl. Angular roles). **Ops note:** Inhire only works in prod once the deploy image is rebuilt with the current Dockerfile. Playwright does NOT unblock Wellfound — real headless Chromium still gets HTTP 403 (anti-bot needs a logged-in session + stealth; see `docs/new-sources/QUEUE-3-hard.md`).

### Code Quality

5. **No pyproject.toml / setup.py.** Project can't be installed as a package. No mypy/pyright config.

6. **Filters are 293 lines** with complex German-language detection regex spanning 40+ patterns. Works but hard to maintain.

7. **tracker.py is ~980 lines.** Multiple functions re-open and re-parse the entire Excel file per call. The in-memory `tracker_cache` solves dedup/stats O(1) but individual write functions still re-open the workbook.

---

## Refactoring Plan

### Phase 1 — Cleanup (LOW risk, immediate value)

- [x] **1.1** Delete stale docs: `PLAN.md`, `HUNTER_PLAN.md`, `EXPIRED_PLAN.md`, `PROJECT_REVIEW_AND_REFACTOR_PLAN.md`, `WEBSITE_PLAN.md`
- [x] **1.2** Delete debug artifacts: `_probe*.py`, `tracker_broken.xlsx`
- [x] **1.3** Add `__pycache__/` and `*.pyc` to `.gitignore`, remove tracked `__pycache__` dirs (was already done)
- [x] **1.4** Unify `_run_apply_agent` in `telegram_bot.py` to use `services/apply_service.py`

### Phase 2 — Split telegram_bot.py (MEDIUM risk) ✅ COMPLETE (2026-05-26)

- [x] **2.1** Extract command handlers into `hunter/commands/` module (15 files)
- [x] **2.1b** Extract bot infrastructure into `hunter/bot/` (6 files: state, keyboards, notifications, paste, formatters, apply_runner)
- [x] **2.1c** Extract scheduled callbacks into `hunter/schedules/` (9 files + register() helper)
- [x] **2.2** `build_application()` + schedule setup remain in `telegram_bot.py` (schedule uses `schedules.register()`)
- [x] **2.3** `telegram_bot.py` is now a ~200-line import shim with re-exports for backward compat

### Phase 3 — Merge job_fetch/ into sources/ (MEDIUM risk) ✅ COMPLETE (2026-05-26)

- [x] **3.1** Add `fetch_text(url) -> str` + `matches_url(url) -> bool` to `BaseSource` ABC; port `html_fallback` into `hunter/sources/`
- [x] **3.2** Move `job_fetch/*.py` logic into the corresponding `hunter/sources/*.py` — 5 batches: trivial wrappers (3.2a), ATS aggregator (3.2b), JSON APIs (3.2c), NEXT_DATA/cloudscraper (3.2d), Playwright-heavy (3.2e)
- [x] **3.3** Add `hunter.sources.fetch_job_text(url)` dispatcher + route every caller (`apply_agent`, `expired_marker`, `gmail_enricher`, `bot/apply_runner`, `commands/*`) through it. Fold `linkedin_parse.py` URL helpers into `hunter/sources/linkedin.py`.
- [x] **3.4** Delete `job_fetch/` package

### Phase 4 — Split apply_agent.py (MEDIUM risk) ✅ COMPLETE (2026-05-27)

- [x] **4.1** Extract API pipeline into `hunter/apply_api.py`
- [x] **4.2** Extract CLI pipeline into `hunter/apply_cli.py`
- [x] **4.3** Make apply callable as import (not just subprocess)
- [x] **4.4** Keep `apply_agent.py` as thin CLI entry point

Shared helpers extracted to `hunter/apply_shared.py` (constants, Telegram, CL review,
validate_content, compute_output_folder, ApplyError). All module-level mutable globals
(_SKIP_DEDUP, _FULL_MODE, _APPLY_META_COMPANY/TITLE) replaced by function parameters.
apply_agent.py: 1473 → 194 lines. 61 new tests (903 + 13 = 916 total).

### Phase 5 — SQLite tracker (HIGH impact, MEDIUM risk) ✅ COMPLETE (2026-05-27)

- [x] **5.1** Create `hunter/db.py` with SQLite schema (WAL mode, `sheets_row`+`sheets_dirty` columns)
- [x] **5.2** Migrate tracker functions to SQLite (atomic writes, no PermissionError)
- [x] **5.3** Add `/export` command for Excel export
- [x] **5.4** Keep openpyxl only for doc generation formatting; tracker_cache loads from SQLite
- [x] **5.5** gsheets_sync: all Sheets metadata (`sheets_row`, `sheets_dirty`) moved from TrackerCache to DB. 6 new tracker.py functions. `_apply_pull_delta_db()` replaces `cache.apply_pull_delta()`. TrackerCache no longer has `sheet_row_index`, `dirty_ids`, or Sheets-related methods.

### Phase 6 — Project structure (after phases 1-5) ✅ COMPLETE (2026-05-31)

- [x] **6.1** Add `pyproject.toml` with metadata and mypy config (replaces `pytest.ini`)
- [x] **6.2** Make project installable (`pip install -e .`); Dockerfile updated with `pip install -e . --no-deps`
- [x] **6.3** Entry point: `python -m hunter` via `hunter/__main__.py`; `hunter.py` becomes a thin shim; `hunter` CLI script registered in `pyproject.toml`

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
| Working Nomads | 2026-06 | OK | Public Elasticsearch `/jobsapi/_search` (5400+ jobs) |
| Jobspresso | 2026-06 | OK | RSS `?feed=job_feed`; only ~10 latest, no pagination |
| Built In | 2026-06 | OK | cloudscraper + BS4 DOM (`data-id="job-card"`); detail via html_fallback |
| JustRemote | 2026-06 | OK | JSON API `justremote-api.herokuapp.com/api/v1/jobs?category=developer` (~10 newest); detail via single-job API |
| RemoteOK | 2026-04 | OK | JSON API |
| Himalayas | 2026-04 | OK | JSON API |
| 4dayweek.io | 2026-04 | OK | JSON API v2 |
| WeWorkRemotely | 2026-04 | OK | RSS feed |
| RemoteLeaf | 2026-04 | OK | HTML listing |
| Inhire.io | 2026-06 | OK | Playwright + Vuex; live-verified 25 jobs (Angular roles). Needs prod image rebuilt with current Dockerfile |
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
| 2026-05-22 | sonnet | Drive URL tracking: tracker col 12 (Drive URL), get_drive_url_by_url, set_drive_url, upload_application_folder writes URL after upload, upload_missing_folders skips already-uploaded rows (17 new tests, 458 total) |
| 2026-05-26 | opus | Phase 2 complete: split telegram_bot.py (1967→200 lines) into bot/ (6), commands/ (15), schedules/ (9). All 748 tests pass. |
| 2026-05-26 | sonnet | Fix hanging test: test_cmd_url_force_waiting_triggers_force_run patched bot._force_run but cmd_url calls url_message._force_run directly; changed patch target to hunter.commands.url_message._force_run. 748 tests in 4.55s. |
| 2026-05-26 | opus | Phase 3 complete: merged job_fetch/ (23 files, ~2475 lines) into hunter/sources/. Each source now owns matches_url + fetch_text. hunter.sources.fetch_job_text() dispatches by URL. linkedin_parse helpers folded into linkedin source. Workable JSON-API extraction restored on AtsAggregator. job_fetch/ deleted. 94 new tests, 842 total in 4.84s. |
| 2026-05-27 | sonnet | Phase 4 complete: split apply_agent.py (1473→194 lines) into hunter/apply_shared.py (702), hunter/apply_api.py (370), hunter/apply_cli.py (331). All module globals eliminated; functions importable with clean params. 74 new tests (916 total in 6s). |
| 2026-05-27 | sonnet | Phase 5 complete: SQLite tracker migration. 5.1 db.py schema, 5.2 all tracker CRUD → SQLite, 5.3 /export command, 5.4 openpyxl removed from tracker_cache (load_from_db), 5.5 gsheets Sheets metadata moved to DB (set_sheets_row etc.), gsheets_sync rewritten, _apply_pull_delta_db replaces cache.apply_pull_delta. 937 tests pass. |
| 2026-05-27 | sonnet | Drive log upload: upload_log_file() in gdrive_sync.py uploads hunter_errors.log to Job Hunter/Logs/ on Drive daily at 06:10 (scheduled_gdrive_upload_logs). 5 new tests (942 total). |
| 2026-05-31 | sonnet | Phase 6 complete: pyproject.toml (metadata + mypy + pytest config), hunter/__main__.py (main() moved from hunter.py), hunter.py → thin shim, pytest.ini deleted, Dockerfile updated with pip install -e . --no-deps. |
| 2026-05-29 | sonnet | CV generation quality: 5 base CVs per track (angular/react/ai/fullstack_angular_nest/fullstack_react_next), stack detection in apply_api.py (31 tests), generation_rules.md renamed + strengthened RED LINES (no Angular version in summary, no invented client scale, no foreign-language keywords in EN), CLI paste-file support via Pro subscription, APPLICATIONS_DIR env var in apply.md, preview_apply.py tool, real job fixtures in tests/fixtures/sample_jobs/. 976 tests total. |
| 2026-06-03 | opus | Bootstrap dedup self-heal (BOOTSTRAP_DEDUP_PLAN.md): `tracker.insert_pulled_rows()` inserts Sheet rows missing from tracker.db (dedup by id+url_norm, skips blank ID, intra-batch dedup); `pull_full_snapshot()` now inserts-then-updates and returns `inserted` count; `_post_init` pulls once at startup so a fresh/empty DB self-heals after container restart. Fixes re-processing of live vacancies. 9 new tests in test_bootstrap_dedup.py (1040 total). Verified in prod: startup pull inserted 23 rows. |
| 2026-06-03 | opus | `tools/dedup_sheet.py`: one-time cleanup of historical duplicate rows in the Sheets tracker. Groups by normalize_url, keeps best row (filled Sent, else earliest), deletes rest via delete_sheet_row (high→low). Dry-run by default, `--apply` to delete; local tracker.db untouched. 10 new tests (1050 total). |
| 2026-06-05 | opus | pracuj 429 fix (PRACUJ_RATE_LIMIT_FIX.md, branch fix/pracuj-rate-limit). Root cause of /hunt gmail mass-429: gmail_enricher fired up to 5 parallel detail fetches at one Cloudflare host. (1) Extracted reusable hunter/rate_limiter.py DomainLimiter (global+per-host concurrency + per-host delay + per-host overrides) out of expired_marker. (2) Rewrote enrich_jobs on it (async, pracuj override 2 conc/1.0s). (3) pracuj _fetch_detail_html backs off on 429 (Retry-After/exp, 2 retries) instead of cascading; fetch_text re-raises 429 instead of html_fallback. (4) Circuit breaker in _retry_failed (shared _CONSECUTIVE_FAIL_LIMIT). (5) APPLY_RATE_LIMITED_EXIT_CODE 45 + is_rate_limit_error → ApplyOutcome "rate_limited"; retry no longer escalates increment_fail_count on transient 429. (7) gmail stub title derived from URL slug (_title_from_url) not email subject, so title↔URL always agree. 26 new tests (1142 total). |
| 2026-06-07 | opus | Pull deletion-reconcile + cache refresh. Root cause of `/unsent` showing rows that look sent in Sheets: pull only inserted+updated by ID, never reacted to rows *deleted* from the Sheet → orphans lingered in tracker.db with blank Sent. Added `tracker.mark_orphans_expired()` + `gsheets_sync._reconcile_deleted_rows()` (stamps EXPIRED, clears sheets_dirty + stale sheets_row, keeps row for dedup, never overwrites existing Sent; guarded by `_RECONCILE_MIN_RATIO=0.8` against partial reads). Wired as step 3 of `pull_full_snapshot()`. Second fix: `scheduled_gsheets_pull` now calls `cache.load_from_db()` after any pull change so `/unsent`+`/status` aren't stale until restart. Manually reconciled 14 existing orphans in prod tracker.db. 8 new tests (1150 total). |
| 2026-06-04 | opus | Sent → clean-date normalizer. `hunter/sent_parse.py` parses the messy Sent column (DD MM YY, ISO, `1305`, Polish/English "applied", EXPIRED markers) into a real date; `hunter/sent_normalizer.py` writes it into Sheet-only column L "Applied Date" (A–K sync never touches L). Wired as `/normalize` command + daily `scheduled_normalize_sent` (00:20, GSHEETS_ENABLED). CLI `tools/normalize_sent.py` (dry-run/`--apply`) + read-only `tools/stats_sheet.py`. Third Sheet tab uses COUNT + QUERY(YYYY-MM) over column L for totals/monthly. Verified on prod sheet: 511 rows → 103 dates. 59 new tests (1109 total). |
| 2026-06-08 | opus | New remote sources Queue 1 (docs/new-sources/, from "13 sites" PDF). Working Nomads (`workingnomads.py`, public Elasticsearch `/jobsapi/_search`, 5400+ jobs, description in `_source`, `fetch_text` re-queries by slug) + Jobspresso (`jobspresso.py`, WP Job Manager RSS `?feed=job_feed`, ~10 latest only). Both wired into config toggles + ALL_SOURCES + `_fetch_roster`. 17→19 sources. Live-verified: WN 30 frontend hits, JP trickle. |
| 2026-06-08 | opus | New remote sources Queue 2 — Built In (`builtin.py`). Cloudflare-fronted, no JSON API / NEXT_DATA / JSON-LD on listings → cloudscraper + BeautifulSoup DOM via stable `data-id` markers (`job-card`/`company-title`/`job-card-title`). Queries `/jobs/remote/dev-engineering?search={angular,frontend,react}`; arrangement label parsed by fullmatch (avoids title "…- Remote" false hits), location defaults Remote. `fetch_text` uses html_fallback. 19→20 sources. Live-verified: 23 search → 17 pass central filter. |
| 2026-06-08 | opus | Playwright status clarified (no code). Found Known Issue #4 stale: `playwright` is already active in requirements.txt + Dockerfile (`playwright install chromium --with-deps`) on master. Live-verified Inhire now returns 25 jobs incl. Angular roles. Empirically confirmed Playwright does NOT unblock Wellfound (real headless Chromium → HTTP 403; needs login session + stealth) and is irrelevant to Jobgether (its problem is 0 frontend yield, not access). Updated Known Issue #4 + Inhire health note. |
| 2026-06-08 | opus | New remote sources Queue 3 — recon only, both DEFERRED (no code). Wellfound: hard 403 on every request (1692-byte challenge), unbypassed by requests *and* cloudscraper → needs Playwright+login (Docker-blocked), realises plan variant C (decline). Jobgether: reachable but no clean listing JSON (`/feed/remote-jobs.json` is an 82-byte summary; Algolia creds not extractable; data only in detail-page JSON-LD), fragile Tailwind DOM with no stable data-id/testid, no server-side frontend filter (`?search=` ignored), and 0 title-filter hits in the 50-job dev category. Confirms plan's "low ROI — don't start unless needed". Findings documented in docs/new-sources/QUEUE-3-hard.md + OVERVIEW.md. |
| 2026-06-08 | opus | Fix false-EXPIRED from pull reconcile (branch fix/reconcile-false-expired). Root cause traced from prod: after the Sheets OAuth token expired (`invalid_grant`), new applies/skips couldn't be mirrored (`mirror_new_row` returns early → `sheets_row` stays NULL); the next successful pull's `_reconcile_deleted_rows` saw "ID in DB, absent from Sheet" and `mark_orphans_expired` stamped them EXPIRED — conflating *never-pushed* with *user-deleted* (incl. URL-less rows, proving it wasn't `expired_check`; verified `is_job_expired`→False on a live Built In job). Fix: `mark_orphans_expired` WHERE now also requires `sheets_row IS NOT NULL` (only ever-mirrored rows can be reconciled as deletions). Updated 2 existing reconcile tests (deleted orphans now carry a sheets_row) + 2 new never-mirrored-protection tests (1200 total). |
| 2026-06-08 | opus | New remote sources Queue 2 — JustRemote (`justremote.py`). SPA backed by a public JSON API on a separate host (`justremote-api.herokuapp.com/api/v1/jobs`); listing `?category=developer` returns ~10 newest dev roles (skill filter is client-side, API ignores it → low-volume trickle like Jobspresso). `fetch_text` uses the single-job API `/jobs/{slug}` (about_role/who_looking_for/our_offer/about_company) instead of scraping the SPA. Canonical URL `justremote.co/{href}`; `_format_location` guarantees a remote token. No pagination (page 1==2). 20→21 sources. Live-verified: API + single-job fetch work; momentary 0 frontend in the newest-10. |
| 2026-06-08 | opus | Source helper consolidation (deferred from PR #83 review). New `hunter/sources/text_utils.py`: `strip_html(html, max_len)` (HTML fragment → plain text, unescape + whitespace-collapse + truncate) replaces 8 local `_text_preview`/`_html_to_plain` copies (arbeitnow, himalayas, remoteok, remotive, weworkremotely, workingnomads, jobspresso, justremote); `REMOTE_ANY` frozenset + `ensure_remote_token(base, geo=None)` replace the duplicated `_REMOTE_ANY` set (workingnomads, jobspresso) and justremote's substring remote-token logic. Each source keeps its own `_format_location` wrapper (input shapes differ) but delegates the core. remotive's `_format_location` left untouched — its synonym set intentionally excludes "remote". No behaviour change (location strings + stripped text identical). 11 new tests in test_text_utils.py (1198 total). |
| 2026-06-10 | opus | PL/EN language routing + enforce-gate (branch fix/pl-en-language-routing). Root cause (traced from 2 prod CVs, RTVEuroAGD/theprotocol + DCG/solid.jobs): for Polish postings the EN CV shipped riddled with Polish ("responsywne interfejsy (responsive interfaces)", "monolitycznych to mikroserwisach", "(7+ lat doświadczenia)") because (a) `lang` was detected but never used, (b) the ATS loop mirrors the Polish posting's keywords verbatim into resume_en, (c) `resume_sanitizer`/`content_qa` only *warn*, never block — the broken EN PDF (the one delivered in short mode) was sent anyway. Fix: new `hunter/lang_guard.py` (deterministic `detect_posting_language` + Polish-in-EN / English-in-PL detection via diacritics+lexicon+suffix+bilingual-gloss, dependency-free, Polish place-name allowlist so "Wrocław" isn't flagged); new `apply_shared.enforce_language_separation` enforce-gate wired into BOTH `apply_api` and `apply_cli` after sanitize — repairs by *translating from the clean opposite-language counterpart* (role-count guarded) + up to 2 in-place cleanup passes, and BLOCKS delivery (no broken doc: API `sys.exit(0)`, CLI deletes docs+returns) if strong Polish survives. ATS rewrite prompts now forbid foreign words/glosses. Delivery routing: `content["primary_lang"]` makes short mode also render the clean PL CV for PL postings (so a Polish vacancy ships BOTH PL+EN CV and CL). **Live-verified** on both prod URLs (theprotocol + solid.jobs): EN resume now fully clean (en_strong/soft/pl all empty), full bilingual set generated, gate logs show active repair each run; full suite run 4× green. 32 new tests (test_lang_guard 21 + test_lang_enforce_gate 5 + ATS-prompt/routing ... 1232 total). |
