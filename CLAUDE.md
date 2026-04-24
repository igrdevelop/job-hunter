# CLAUDE.md — Project Context for AI Agents

This file provides context for Claude and other AI agents working in this repository.
Read it before making any changes.

---

## What This Project Is

**Job Hunter Bot** — an autonomous system that:
1. Scrapes Polish/European IT job boards for Senior Frontend (Angular) vacancies
2. Filters them by location, seniority, stack
3. Sends new jobs to Telegram for review
4. On user approval (or automatically), generates a tailored CV + cover letter package via LLM
5. Tracks everything in `tracker.xlsx`

**Owner profile:** Senior Frontend Developer, Angular, 7+ years. Located in Wrocław. Seeking full-remote or hybrid-Wrocław roles. Polish and English speaker.

---

## Repository Layout

```
apply_agent.py          # Core apply pipeline: fetch job → LLM → generate docs → tracker
generate_docs.py        # DOCX/PDF/TXT generation from content.json using python-docx + LibreOffice
hunter.py               # Entry point: starts the Telegram bot + scheduler
llm_client.py           # Thin wrapper: supports Anthropic API and OpenAI API

hunter/
  config.py             # ALL config: env vars, filters, schedule, paths, enabled sources
  models.py             # Job dataclass (title, company, location, salary, url, source)
  filters.py            # Central job filter: location, seniority, stack, exclude patterns
  main.py               # Hunt loop: fetch all sources → filter → dedup → notify Telegram
  telegram_bot.py       # Telegram bot: /hunt [sources…], /force, /status, /schedule + inline buttons
  tracker.py            # tracker.xlsx read/write: known URLs, add rows, dedup helpers
  sources/
    base.py             # BaseSource ABC — all scrapers implement search() → list[Job]
    linkedin.py         # LinkedIn scraper (Playwright, requires session cookie)
    justjoin.py         # JustJoin.it API scraper
    nofluffjobs.py      # NoFluffJobs API scraper
    bulldogjob.py       # Bulldogjob.pl scraper
    pracuj.py           # Pracuj.pl scraper (cloudscraper + __NEXT_DATA__ parsing)
    theprotocol.py      # theprotocol.it scraper (cloudscraper + __NEXT_DATA__ / dehydratedState)
    solidjobs.py        # Solid.Jobs RSS feed scraper
    arbeitnow.py        # Arbeitnow.com JSON API (EU / remote)
    inhire.py           # Inhire.io scraper (Playwright + Vuex store, disabled by default)

job_fetch/
  __init__.py           # Dispatcher: routes job URL to the right fetcher
  linkedin.py / pracuj.py / theprotocol.py / ... # Per-site detail fetchers
  html_fallback.py      # Generic HTML fetcher (requests/cloudscraper + BeautifulSoup)

prompts/
  system_prompt.md      # LLM instructions: resume tailoring, ATS gap analysis, cover letter rules
  candidate_profile.md  # Candidate data: contact, stack, work experience, education (single source of truth)

.claude/commands/
  apply.md              # Claude Code slash-command /apply — used in CLI fallback mode
  batch.md              # /batch — process multiple URLs
  add-source.md         # /add-source — guide for adding a new job board scraper

tracker.xlsx            # Main data store: every job seen, applied, or skipped
Applications/           # Generated documents: Applications/{YYYY-MM-DD}/{CompanyName}/
  2026-04-14/
    CompanyName/
      content.json      # LLM output (structured data)
      job_posting.txt   # Raw job description text (saved for free, no LLM)
      CV_en.pdf
      Cover_Letter_en.pdf
      ...

prompts/system_prompt.md
requirements.txt
.env                    # Secret config — NEVER commit
.env.example            # Template
```

---

