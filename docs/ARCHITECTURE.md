# Architecture — Job Hunter Bot

High-level block diagram of the project. Updated when the structure changes.

For deep details on each subsystem see `CLAUDE.md` (single source of truth for
config, schema, source list, refactor plan).

---

## Block diagram

```
                            ┌──────────────────────────────────────────────┐
                            │              hunter.py (entry)               │
                            │   validate config → build TG app → polling   │
                            └────────────────────┬─────────────────────────┘
                                                 ▼
                            ┌──────────────────────────────────────────────┐
                            │       hunter/telegram_bot.py (shim ~244)     │
                            │ build_application() + schedules.register()   │
                            └────────────────────┬─────────────────────────┘
                                                 │
        ┌────────────────────┬───────────────────┼──────────────────────┬────────────────────────┐
        ▼                    ▼                   ▼                      ▼                        ▼
┌──────────────┐   ┌───────────────────┐   ┌─────────────┐    ┌──────────────────┐   ┌─────────────────────┐
│   bot/       │   │   commands/       │   │ schedules/  │    │   JobQueue       │   │  URL message        │
│  (state,     │   │  /start /hunt     │   │ hunt 08/13  │    │  scheduler       │   │  paste detector     │
│  paste,      │   │  /force /status   │   │ check_exp.  │    │  Europe/Warsaw   │   │  Apply/Skip btns    │
│  keyboards,  │   │  /schedule        │   │ tracker_bk  │    │                  │   │                     │
│  notifs,     │   │  /unsent          │   │ gdrive      │    └──────────────────┘   └─────────────────────┘
│  formatters, │   │  /sync_sent       │   │ gsheets     │
│  apply_run.) │   │  /process_manual  │   │ pending_rep │
└──────────────┘   │  /about_me        │   │ email_resp  │
                   │  /check_expired   │   │ daily_summ. │
                   │  /debug_url       │   └─────────────┘
                   │  /gsheets_*       │           │
                   │  /gdrive_*        │           ▼
                   │  /check_responses │   ┌─────────────────────────────────────────────┐
                   └────────┬──────────┘   │       hunter/main.py — run_hunt()           │
                            │              └──────────────────┬──────────────────────────┘
                            └─────────────────────────────────┤
                                                              ▼
                       ┌───────────────────── HUNT PIPELINE ────────────────────────┐
                       │                                                            │
                       │   ┌── 1. FETCH ────────────────────────────────────────┐   │
                       │   │  for source in ALL_SOURCES:                        │   │
                       │   │      jobs += source.search()  ← hunter/sources/    │   │
                       │   │   17 sources (JustJoin, NoFluff, LinkedIn,         │   │
                       │   │    Pracuj, theProtocol, Bulldog, ATS-aggreg, ...)  │   │
                       │   └────────────────────────┬───────────────────────────┘   │
                       │                            ▼                               │
                       │   ┌── 2. FILTER ───────────────────────────────────────┐   │
                       │   │  filters.apply_filters_with_stats(jobs)            │   │
                       │   │  keywords / level / location / patterns /          │   │
                       │   │  React-only / German                               │   │
                       │   └────────────────────────┬───────────────────────────┘   │
                       │                            ▼                               │
                       │   ┌── 3. DEDUP ────────────────────────────────────────┐   │
                       │   │  vs tracker.xlsx (URL norm + company+title +       │   │
                       │   │  fuzzy CT + cooldown) — uses tracker_cache         │   │
                       │   └────────────────────────┬───────────────────────────┘   │
                       │                            ▼                               │
                       │   ┌── 4. ACT ──────────────────────────────────────────┐   │
                       │   │   AUTO_APPLY=false → Telegram cards (Apply/Skip)   │   │
                       │   │   AUTO_APPLY=true  → subprocess apply_agent.py     │   │
                       │   │                      (cap MAX_JOBS_PER_RUN)        │   │
                       │   └────────────────────────┬───────────────────────────┘   │
                       │                            │                               │
                       │   ┌── 5. RETRY ────────────▼───────────────────────────┐   │
                       │   │  _retry_failed() — read FAIL rows, run apply again │   │
                       │   └────────────────────────────────────────────────────┘   │
                       └────────────────────────────┬───────────────────────────────┘
                                                    │  (per job, subprocess)
                                                    ▼
                ┌──────────────────────── APPLY PIPELINE (apply_agent.py) ─────────────────────┐
                │                                                                              │
                │   ┌── A. FETCH JOB TEXT ────────────────────────────────────────────────┐    │
                │   │  hunter.sources.fetch_job_text(url)                                 │    │
                │   │     → pick source by matches_url → call source.fetch_text(url)      │    │
                │   │     → fall back to html_fallback.fetch_html when nothing matches    │    │
                │   │  HTTP / cloudscraper / Playwright / RSS / html_fallback             │    │
                │   │  → job_posting.txt                                                  │    │
                │   └──────────────────────────────────┬──────────────────────────────────┘    │
                │                                      ▼                                       │
                │   ┌── B. EXPIRED CHECK ─────────────────────────────────────────────────┐    │
                │   │  expired_check.is_job_expired(text)  → skip (write EXPIRED)         │    │
                │   └──────────────────────────────────┬──────────────────────────────────┘    │
                │                                      ▼                                       │
                │   ┌── C. LLM CALL ──────────────────────────────────────────────────────┐    │
                │   │  llm_client.call_llm(                                               │    │
                │   │    system_prompt.md + candidate_profile.md + job_text               │    │
                │   │  )  →  content.json {resume, cover_letter, ats_score, ...}          │    │
                │   │   API mode  : Anthropic / OpenAI                                    │    │
                │   │   CLI mode  : claude CLI (Pro subscription)                         │    │
                │   └──────────────────────────────────┬──────────────────────────────────┘    │
                │                                      ▼                                       │
                │   ┌── D. CL SELF-REVIEW LOOP ───────────────────────────────────────────┐    │
                │   │  up to 3 LLM rounds: critique → rewrite cover letter                │    │
                │   └──────────────────────────────────┬──────────────────────────────────┘    │
                │                                      ▼                                       │
                │   ┌── E. DOC GENERATION ────────────────────────────────────────────────┐    │
                │   │  generate_docs.py  →  python-docx  →  LibreOffice headless → PDF    │    │
                │   │  output: Applications/<date>/<Company>/CV_<Name>.pdf + DOCX + ...   │    │
                │   └──────────────────────────────────┬──────────────────────────────────┘    │
                │                                      ▼                                       │
                │   ┌── F. TRACKER WRITE ─────────────────────────────────────────────────┐    │
                │   │  tracker_service.record_successful_apply()                          │    │
                │   │  → tracker.xlsx row (12+ cols: Date, Company, Title, Stack, ATS%,   │    │
                │   │     URL, Folder, Sent, …, Drive URL)                                │    │
                │   └──────────────────────────────────┬──────────────────────────────────┘    │
                │                                      ▼                                       │
                │   ┌── G. EXTERNAL SYNC (best-effort) ───────────────────────────────────┐    │
                │   │  gsheets_sync.mirror_new_row(row)  → Google Sheets API v4           │    │
                │   │  gdrive_sync.upload_application_folder() → Google Drive API v3      │    │
                │   │  TG notify + send PDF/DOCX file                                     │    │
                │   └─────────────────────────────────────────────────────────────────────┘    │
                └──────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────── DATA STORES & EXTERNAL ─────────────────────────────┐
│                                                                                 │
│   tracker.xlsx          ── source of truth (dedup + history)                    │
│   tracker_cache (RAM)   ── O(1) dedup, asyncio.Lock                             │
│   gsheets_state.json    ── active spreadsheet ID                                │
│   gsheets_token.json    ── OAuth2 token (auto-refresh)                          │
│   Applications/<date>/  ── generated DOCX/PDF folders                           │
│   backups/              ── daily snapshots of tracker.xlsx                      │
│                                                                                 │
│   ─── External APIs ───                                                         │
│   Telegram Bot API      ← bidirectional (commands ↔ notifications)              │
│   17 job board APIs     ← scrapers (HTTP / cloudscraper / Playwright / RSS)     │
│   Anthropic / OpenAI    ← LLM (resume + cover letter)                           │
│   Google Sheets API v4  ↔ mirror tracker (push new rows, pull user edits)       │
│   Google Drive API v3   → upload application folders                            │
│   Gmail API             ← parse LinkedIn/NoFluff/Pracuj/JustJoin alerts +       │
│                            confirmation emails for /check_responses             │
│   LibreOffice headless  → DOCX → PDF conversion                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Key facts for whole-picture understanding

- **Two independent processes** joined by subprocess: `hunter.py` (long-running
  async bot) and `apply_agent.py` (CLI tool per vacancy). Historical reason —
  apply_agent was originally standalone; the bot now invokes it per job.
- **tracker.xlsx is the single source of truth.** Sheets, Drive, RAM cache —
  all derive from it.
- **Google Sheets is bidirectional.** Bot pushes new rows; user edits
  `Sent / To-Learn / Re-application` in the browser; bot polls and merges
  back into tracker.xlsx via a conflict matrix.
- **Schedule is staggered:** 17 sources × 3 base times, each offset by ~40 min,
  so a full cycle spans ~11 hours. Prevents hammering all APIs at once.
- **LLM is used as a single transform step** (`job_text → resume JSON`), not
  as an autonomous agent. The cover-letter self-review loop is the closest
  thing to agentic behaviour, but it's still a linear chain. CLI mode
  (`APPLY_USE_CLI=true`) gives Claude CLI real tool use, but it's still a
  single agent, not a chain.

---

## When to update this file

- A new top-level subsystem is added/removed (e.g. another external API, a new
  bot command group, a new pipeline stage).
- A monolith is split (e.g. when Phase 4 / 5 of the refactor plan lands).
- A file count or line count in the diagram becomes meaningfully wrong.

For source list, config vars, tracker schema and refactor checklist — stay in
`CLAUDE.md`. This file is the picture; `CLAUDE.md` is the reference.