## Key Configuration (`hunter/config.py` + `.env`)

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHAT_ID` | — | Required |
| `AUTO_APPLY` | `false` | Auto-generate docs without manual Telegram button press |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `LLM_MODEL` | `claude-3-5-haiku-20241022` | Model for API mode |
| `LLM_API_KEY` | — | API key for LLM |
| `APPLY_USE_CLI` | `false` | Use Claude Code CLI (Pro subscription) instead of API |
| `MAX_JOBS_PER_RUN` | `10` | Safety cap: max docs generated per hunt cycle |
| `APPLY_DELAY_SEC` | `30` | Pause between jobs in auto-apply batch |
| `GENERATE_PL_RESUME` | `false` | Generate Polish CV variant (full-mode only by default) |
| `INHIRE_ENABLED` | `false` | Requires Playwright — disabled until installed |

All source toggles: `LINKEDIN_ENABLED`, `BULLDOGJOB_ENABLED`, `PRACUJ_ENABLED`, `THEPROTOCOL_ENABLED`, `SOLIDJOBS_ENABLED`, `INHIRE_ENABLED`, `JOBLEADS_ENABLED`, `ARBEITNOW_ENABLED`.

---

## Pipeline Flow

### Hunt cycle (`hunter/main.py`)
1. Each source calls `source.search()` → `list[Job]`
2. `filters.apply_filters()` — location, seniority, stack, exclude patterns
3. Dedup: URL-based (`normalize_url`) + company+title key
4. New jobs → Telegram card with inline buttons: **Apply** / **Skip**
5. If `AUTO_APPLY=true` — immediately trigger apply pipeline

### Apply pipeline (`apply_agent.py`)
1. `job_fetch.fetch_job_text(url)` — fetch full job description (free, no LLM)
2. Save `job_posting.txt` to the output folder
3. LLM call: `candidate_profile.md` + `system_prompt.md` + job text → structured `content.json`
   - **Auth priority:** CLI first (Claude Pro) → API fallback (LLM_API_KEY)
   - **React-only skip:** if stack is React without Angular → log to tracker, skip docs
4. Cover letter self-review loop (up to 3 rounds, using LLM)
5. Compute output folder: `Applications/{today}/{CompanyName}/`
6. Write `content.json`
7. Run `generate_docs.py` → DOCX/PDF via python-docx + LibreOffice (`soffice.exe`)
8. Update `tracker.xlsx`
9. Send Telegram notification with file list

### Doc generation modes
- **Short mode** (default): PDF only, English CV only (3 files: CV EN, Cover Letter EN, Cover Letter PL)
- **Full mode** (`--full` flag only): DOCX + PDF, EN + PL CV, About_Me `.txt` files (10 files)
- **Force mode** (`--force`): skip tracker dedup check (used by `/force` Telegram command)

---

## tracker.xlsx Schema

| Column | Description |
|---|---|
| Date | Application date |
| Company | Company name |
| Job Title | Position title |
| Stack | Tech stack from LLM analysis |
| ATS % | Keyword match score (or SKIP / FAIL / —) |
| URL | Canonical job URL (dedup key) |
| Folder | Relative path to `Applications/` subfolder |
| Sent | Date sent, or blank/dash |
| Re-application | Flag |
| To Learn | Skills gap noted |
| ID | Short UUID (8-char hex) — internal key for `to_send.xlsx` sync |

**Dedup logic:**
- URL dedup: `normalize_url()` strips tracking params (`utm_*`, `sendid`, `trk`, etc.)
- Company+title dedup: prevents same role from two sources
- React-only jobs: written with `Sent = "—"` to block future reprocessing

---

## to_send.xlsx — Sending Workflow

`to_send.xlsx` is a **derived, human-editable** file that shows only the rows you have not yet sent. It is rebuilt automatically and is safe to keep open while the bot runs.

**How it works:**
1. Every `add_applied()` / `add_failed()` / `add_manual_jobleads_pending()` in `tracker.xlsx` gets a short `ID`.
2. After each successful apply, `tracker_service.record_successful_apply()` calls `to_send.sync_and_rebuild()` automatically.
3. You open `to_send.xlsx`, find the rows you have sent, and fill in the `Sent` column (any value: a date, `+`, `ok` — doesn't matter).
4. Run `/sync_sent` in Telegram (or wait for the next apply). The bot:
   - reads the `Sent` values from `to_send.xlsx` by `ID`,
   - writes them back to `tracker.xlsx`,
   - rebuilds `to_send.xlsx` — sent rows disappear, only pending rows remain.
5. If `to_send.xlsx` is open/locked when the bot tries to rebuild it, it logs a warning and continues without crashing. Close the file and run `/sync_sent` again.

**Key files:**
- `hunter/to_send.py` — `read_sent_marks()`, `rebuild()`, `sync_and_rebuild()`
- `hunter/config.py` — `TO_SEND_PATH`
- `hunter/tracker.py` — `iter_rows_for_to_send()`, `apply_sent_updates()`

**Rows in to_send.xlsx:**
- Included: successful applies (ATS%), FAIL rows, MANUAL rows — anything actionable.
- Excluded: SKIP rows (geo/stack filtered, nothing to send), rows already marked Sent.

---

## Adding a New Job Source

See `.claude/commands/add-source.md` for the full step-by-step guide.

**Short version:**
1. Create `hunter/sources/yoursite.py` — subclass `BaseSource`, implement `search() → list[Job]`
2. Create `job_fetch/yoursite.py` — implement `fetch_yoursite(url) → str` (returns job description text)
3. Add `YOURSITE_ENABLED` toggle to `hunter/config.py`
4. Register in `hunter/sources/__init__.py` (conditional import) and `job_fetch/__init__.py` (domain routing)

**Scraping strategies (in priority order):**
1. Public JSON API (easiest)
2. `__NEXT_DATA__` / React Query `dehydratedState` in HTML (Next.js sites)
3. RSS feed (Solid.Jobs)
4. `cloudscraper` + BeautifulSoup DOM (Cloudflare-protected sites: Pracuj, theprotocol)
5. Playwright headless browser (SPAs: LinkedIn, Inhire)

---

## Git Workflow

- **Active branch:** `develop` — all changes go here
- `main` is production-stable
- Always commit on `develop`, never force-push `main`

---

## Important Rules for Agents

- **Never commit `.env`** — it contains real secrets
- **Never commit `tracker.xlsx`** — it contains personal application data
- **Never commit files in `Applications/`** — generated documents
- Always test syntax after edits: `python -m compileall .`
- When editing `tracker.py` or `generate_docs.py`, check column index constants in `tracker.py` — they are hardcoded (`URL_COL_INDEX = 6`, etc.)
- Candidate profile is the single source of truth: `prompts/candidate_profile.md` — update it when experience changes
- LibreOffice path on Windows: typically `C:/Program Files/LibreOffice/program/soffice.exe` — configured in `generate_docs.py`
- On Windows, use PowerShell; `&&` is not valid — use `;` or separate commands
- When changing `tracker.xlsx` schema (columns, constants), adding new runtime files (like `to_send.xlsx`), or changing user-facing bot behaviour — update the relevant section of `CLAUDE.md` in the same change
