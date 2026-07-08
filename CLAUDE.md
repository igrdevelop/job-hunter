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
                              /sync_sent /process_manual /check_expired /funnel /health
                              /gsheets_status /gsheets_resync /llm /dual
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

## Job Sources (22 active)

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
| LinkedIn Scout relay | linkedin_scout_relay.py | Drains a JSON queue file | No scraping — reads what the standalone `linkedin_scout/` script found; behaves like any other source (not `manual_only`), see below |

---

## Repository Layout

```
apply_agent.py              Core apply pipeline: fetch job -> LLM -> content.json -> generate docs
generate_docs.py            DOCX/PDF generation from content.json (python-docx + LibreOffice)
hunter.py                   Entry point: starts Telegram bot + scheduler
llm_client.py               LLM wrapper: Anthropic + OpenAI with retry + JSON parsing.
                            Anthropic path caches the (large, repeated) system prefix via
                            cache_control=ephemeral, and on effort-capable models (Sonnet 4.6,
                            Opus 4.5+, Fable 5) sets output_config.effort=low + thinking disabled.
                            Both are model-gated so Haiku judge calls never 400.

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
  funnel.py                 Application funnel analytics over tracker.db: compute_funnel(days?) →
                            tracked→generated→sent→confirmed→answered, overall + per source (source
                            inferred from URL via each source's matches_url + registered-domain
                            fallback). Confirmed = ATS ack (confirmation col, stamped by
                            /check_responses); Answered = human reply (answer col). Feeds /funnel
  claim_judge.py            LLM-as-judge CV verification: judge_content() flags claims absent
                            from the candidate profile + posting (fabrication/exaggeration/
                            style); repair_content() drops the offending clause (deterministic
                            quote-drop, LLM rewrite fallback, role-count guarded). Runs between
                            the scrubs and the language gate in both pipelines. See
                            docs/CV_JUDGE_PLAN.md
  expired_check.py          Expired job detection (regex patterns)
  expired_marker.py         Parallel expired check for unsent rows; writes EXPIRED to tracker
  rate_limiter.py           Per-domain async concurrency + delay limiter (DomainLimiter);
                            shared by expired_marker and gmail_enricher to avoid HTTP 429
  source_health.py          Per-source yield tracking in SQLite (source_runs table): record_run()
                            after each source.search() in the hunt loop, health_report() for /health,
                            newly_broken() alerts once when a previously-working source goes dry for
                            SOURCE_HEALTH_ALERT_STREAK consecutive runs (broken selector vs quiet day)
  gsheets_sync.py           High-level Sheets mirror (push/pull/resync/bootstrap)
  gsheets_client.py         Low-level Sheets API v4 wrapper
  gdrive_sync.py            High-level Drive upload (upload_application_folder)
  gdrive_client.py          Low-level Drive API v3 wrapper
  gmail_client.py           Gmail API wrapper
  oauth_alert.py            Detect Google OAuth token expiry (invalid_grant/RefreshError) at the
                            gsheets/gmail/gdrive auth boundary; refresh_or_alert() fires a
                            cooldown-deduplicated Telegram "re-auth needed" alert then re-raises
                            (a dead Sheets token once caused a false-EXPIRED cascade)
  gmail_parsers.py          Parse job alert emails from various boards
  gmail_report.py           Per-email hunt report: build_gmail_report() renders
                            [date · aggregator · subject → taken/dup/filtered]
                            per alert email (chunked under Telegram 4096). Fed by
                            GmailSource.last_email_log + per-job JobOutcome tags
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
    funnel.py               /funnel [days] — application funnel report (hunter.funnel)
    health.py               /health — per-source scraper yield report (source_health)
    llm.py                  /llm [name] — show/switch active LLM profile (hunter.llm_profiles)
    dual.py                 /dual [on|off] — toggle dual-apply A/B comparison (hunter.dual_apply)
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

prompts/                        See prompts/README.md. System files are tracked; ALL
                                candidate-personal files are GITIGNORED (public repo) —
                                they exist locally / on the deploy host only, and
                                docker-compose mounts them into the image.
  README.md                     System-vs-personal split + setup instructions
  generation_rules.md           LLM instructions for resume/CL generation (was system_prompt.md) [tracked]
  judge_rules.md                Claim-judge instructions [tracked]
  candidate_profile.example.md  Template for candidate_profile.md [tracked]
  base_cv_angular.example.md    Template for base CV track files [tracked]
  candidate_profile.md          Candidate data (single source of truth for personal info) [GITIGNORED]
  base_cv_angular.md            Pre-polished bullets for Angular track [GITIGNORED]
  base_cv_react.md              Pre-polished bullets for React / JS track [GITIGNORED]
  base_cv_ai.md                 Pre-polished bullets for AI-first track [GITIGNORED]
  base_cv_fullstack_angular_nest.md  Pre-polished bullets for Angular + NestJS track [GITIGNORED]
  base_cv_fullstack_react_next.md    Pre-polished bullets for React + Next.js track [GITIGNORED]
  examples/                     Cover letter examples, About Me texts [GITIGNORED]
  candidate/                    Private interview notes (not read by code) [GITIGNORED]

tests/                      37+ test files, ~3200 lines (pytest)
tests/fixtures/sample_jobs/ Real job postings per track (angular/react/ai/fullstack_*) for preview
tools/                      Utilities: backup, dedup, gmail auth, gsheets auth, LinkedIn login
tools/preview_apply.py      Run apply pipeline against sample fixtures via CLI subscription
tools/preview_judge.py      Run the claim-judge (+scrubs) on an existing content.json without
                            regenerating — one Haiku call; mirrors run_judge_stage (JUDGE_MODE env)
tools/dedup_sheet.py        One-time cleanup of duplicate rows in the Sheets tracker (--apply to delete)
tools/normalize_sent.py     Write clean "Applied Date" into Sheets column L from Sent (--apply to write)
tools/stats_sheet.py        Read-only stats over the Sheets Sent column (--write-tab for a Stats tab)
tools/screen_calibrate.py   Doomed-gate calibration (docs/DOOMED_GATE_PLAN.md M4): runs
                            assess_job_text over the offline Applications/**/job_posting.txt
                            corpus + a live Google Sheet sample, read-only/dry-run, reports
                            hard/soft hit rate and flags any HARD finding on a row the owner
                            actually sent (must be zero)

linkedin_scout/             STANDALONE — not imported by hunter/, not in Docker, not on the bot's
                            schedule. Runs on the owner's own desktop (residential IP, real Chrome)
                            via Windows Task Scheduler. See "LinkedIn Posts Scout" section below +
                            linkedin_scout/README.md.

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
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, or `openrouter`. **Prefer `/llm <profile>` in Telegram** — the profile system (`hunter/llm_profiles.py`) is the recommended way to switch models at runtime without restart. |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model for API mode (effort `low` + thinking disabled on supporting models). **Source of truth is this `config.py` default — leave `LLM_MODEL` unset in `.env` so model upgrades ship as a commit, not a manual prod edit.** Set it in `.env` only to override (experiment/temporary). Dated snapshots retire (`claude-sonnet-4-20250514` → 2026-06-15, `claude-3-5-haiku-20241022` → 2026-02-19); prefer non-dated aliases. |
| `LLM_DEFAULT_PROFILE` | — | Pin a named profile as default (e.g. `deepseek-r1`). Overrides `LLM_PROVIDER+LLM_MODEL`. Persisted per-vacancy selection via `/llm <name>` wins over this. |
| `DUAL_SHADOW_PROFILE` | `deepseek-v3` | Profile used for the dual-apply shadow comparison run. DB key `dual_shadow_profile` (set via env or future UI) overrides; this is the fallback. Toggle dual mode itself with `/dual on`/`/dual off` (DB key `dual_apply_enabled`). |
| `LLM_API_KEY` | — | API key for LLM provider (fallback; prefer provider-specific vars below) |
| `ANTHROPIC_API_KEY` | — | Anthropic key (for `sonnet` profile + judge) |
| `OPENROUTER_API_KEY` | — | OpenRouter key (for `deepseek-r1`, `deepseek-v3`) |
| `OPENAI_API_KEY` | — | OpenAI key (for `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o`) |
| `APPLY_USE_CLI` | `false` | Use Claude CLI (Pro subscription) instead of API |
| `JUDGE_ENABLED` | `true` | Run the LLM-as-judge CV verification pass |
| `JUDGE_MODEL` | `claude-haiku-4-5-20251001` | Cheap model for the judge (independent of generator). Always Anthropic — uses `JUDGE_PROVIDER`/`JUDGE_API_KEY`, not the main profile. |
| `JUDGE_PROVIDER` | `anthropic` | Judge LLM provider (separate from main provider; Haiku is Anthropic-only) |
| `JUDGE_API_KEY` | — | Judge API key (reads `ANTHROPIC_API_KEY` first; falls back to `LLM_API_KEY`) |
| `JUDGE_MODE` | `warn` | Rollout: `report` (artifact only) / `warn` (+Telegram) / `block` (+abort on surviving fabrication) |
| `JUDGE_MAX_REPAIR_ROUNDS` | `1` | Repair rounds before warn/block |
| `ATS_VERDICT_ENABLED` | `true` | Final independent ATS verdict: after generate_docs, ONE `JUDGE_MODEL` (Haiku) call scores the text extracted from the rendered EN CV PDF against the posting. Stored as `ats_verdict` on content.json + tracker row (`set_ats_verdict`, which now also overwrites `ats_status`/"ATS %"), mirrored to Sheet column **N** (`hunter.verdict_writer`), and shown as the **only** "ATS:" number in Telegram (generator self-score stays in content.json only), and computed for dual-apply shadows too (verdict-based `_ats{NN}` filename suffix). Informational only — never blocks delivery. |
| `ATS_VERDICT_TARGET` | `95` | Target score (%) for the verdict refine loop (`hunter.verdict_refine`) — a verdict at or above this is left alone. |
| `ATS_VERDICT_MAX_REFINES` | `3` | Max escalating rewrite rounds the refine loop runs when the verdict is below target (rounds 1–2 honest, round 3+ stretch — `verdict_refine.STRETCH_FROM_ROUND`). Default `3` (owner decision 2026-07-07: two honest visibility passes, then one openly-add-skills round). `0` disables the loop (old one-shot verdict). See docs/VERDICT_REFINE_PLAN.md. |
| `DOOMED_GATE_ENABLED` | `true` | Deterministic (regex-only, zero LLM cost) full-text screen (`hunter.apply_shared.run_doomed_gate` → `hunter.filters.assess_job_text`), run right after expired-check and before the first LLM call in both pipelines (Step 1.5f). HARD findings (non-Poland onsite/hybrid, non-EU work authorization, unsupported required language) write a SKIP tracker row and abort generation for $0.00; SOFT findings (e.g. stack mismatch) warn in Telegram and generation continues. Force-mode/manual-paste always degrades HARD to warn. See docs/DOOMED_GATE_PLAN.md. |
| `DOOMED_GATE_HARD_ACTION` | `skip` | `skip` aborts generation on a HARD finding; `warn` is an emergency lever to downgrade every HARD finding to a warning without disabling the gate entirely (e.g. if live-data precision turns out worse than calibration). |
| `APPLICATIONS_DIR` | `Applications/` | Output folder override (useful for preview/testing) |
| `CV_GDPR_CLAUSE` | `both` | GDPR/RODO consent clause at CV bottom: `both` (PL+EN), `pl` (PL CV only), `none` |
| `MAX_JOBS_PER_RUN` | `10` | Cap per hunt cycle |
| `APPLY_DELAY_SEC` | `30` | Pause between auto-apply jobs |
| `APPLY_AGENT_TIMEOUT_SEC` | `900` | Subprocess timeout (15 min) |
| `DUAL_SHADOW_TIMEOUT_SEC` | `900` | Hard wall-clock cap for the detached dual-apply shadow run (its own watchdog; independent of the primary timeout). |
| `LINKEDIN_STORAGE_STATE` | — | Path to a Playwright session JSON from `python tools/linkedin_login.py`. **Without it every LinkedIn fetch 429s — the single biggest source of FAIL rows.** Once set, drop `linkedin.com` from `GMAIL_ENRICH_SKIP_HOSTS`. |
| `TELEGRAM_SEND_DOCS` | `true` | Send PDF/DOCX via Telegram after apply |
| `TRACKER_BACKUP_ENABLED` | `true` | Daily backups via JobQueue |
| `SOURCE_HEALTH_ENABLED` | `true` | Record per-source yield per hunt + alert on breakage |
| `SOURCE_HEALTH_ALERT_STREAK` | `3` | Consecutive 0/error runs (for a previously-working source) before alerting |
| `SOURCE_HEALTH_KEEP` | `50` | Per-source run rows retained (ring buffer) |
| `GSHEETS_ENABLED` | `false` | Enable Google Sheets mirror |
| `GSHEETS_TRACKER_ID` | — | Spreadsheet ID (set after first run or auto-created) |
| `GSHEETS_REFRESH_INTERVAL_MIN` | `30` | Sheets → Excel pull interval |
| `GDRIVE_ENABLED` | `false` | Upload application docs to Google Drive after apply |
| `GDRIVE_ROOT_FOLDER_ID` | — | Optional: existing Drive folder ID (auto-creates "Job Hunter" if empty) |
| `GDRIVE_ROOT_FOLDER_NAME` | `Job Hunter` | Name of auto-created root folder on Drive |
| `GMAIL_LOOKBACK_HOURS` | `25` | How far back the Gmail scan reads the inbox (hours) |
| `GMAIL_MAX_RESULTS` | `100` | Max alert emails per scan; report warns if ceiling hit |
| `GMAIL_ENRICH_CONCURRENCY` | `5` | Global cap on parallel enrichment fetches (all hosts) |
| `GMAIL_ENRICH_DOMAIN_LIMIT` | `2` | Default per-host concurrent enrichment fetches |
| `GMAIL_ENRICH_DOMAIN_DELAY` | `0.0` | Default per-host delay (sec) between enrichment fetches |
| `GMAIL_ENRICH_SKIP_HOSTS` | `linkedin.com,pracuj.pl` | Hosts NOT enriched during the hunt (they hard-block → 429/403 and poison the shared rate budget). The email-derived stub is kept. Comma-separated; remove a host once it fetches reliably. |
| `PRACUJ_HOST_CONCURRENCY` | `2` | pracuj.pl per-host concurrency override (Cloudflare 429) |
| `PRACUJ_HOST_DELAY_SEC` | `1.0` | pracuj.pl per-host delay (sec) override |

Source toggles (all default `true` except `GMAIL_ENABLED=false`):
`LINKEDIN_ENABLED`, `BULLDOGJOB_ENABLED`, `PRACUJ_ENABLED`, `THEPROTOCOL_ENABLED`,
`SOLIDJOBS_ENABLED`, `INHIRE_ENABLED`, `JOBLEADS_ENABLED`, `ARBEITNOW_ENABLED`,
`REMOTIVE_ENABLED`, `WORKINGNOMADS_ENABLED`, `JOBSPRESSO_ENABLED`, `BUILTIN_ENABLED`,
`JUSTREMOTE_ENABLED`, `REMOTEOK_ENABLED`, `HIMALAYAS_ENABLED`, `FOURDAYWEEK_ENABLED`,
`WEWORKREMOTELY_ENABLED`, `REMOTELEAF_ENABLED`, `ATS_AGGREGATOR_ENABLED`, `GMAIL_ENABLED`,
`LINKEDIN_SCOUT_RELAY_ENABLED` (default `true` — no scraping, just drains a JSON queue
file the standalone `linkedin_scout/` script writes; see "LinkedIn Posts Scout" below).

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
3a. **Manual-apply "warn but allow" screen** (`filters.screen_job_text`, Step 1.5e):
   re-runs the listing-level body gates against the fetched full text and warns
   (never blocks) if a manually-pasted URL would normally have been filtered —
   hunt/AUTO jobs already passed these at listing level.
3b. **Doomed-vacancy gate** (`hunter.apply_shared.run_doomed_gate` →
   `hunter.filters.assess_job_text`, Step 1.5f, docs/DOOMED_GATE_PLAN.md):
   deterministic (regex-only, zero LLM cost) second line of defense on the
   FULL job text — the listing-level filters (PR #110) can't see a hybrid/
   location/authorization requirement buried in the body. Two rule families:
   **HARD** (non-Poland onsite/hybrid tied to a US/Western-Europe/UK/Canada
   city or state, vetoed by an explicit fully-remote signal or a Wrocław/
   weekly-Warsaw-Kraków-hybrid mention; non-EU work authorization — W2/C2C/
   H1B/US citizen/green card/security clearance; a required language the
   candidate doesn't speak; a known AI-training/staffing-mill name in the
   BODY text — `ai_mill_body`, scans the full text for every
   `exclude_companies` entry incl. micro1.com apply links, because the
   company-field check is blind for Gmail-alert stubs where company is
   empty — exactly how the micro1 fronts QuikHireStaffing/HireFeed reached
   generation on 2026-07-06) writes a SKIP tracker row (`tracker.add_skipped`)
   and aborts generation for $0.00 — `DOOMED_GATE_HARD_ACTION=skip` (default).
   **SOFT** (primary stack isn't the candidate's — e.g. Vue/Svelte/Ember-first
   with neither Angular nor React in the requirements) warns in Telegram and
   generation continues. Force-mode (`skip_dedup`) and manual paste always
   degrade HARD to warn (the owner explicitly said generate this one); a
   HARD-but-degraded or SOFT finding surfaces in one Telegram message with
   the rule + a short evidence quote. `DOOMED_GATE_ENABLED`/
   `DOOMED_GATE_HARD_ACTION` gate/downgrade the whole thing without touching
   listing-level filters. Calibrated against ~450 real postings + a live
   Google Sheet sample — see `docs/DOOMED_GATE_CALIBRATION.md`.
4. LLM call: `candidate_profile.md` + `generation_rules.md` + job text -> `content.json`
4a. **ATS keyword loop** (`_ats_check_loop`, deterministic): regex keyword check
   against the posting; the resume is rewritten ONLY while *actionable* keywords are
   missing (up to 5 rounds: 2 honest → 1 soft → 2 aggressive). Early exit as soon as
   the filtered missing-keyword list is empty — at keyword=100% the combined score is
   capped by TF-IDF, which no rewrite moves (prod data: 88% of runs used to burn all
   5 rewrites there). No LLM review runs inside the loop; the independent LLM scoring
   moved to the post-render verdict (step 7a).
5. Cover letter self-review loop (up to 3 LLM rounds)
5a. **Content scrubs** (`apply_shared`, run in BOTH API and CLI pipelines): after
   sanitize — `_strip_compliance_claims` (employer's DORA/RODO/ISO… credentials never
   claimed as the candidate's; API only), `_strip_prestige_claims` (fabricated
   "Fortune 500"/"top-tier"/"blue-chip" client claims removed from summary/skills/
   bullets/about-me in EN+PL — *unless the term actually appears in the job posting*),
   `_dedup_skill_glosses` (collapse "term / synonym" pairs the ATS keyword mirroring
   leaves in skills, e.g. "Performance Optimization / Performance optimisation" — keeps
   the first side; genuinely different "A / B" entries like "OpenShift / container
   platforms" are kept). In the CLI pipeline any scrub fix rewrites content.json and
   regenerates the docs.
5a-bis. **Claim judge** (`hunter.claim_judge`, runs in BOTH pipelines after the scrubs,
   BEFORE the language gate; toggled by `JUDGE_ENABLED`): a second cheap model (`JUDGE_MODEL`,
   Haiku) verifies every generated claim (summary, skills, bullets, cover letters, about-me;
   `_en` + `_pl`) against the candidate profile + job posting and returns a structured
   violations list (`fabrication`/`exaggeration`/`style`). Each finding's `quote` must be a
   verbatim substring of the named field — non-verbatim findings are dropped, neutralising
   judge hallucinations. The whole stage is orchestrated by `run_judge_stage(content,
   job_text, base_cv, *, enabled, mode)` (pure logic; the pipelines own notify + block).
   **Only `fabrication` is auto-repaired** (high-precision: absent from BOTH profile and
   posting, quote-validated); `exaggeration` is a judgment call (a tool genuinely in the
   profile can be mis-flagged) so it is surfaced (Telegram) but NOT auto-dropped until the
   prompt is tuned (plan M4); `style` is report-only (the gloss-dedup owns it). Repair:
   deterministic clause-drop first (keeps the honest preceding clause via connector-aware
   boundaries), single targeted LLM rewrite for fields a drop would empty; rejected if it
   worsens `validate_content` (role-count guard). `JUDGE_MODE` stages the rollout: `report`
   (write `judge_report.json` only — **no content change**), `warn` (repair fabrications +
   Telegram notify), `block` (+abort delivery when a fabrication survives — API `sys.exit(0)`,
   CLI delete-docs+return). Best-effort: any judge failure logs a warning and continues.
   Verify a generated CV without regenerating it via `tools/preview_judge.py content.json
   [job.txt]` (one Haiku call).
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
7a. **PDF roundtrip + final ATS verdict** (both pipelines): deterministic re-score of
   the text extracted from the rendered EN CV PDF (+ NBSP self-heal on big deltas),
   then ONE independent `JUDGE_MODEL` (Haiku) call scores that same PDF text against
   the posting (`ats_pdf_roundtrip.run_llm_verdict`, gated by `ATS_VERDICT_ENABLED`).
   The verdict — from a model that did NOT write the resume, on what a real ATS
   actually parses — is stored as `ats_verdict` in content.json, stamped on the
   tracker row (`tracker.set_ats_verdict`; the row exists since Step 7/8), and is
   the **only** ATS number shown in Telegram / the tracker "ATS %" column (the
   generator's self-score stays in content.json for diagnostics only — see M4
   below). The Telegram success message also carries the verdict's
   `gap_report` as its own line (`ats_pdf_roundtrip.format_gap_report`,
   trimmed + HTML-escaped; CLI gets it via `format_verdict`) so the owner
   sees WHY the score isn't higher, not just the number. The Sheet column-N cell is written later by the bot process (step 9
   below): `mirror_new_row` reads `ats_verdict` from the DB after the A–K append.
   Informational only; never blocks delivery.
7b. **Verdict refine loop** (`hunter.verdict_refine.refine_loop`, both pipelines,
   docs/VERDICT_REFINE_PLAN.md): if the Step 7a verdict is below
   `ATS_VERDICT_TARGET` and `ATS_VERDICT_MAX_REFINES > 0` (default **3** —
   owner decision 2026-07-07: two honest passes, then one stretch), rewrite
   `resume_en` against the verdict's own `missing_keywords`/`recommendations`
   (deterministically dropping unfixable ones — location/relocation/hybrid/
   on-site/cover-note/LinkedIn/years-of-experience — via `build_refine_feedback`),
   re-render, and re-verdict, for up to `ATS_VERDICT_MAX_REFINES` escalating
   rounds: **rounds 1–2 (honest)** — only candidate_profile.md-supported facts,
   nothing new; **round 3+ (stretch)** — may ADD posting technologies absent
   from the profile as plain Skills/summary entries (no "familiar with"
   hedging), every addition also appended to `content["to_learn"]` (and, since
   the tracker row already exists by this point — Step 7 — stamped post-hoc
   on the row via `tracker.set_to_learn(url, ...)`, gated on the value actually
   changing vs. before the loop; same contract as the verdict stamp), optionally
   woven into ONE flexible Altoros client project (2018–2022: E-commerce/
   Insurance/Healthcare/Grant Management), NEVER into the recent/verifiable
   employers (Atruvia, Fairmarkit, Intel, SII, SolbegSoft) and never inventing
   employers/projects/metrics/years on any round. Each round re-runs the
   pipeline's own safety stages (sanitize, compliance/prestige/gloss scrubs,
   claim judge capped to `warn`, language gate) before re-rendering — the
   re-render itself passes `--no-tracker` and never `--force` (own
   `build_generate_docs_cmd` call, NOT the Step 7 command): the tracker row
   already exists, so a force-mode apply must not DELETE+INSERT it on every
   round/rollback (new sync ID, false Re-application flag). **Keep-best guard:**
   a round is accepted only if the new verdict is strictly higher than the
   current best; otherwise content.json + the rendered docs are rolled back to
   the pre-round version — regression is impossible by construction. If a PL
   posting's best round after the loop differs from the input (at least one
   round accepted) the PL CV is mirrored from the final `resume_en` exactly
   ONCE, after the loop (not per round — a translate call on a rolled-back
   round is wasted spend), with one extra local re-render. In the CLI pipeline
   the loop is silently skipped (with a log line) when `LLM_API_KEY` is unset,
   since the rewrite call goes through the API regardless of how the base CV
   was generated. `ATS_VERDICT_MAX_REFINES=0` reproduces the old one-shot-verdict
   behaviour byte-for-byte. **Cost re-stamp:** after the verdict block the API
   pipeline re-prices the full usage log (verdict call + every refine round,
   including rolled-back ones) and re-stamps the tracker row via
   `tracker.set_cost(url, total_usd)` — the row was created in Step 7 with the
   Step 6.5 (pre-verdict, pre-refine) figure, which the loop can more than
   double; without the re-stamp the Sheet column M systematically understated
   real spend (2026-07-06: recorded ~$2 vs ~$6 actual).
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

### Dual-apply (A/B model comparison) — `hunter/dual_apply.py`
Toggled via `/dual on`/`/dual off` (DB key `dual_apply_enabled`; shown in `/status`).
When ON, after the **primary (boevoy)** apply finishes successfully, `apply_agent.main()`
calls `run_shadow(folder)`: a second generation with the **shadow** profile
(`DUAL_SHADOW_PROFILE`, default `deepseek-v3`) into `{Company}/{shadow}/`. The shadow
reuses the saved `job_posting.txt` (no re-fetch) and the same pipeline building blocks
(`call_llm` → `_ats_check_loop` → scrubs → lang gate → `generate_docs --no-tracker`),
forcing the shadow model for every step via `llm_profiles.set_override()`. It is
**comparison-only**: NO tracker row, NO Telegram, NO Sheets mirror. After
generate_docs the shadow gets its own **independent PDF verdict**
(`ats_pdf_roundtrip.run_llm_verdict` on the shadow's rendered EN CV PDF — always
the Anthropic `JUDGE_*` judge, unaffected by `set_override()`, so primary and
shadow are scored by the SAME yardstick); it is persisted in the shadow
content.json and preferred for the filename suffix. Rendered CV/CL
filenames carry that score (`..._EN_ats91.pdf`; falls back to the deterministic
`ats_check` score when the verdict is unavailable). Both pipelines (`main_api`
/ `main_cli`) now return the output folder on success so the single hook in `main()`
covers CLI (Sonnet via Pro subscription) and API alike. Best-effort throughout — any
shadow failure logs and returns; the real application is never touched.

**Drive upload:** the shadow has no tracker row, so it can't ride the normal
apply→tracker→Drive hook. `run_shadow()` calls
`gdrive_sync.upload_shadow_folder(primary_folder, sub)` directly at the end of
`_generate_shadow()` (best-effort, gated by `GDRIVE_ENABLED`), nesting it under the
primary's company folder: `Job Hunter/{date}/{company}/{shadow_name}/`. Because the
shadow also has no Drive-URL tracker column to dedup against, `/gdrive_upload_missing`
(`gdrive_sync.upload_missing_folders`) independently scans every locally-present
company folder for a subdirectory matching a known `llm_profiles.PROFILES` name and
uploads it via `_upload_shadow_subfolders()` — idempotent (Drive upserts by name) and
runs regardless of whether the company folder itself was already uploaded, so a
backfill catches shadow sets generated before this existed. Reported separately in the
command's reply (`shadow_uploaded` count, `shadow_errors` list).

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
| 15 | Cost $ | Per-vacancy LLM USD spend (API mode). Written at row creation with the Step 6.5 figure, then **re-stamped post-hoc** (`tracker.set_cost`) after the verdict + refine loop so it covers the FULL run (verdict call, refine rewrite rounds incl. rollbacks, PL mirror). Blank for CLI mode (Pro subscription, no per-token visibility) and for pre-tracking rows. Mirrored to Sheet column **M** by `hunter.cost_writer` — separate writer (not part of the A–K push), parallel to `sent_normalizer` on column L. |
| — | ATS Verdict (`ats_verdict` DB column) | Independent PDF-verdict score (0–100): one `JUDGE_MODEL` (Haiku) call over the text extracted from the rendered EN CV PDF. Stamped post-hoc by `tracker.set_ats_verdict` (apply Step 7.7; the row already exists). NULL = no verdict. Mirrored to Sheet column **N** by `hunter.verdict_writer` when the bot-process `mirror_new_row` runs (the verdict is in the DB by then); `tools/sync_verdicts.py` backfills misses. Four non-overlapping Sheet writers: A–K main push, L sent_normalizer, M cost_writer, N verdict_writer. |

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

## LinkedIn Posts Scout (standalone, owner's desktop only)

**What it is:** many vacancies never reach LinkedIn Jobs — recruiters post them as
ordinary feed content ("We're hiring an Angular dev — DM me"). `linkedin_scout/` is a
standalone script (NOT part of `hunter/`, NOT in the Docker image, NOT on the bot's
schedule — the SCRAPING never runs inside the bot process) that scrapes LinkedIn
content-search + the home feed for candidate hiring posts. It never sends Telegram
directly and never runs the apply pipeline itself (owner decision 2026-07-08: "this is
just another job source, like the other 21"). Instead it relays a match to the bot as a
`/scoutfound <payload>` Telegram command, sent through the **owner's own Telegram user
session** (Telethon/MTProto, NOT the bot's token — see "Why a Telegram command" below).
`hunter/commands/scoutfound.py` receives it and queues it into `hunter/sources/
linkedin_scout_relay.py` (a tiny, scrape-free source INSIDE the bot), which drains that
queue on the bot's own hunt cycle — from there a candidate goes through the exact same
pipeline as any other source: central
filters, the doomed-vacancy gate, tracker dedup, and normal `AUTO_APPLY` handling — NOT
`manual_only` (owner decision 2026-07-08: "we dropped confirmation cards long ago, I
never wait for them — there's already a full check pipeline other job-board postings
go through, I want these to go through it too"). A HARD doomed-gate finding still
aborts generation for $0.00 exactly like any other source; paste-mode does NOT
downgrade HARD findings (only genuine `/force` does), so a bad heuristic match is
still caught downstream, just not by a human looking at a card first. Apply routes
through the paste flow (the URL used for dedup/routing stays a synthetic key — see
below) in BOTH the AUTO_APPLY and manual-card code paths, using the saved post text
automatically — no manual re-paste needed either way. Separately, when a post's DOM
exposes a real permalink (see "Post permalinks" below), it's carried along
convenience-only and shown in the pre-apply Telegram notification.

**Why standalone:** an earlier design ran this inside the bot's Docker container
(`docs/LINKEDIN_POSTS_SOURCE_PLAN.md`, branch `feat/linkedin-posts-source`, PR #114).
Rejected: a datacenter IP + no display + a bare container fingerprint are exactly what
gets a LinkedIn session flagged — and a flagged session also breaks the bot's own
`LINKEDIN_STORAGE_STATE`-based detail-page fetches. This version runs on the owner's
own Windows desktop, on his residential IP, via Task Scheduler, while he's away from
the keyboard — see `docs/LINKEDIN_POSTS_SCOUT_TASK.md` for the full spec.

**Why a Telegram command instead of a shared file** (owner discovery 2026-07-08,
post-deploy): the bot auto-deploys to its own server — it does NOT share a filesystem
with this script's Windows desktop, so an earlier local-queue-file design (the scout
writing `pending_candidates.json` directly) could never actually reach the bot. Telegram
bridges the two machines instead. **Why the owner's own Telegram USER session and not
the bot's own token:** Telegram never delivers a bot's own outgoing `sendMessage` calls
back to that same bot as an incoming update — there is no way to make the bot's polling
`Application` react to something it sent to itself. The command has to come from a
genuinely different account (the owner's), which requires a real Telegram user login
(Telethon/MTProto — `tools/telegram_user_login.py`), not just the existing bot token.

**Two independent tracks** (owner decision 2026-07-07, after M2 shipped):
- `--track search`: content-search by keyword, rotating one keyword per run
  (`LINKEDIN_SCOUT_KEYWORDS`).
- `--track feed`: scrolls the plain home feed, no keyword — relies on the same
  `is_hiring_post()` gate (which already requires "angular" to be prominent) to narrow
  results.

Each track owns its own persistent Chrome profile + circuit-breaker state file, so a
trip on one never silences the other.

**Modules** (`linkedin_scout/`):
| File | Role |
|---|---|
| `heuristics.py` | `is_hiring_post()` (stack + hiring-signal + candidate-side/spam/US-staffing/India-staffing negatives + Angular-prominence gate), `check_location()` (three-way gate, reuses `hunter.filters._is_unwanted_onsite_location` — not duplicated) |
| `parser.py` | `parse_posts()` — splits captured `innerText` into (author, body) blocks on "Feed post" markers |
| `seen_store.py` | `dedup_key()` + `SeenStore` — plain JSON, atomic write, independent of `tracker.db` |
| `state.py` | `ScoutState` — circuit-breaker trip flag + round-robin keyword rotation, one JSON file per track |
| `browser.py` | Playwright mechanics: persistent Chrome profile, cookie re-seeding, shadow-DOM-aware extraction JS (incl. `LI_PERMALINK::` marker capture), `...`-menu permalink capture for M1 candidates (`_fetch_menu_permalinks`/`_copy_link_via_menu`), `scout_keyword()` (off-screen window) / `scout_feed()` (long randomized scroll + plateau stop), `run_once()`/`run_feed_once()` (circuit breaker + M1 filter wiring) |
| `telegram_relay.py` | `send_candidates()` — only relays a candidate that has a captured `permalink` (owner decision 2026-07-08: a candidate with no real clickable link is held back, NOT marked seen, so a later run gets another shot at it instead of the post being lost silently), then dedup-before-send (reuses `seen_store.dedup_key`), builds a base64(JSON) payload per candidate (`build_payload`, capped ~3000 raw chars to stay under Telegram's 4096-char command limit) and sends `/scoutfound <payload>` via Telethon, using the OWNER'S OWN Telegram user session (`TELEGRAM_API_ID`/`_HASH`/`TELEGRAM_USER_SESSION`/`TELEGRAM_BOT_USERNAME`) — NOT the bot's token |
| `notify.py` | Direct Telegram `sendMessage` via the bot's own token (no `Application`/polling) — only used for `--dry-run` console preview formatting now; real runs go through `telegram_relay.py` instead |
| `run.py` | CLI entry point + Task Scheduler glue: `--track`, `--reset`, `--dry-run`, skip-chance + jitter |

**Bot-side relay** (`hunter/commands/scoutfound.py` + `hunter/sources/
linkedin_scout_relay.py`, inside the main repo — this piece IS in Docker/the bot
process, since it does zero scraping): the `/scoutfound` command handler
(`cmd_scoutfound`) only accepts the command from the configured `TELEGRAM_CHAT_ID` (the
owner's own chat — this ultimately feeds `AUTO_APPLY`, real LLM spend, so it must not
be triggerable by anyone else), decodes the base64(JSON) payload, and calls
`linkedin_scout_relay.append_to_queue()`, which writes into `pending_candidates.json`
**on the bot's own filesystem** (a `threading.Lock` guards this against
`LinkedInScoutRelaySource.search()`'s concurrent read+clear on the hunt cycle — both
run inside this one process's thread pool now, so no cross-machine race is possible).
`search()` reads+drains that same, now-local, file into normal `Job` objects (synthetic
dedup-key URL `https://linkedin.com/scout-posts/#p...`, deliberately never a real
LinkedIn URL — that would collide with `LinkedInSource.matches_url`'s host-based, not
path-based, dispatch), registered in `ALL_SOURCES` behind `LINKEDIN_SCOUT_RELAY_ENABLED`
(default true) and in the fetch-dispatch roster.

**Post permalinks** (owner discovery 2026-07-08, live-verified — an earlier probe found
none reachable, which was wrong): some posts (LinkedIn "share"-type, at least) wrap
their body text in a real `<a href="https://www.linkedin.com/feed/update/urn:li:
share:...">` already present in the DOM, no click needed. `browser.py`'s `_EXTRACT_JS`
emits a `LI_PERMALINK::<url>` marker line right where that anchor sits in the
document-order text stream; `parser.py::parse_posts()` detects and strips it into
`ParsedPost.permalink` (best-effort, `None` when absent, keeps the first marker per
post block). LinkedIn also exposes a `Copy link to post` item in every post's `...`
menu (works on every post, unlike the DOM-anchor which is share-type-only, per a second
owner discovery the same day) — `browser._fetch_menu_permalinks()` runs right before
the persistent Chrome context closes (same page still open, one call per `scout_keyword`/
`scout_feed` invocation via a `permalink_sink` out-parameter on `_open_scroll_extract`,
kept as an out-param specifically so `scout_keyword()`/`scout_feed()`'s existing `str`
return type — and every test that monkeypatches them — didn't have to change) and,
for each M1 candidate that didn't already get a DOM-marker permalink, best-effort
clicks `...` → `Copy link to post` and reads the clipboard (`_copy_link_via_menu()`,
capped at `_MAX_MENU_PERMALINK_ATTEMPTS`/run — clicking is slower and adds anti-bot
surface, so it's spent only on posts that already passed `is_hiring_post()`/
`check_location()`, never on every post on the page). Either source threads through
`ScoutCandidate.permalink` → `telegram_relay.build_payload()` → `job.raw["permalink"]`
on the bot side (the synthetic `job.url` above is untouched — this is convenience-only,
never used for dedup/fetch/routing) → an extra "🔗 Post:" line in the pre-apply Telegram
notification (`hunter/main.py::_auto_apply_all`, source-agnostic — any Job with
`raw["permalink"]` gets it) and in `notify.py`'s `--dry-run` preview. The `...`-menu
selectors (`_POST_CONTAINER_SELECTORS`/`_MENU_BUTTON_SELECTORS`) are best-effort and
UNVERIFIED against a live session, same caveat as every other DOM-shape assumption in
this module — a failed lookup just skips that candidate's permalink, never blocks the
run. It is NOT `manual_only` (new
`BaseSource.manual_only: bool = False` attribute, added for any future source that DOES
want to force a card — `hunter/main.py`'s ACT step partitions `new_jobs` on it before
the `AUTO_APPLY` branch, currently a no-op since nothing sets it True). Paste-flow
wiring exists on BOTH code paths since either could run depending on
`AUTO_APPLY`: `hunter.services.apply_service.run_apply_agent_subprocess` (the
`AUTO_APPLY=true` path) detects `job.raw["post_text"]`, writes it to a temp file,
passes `--paste-file` (cleaned up in a `finally`); `hunter/commands/url_message.py::
_handle_apply` (the manual Telegram-card path, used when `AUTO_APPLY=false`) does the
same via `_run_apply_agent(url, paste_file=...)`.

**Safety rails:** circuit breaker (any login/checkpoint/authwall/captcha signal aborts
immediately, trips state, sends exactly one Telegram alert, every later run no-ops
until `--reset`); ~30% skip-chance + 0-45min jitter per invocation; headed real Chrome
with stealth flags (never headless — that got flagged within 2-3 loads in the original
live probe). Search track originally did ONE rotation-keyword per run (full coverage
over several days); owner decision 2026-07-08 changed `run_once()` to search the
ENTIRE `LINKEDIN_SCOUT_KEYWORDS` list every invocation instead, in a freshly
randomized order each call (`random.shuffle`, same owner decision) with a
10-30s jittered pause between keywords, circuit breaker still aborting the
whole run immediately on a trip — no further keywords attempted. See
`linkedin_scout/README.md` for the Task Scheduler setup and the full
safety-rail rationale.

**Verification status (as of 2026-07-07):** the full launch → cookie-seed →
navigate → scroll → extract pipeline has been run end-to-end against a REAL Chrome
browser using local `file://` HTML fixtures with actual open shadow DOM (zero network,
no LinkedIn contact) — see `tests/test_linkedin_scout_extract_integration.py`. This
caught and fixed two real bugs a mocked unit test couldn't have: (1) the original plan
claimed `document.body.innerText` renders shadow DOM content — verified FALSE against
real Chrome; (2) cookies injected via Playwright's `add_cookies()` on a persistent
context do NOT survive to the next process launch (checked the on-disk SQLite cookie
store directly) — fixed by re-seeding every run instead of "once ever". What is still
NOT verified: the actual live LinkedIn DOM shape and its anti-bot behavior — that
requires a real run on the owner's own machine, which this repo cannot do.

## Git Workflow

- **Active branch:** `develop` — all changes go here
- `master` is production-stable (60+ commits behind develop)
- Always commit on `develop`, never force-push `master`

---

## Important Rules for Agents

- **Never commit** `.env`, `tracker.xlsx`, `Applications/`, `backups/`, `gmail_token.json`, `gsheets_token.json`, `gsheets_credentials.json`, and the personal prompt files (`prompts/candidate_profile.md`, `prompts/base_cv_*.md`, `prompts/candidate/`, `prompts/examples/` — gitignored; repo is public, only `.example` templates are tracked)
- Always test syntax after edits: `python -m compileall .`
- Run `ruff check .` before committing — CI gates on it (config in `pyproject.toml`,
  covers the whole repo: `hunter/` + entry scripts + `tests/` + `tools/`)
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

7. ~~**tracker.py is ~980 lines.** Multiple functions re-open and re-parse the entire Excel file per call.~~ ✅ Resolved by the Phase 5 SQLite migration (2026-05-27): tracker.py no longer imports openpyxl at all — every read/write goes through `hunter.db.get_db()` (SQLite, WAL). No per-call workbook re-parse remains. (tracker.py is still ~1050 lines, but that's surface area, not the Excel-reparse cost the issue described.)

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
| NoFluffJobs | 2026-06 | OK | Listing POST `/api/search/posting`; detail `/api/posting/{slug}` schema changed (no more `sections` — content moved to `details.description` / `requirements.description` / `specs.dailyTasks`, salary to `essentials.originalSalary`, company name to `company.name`). `_format_posting_text` now multi-path with legacy fallback |
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
| JobLeads | 2026-06 | PARTIAL | Listing OK (`data-testid="search-job-card"`, relative hrefs — re-verified 2026-06-15); detail pages Cloudflare-blocked → MANUAL flow. Note: server ignores `q=` param (generic results), so few survive the frontend filter |
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
| 2026-07-08 | sonnet | LinkedIn Scout: two follow-up fixes after a live `--track search` run + owner audit of real relayed Telegram messages (branch fix/scout-require-permalink). **Fix 1 — permalink-required relay** (`telegram_relay.send_candidates`): owner decision that only candidates with a real, captured post permalink should ever reach the bot — a candidate that passed the M1 heuristic gate but got no DOM-marker/menu-click link is now held back before the seen-store dedup check, and deliberately NOT marked seen, so a later run (better DOM luck, or a selector fix) gets another shot at the same post instead of it being silently lost forever. **Fix 2 — India-staffing/recruiter-spam gate** (`heuristics.is_hiring_post`): the owner pasted 3 real `/scoutfound` messages already sitting in the bot chat (Hyderabad "Full Stack Java Developer (Spring Boot + Angular)", Chennai "CTC: INR 15-27 LPA", "Remote (India Only)" with `Client ID:`/`Job ID:` staffing-agency codes) that should never have been relayed — root cause traced to `check_location()`'s anti-hybrid city list (`hunter.filters._is_unwanted_onsite_location`) only ever covering Western-commute-to-Wrocław cities, `US_STAFFING_RES` only recognizing US-specific markers (W2/C2C/H1B/state codes), and the "Remote (India Only)" case additionally short-circuiting `check_location` via its own explicit-remote-anywhere -> KEEP rule (so a location-list fix alone wouldn't have caught it — this had to be an `is_hiring_post` disqualifier). New `INDIA_STAFFING_RES` negative pattern set (CTC/LPA/Notice Period/₹/INR/Client ID:/Job ID:/"India Only"/major Indian cities — Hyderabad, Chennai, Bangalore, Bengaluru, Pune, Mumbai, Noida, Gurgaon, Gurugram, Kolkata, Ahmedabad, Kochi, Jaipur), checked alongside the existing `US_STAFFING_RES` negative. All 3 real examples verified rejected post-fix (decoded from their actual base64 Telegram payloads and run through the new `is_hiring_post` directly, not just via new unit tests). 4 new tests (1 `send_candidates` no-permalink-skip-not-seen + 3 India-staffing `is_hiring_post` real-example regressions, on top of updating existing `telegram_relay` tests whose default fixture candidate had no permalink). Full scout suite (160) green; `python -m compileall -q .` clean; `ruff check` clean (`ruff` CLI unavailable in this shell, verified via `python -m ruff`). |
| 2026-07-08 | sonnet | LinkedIn Scout: `...`-menu permalink capture as a second source (same feat/capture-post-permalink branch, same-day follow-up). Owner pointed out live that LinkedIn's per-post `...` control menu has a `Copy link to post` item that works on EVERY post — not just the "share"-type posts the DOM-anchor capture from the entry above catches — and asked (given the trade-off between coverage and extra clicks/anti-bot surface) to add it, scoped to M1 candidates only. New `browser._copy_link_via_menu(page, author, body)`: Playwright locators (which pierce open shadow roots, unlike XPath — so no need for the manual `_EXTRACT_JS` walker here) find a post container via a short list of best-effort selectors (`_POST_CONTAINER_SELECTORS`: `[data-urn]`, `[role="article"]`) filtered by a body-text snippet, then a control-menu button (`_MENU_BUTTON_SELECTORS`, several `aria-label` guesses), click it, click `Copy link to post`, read `navigator.clipboard.readText()`; any failure at any step logs and moves to the next selector/post rather than raising (same "never block the run" contract as the rest of this module). New `browser._fetch_menu_permalinks(page, raw_text)` runs this only for posts that pass `is_hiring_post()`+`check_location()` and don't already have a DOM-marker permalink, capped at `_MAX_MENU_PERMALINK_ATTEMPTS=5`/run. Wired into `_open_scroll_extract` via a new `permalink_sink: dict[str,str] | None = None` out-parameter (populated in-place, before `context.close()`, while the page is still live) — deliberately an out-param rather than a return-type change, so `scout_keyword()`/`scout_feed()` keep returning a plain `str` and every existing test that monkeypatches them with `lambda *a, **k: raw_text` needed zero changes; `context.grant_permissions(["clipboard-read", "clipboard-write"])` is requested only when a sink is supplied. `_filter_candidates()` gained a `menu_permalinks` parameter (keyed by `seen_store.dedup_key(author, body)`) that backfills `ScoutCandidate.permalink` when the DOM-marker one is `None` — the DOM-marker source always wins when both exist, since it cost zero extra clicks. `run_once()`/`run_feed_once()`'s internal `scout_call` closures now build a fresh sink dict per call and pass it through. Same downstream surfacing as the entry above (Telegram notification, dry-run preview) — no changes needed there, both already read `permalink` generically regardless of which capture path filled it. Explicitly flagged (module docstring + CLAUDE.md, same pattern as every other DOM-shape assumption in this file): the exact container/button selectors are UNVERIFIED against a live LinkedIn session and will likely need the owner's live-testing feedback to tighten, same as the shadow-DOM walker and the mouse-wheel fix earlier in this file's history. 21 new tests (`_filter_candidates` menu-dict backfill/DOM-wins/absent ×3, `_copy_link_via_menu` happy-path/no-container/non-linkedin-clipboard ×3, `_fetch_menu_permalinks` skip-marker/skip-non-hiring/capture/cap-at-max ×4, plus fake Playwright locator/page test doubles). Full suite (1903) green; ruff clean; compileall clean. |
| 2026-07-08 | sonnet | LinkedIn Scout: capture real post permalinks going forward (branch feat/capture-post-permalink), owner request after live-testing turned up that some LinkedIn posts DO expose a real, clickable permalink — contradicting an earlier probe finding of "no permalinks reachable" (that finding was accurate for the DOM shapes probed at the time; live testing on 2026-07-08 against a current session found that "share"-type posts wrap their body text in a real `<a href="https://www.linkedin.com/feed/update/urn:li:share:...">` already in the DOM, no click needed). Threaded through the whole pipeline, best-effort (`None` when a post has none): `browser.py`'s `_EXTRACT_JS` now detects an `/feed/update/` anchor in a shadow-free subtree and emits a `LI_PERMALINK::<href>` marker line right before that subtree's own text; `parser.py::parse_posts()` detects and strips the marker per post block into new `ParsedPost.permalink` (keeps the first marker if a block somehow has more than one); `ScoutCandidate` gained a matching `permalink` field, populated in `browser._filter_candidates()`; `telegram_relay.build_payload()` adds it to the JSON payload sent via `/scoutfound`; `hunter/sources/linkedin_scout_relay.py::_record_to_job()` puts it in `job.raw["permalink"]` — **`job.url` deliberately stays the synthetic dedup key untouched** (a real linkedin.com URL there would collide with `LinkedInSource.matches_url`'s host-based, not path-based, dispatch precedence). Surfaced to the owner in two places: `hunter/main.py::_auto_apply_all`'s pre-apply Telegram notification gets an extra "🔗 Post:" line when `job.raw["permalink"]` is set (source-agnostic — checks `job.raw`, not hardcoded to this one source) so the owner can click through while the CV generates; `notify.py::format_message()` (the `--dry-run` console preview) shows it the same way `author_profile_url` already did. Convenience-only throughout — never touches dedup, fetch routing, or the paste-flow apply mechanism. 11 new tests (parser marker-extraction/strip/first-wins ×3, browser candidate-threading ×2, telegram_relay payload ×2, relay-source raw-carry ×1, main.py notification ×2, no test needed for notify.py — same pattern as the existing `author_profile_url` line). Full suite (1893) green; ruff clean; compileall clean. |
| 2026-07-08 | sonnet | LinkedIn Scout: PR #123 code review + delivery mechanism rebuilt around Telegram instead of a shared file (branch feat/linkedin-posts-scout-m2, continuing PR #122's leftover unmerged commits — PR #122 had been merged mid-branch, so a fresh PR #123 was opened for everything after). **Code review** (8-angle parallel finder agents + verification against actual code) surfaced 10 findings on the queue-file relay design from the previous entry; 3 confirmed real bugs: (1) `hunter/tracker.py::get_failed_jobs()` only excludes the `paste://no-url` sentinel, not the scout's synthetic URL — a failed scout job's retry rebuilt a bare `Job` with no `raw`/`post_text`, guaranteeing every retry attempt failed by trying to fetch a URL that raises by design; (2) `hunter/validation.py`'s pre-existing `MIN_JOB_TEXT_LEN=300` too-short abort applies unchanged to scout paste text, but typical LinkedIn hiring posts are well under 300 chars — silently defeating the feature for most real posts; (3) a straight-up architecture flaw surfaced mid-review: **the owner confirmed the bot auto-deploys to its own server**, meaning it does NOT share a filesystem with `linkedin_scout/`'s Windows desktop — the previous design's `pending_candidates.json` queue file, written locally by the scout and expected to be read by the bot, could never actually bridge the two machines. Findings (1)/(2) remain open (not yet fixed — flagged for a follow-up); finding (3) is what this entry's rework addresses. **Delivery mechanism rebuilt**: `linkedin_scout/queue_writer.py` deleted; new `linkedin_scout/telegram_relay.py` sends a `/scoutfound <base64(json)>` command through the OWNER'S OWN Telegram user session (Telethon/MTProto, new dependency) instead of writing a local file — Telegram bridges the two machines where a shared filesystem can't. This can't use the bot's own token: Telegram never delivers a bot's own outgoing messages back to itself as an incoming update, so the command must come from a genuinely different account. New `tools/telegram_user_login.py` (interactive phone+code+2FA login, mirrors `tools/linkedin_login.py`'s UX) creates the session file (`TELEGRAM_USER_SESSION`; needs `TELEGRAM_API_ID`/`_HASH` from https://my.telegram.org and `TELEGRAM_BOT_USERNAME` as the send target). Bot side: new `hunter/commands/scoutfound.py` (`/scoutfound` command, registered in `telegram_bot.py`) rejects anything not from the configured `TELEGRAM_CHAT_ID` (this ultimately triggers `AUTO_APPLY`/real LLM spend) and calls `hunter/sources/linkedin_scout_relay.py`'s new `append_to_queue()`, which writes `pending_candidates.json` **on the bot's own filesystem** — the relay source's existing `search()` (unchanged from the previous entry) now reads a file that's actually local to it. A `threading.Lock` (not the previous entry's naive read-then-clobber-write) guards `append_to_queue`/`search()` against each other, since both now run in the SAME process (the append via the async command handler's `asyncio.to_thread`, the drain via the hunt loop's own `asyncio.to_thread(source.search)`) — this also incidentally closes the queue-drain-race finding from the code review, which was a real concern under the old (wrong) cross-machine assumption. 26 new tests (`telegram_relay` ×7 incl. payload-encoding/truncation/dedup, `scoutfound` command ×5 incl. the chat-id rejection, `linkedin_scout_relay`'s `append_to_queue` + lock-guarded round-trip ×4) plus updated the `run.py` test that used to assert a queue-file write to instead assert `telegram_relay.send_candidates` gets called. Full suite (1880) green; ruff clean; compileall clean. **Known gaps still open**: findings (1) and (2) above (retry-path Job reconstruction, MIN_JOB_TEXT_LEN too-short abort) are real and unaddressed; a lower-priority dedup-collision risk (`md5(author + body[:200])`) and a few duplicated-code/efficiency cleanup items from the review are also still open. |
| 2026-07-08 | sonnet | LinkedIn Scout post-M5 follow-ups (same feat/linkedin-posts-scout-m2 branch/PR #122, live-tested on the owner's real machine). **Real-LinkedIn verification found 2 bugs the local-fixture tests couldn't catch:** (1) `page.mouse.wheel()` was a silent no-op — Playwright's mouse has no on-page position until `page.mouse.move()` is called at least once, so every scroll iteration in every real run had done nothing (owner's first two live runs showed identical before/after post counts: 8→8). Fixed with one `mouse.move()` to viewport-center right after page load; new regression fixture (`infinite_scroll.html`, content that only renders on a real 'scroll' event) proves the fix through the FULL `_open_scroll_extract` pipeline, not just a raw JS eval. Live-verified after the fix: feed track went 8→28 posts, then a full 10-minute session did 159 scrolls / 443 posts with zero anti-bot trips. (2) live LinkedIn briefly redirected to a password-reset checkpoint mid-scroll on a second rapid-succession live test — circuit breaker fired as designed (manually tripped both tracks' state as a precaution since the call bypassed `run_once`'s own trip wiring); owner's account was fine on manual check, session refresh + `--reset` resumed cleanly. **Feed track redesigned** for a long, slow, randomized scroll instead of a short fixed burst: `_open_scroll_extract` gained `max_duration_sec` (~10 min) + `plateau_limit` (stop once N scrolls add nothing new) + randomized per-scroll distance; `scout_keyword` (search) unchanged (3 fixed scrolls). **Schedule split** (owner: search hourly all day incl. jitter; feed only overnight 03:00-08:00 hourly + two fixed daytime runs at 13:00/18:00, always `--no-jitter`) — registered via 4 separate `schtasks` entries (`LinkedInScout-Search`, `-Feed-Night`, `-Feed-Day1`, `-Feed-Day2`), `/sc daily /st 03:00 /ri 60 /du 0006:00` for the overnight repeat. **Off-screen window** for the search track only (`--window-position=-3000,0`, `extra_chrome_args` param on `_open_scroll_extract`) — hourly popups no longer steal focus; feed stays on-screen since Chrome throttles occluded/backgrounded tabs and a 10-min session off-screen risked losing lazy-loaded content for no safety benefit. **RU heuristic support**: owner widened the search keyword rotation to EN+PL+RU (`LINKEDIN_SCOUT_KEYWORDS=angular,angular hiring,angular developer,angular praca zdalna,angular programista,angular разработчик,angular вакансия`); added matching RU patterns to `heuristics.py` (hiring signal: ищем/требуется/вакансия/набираем; candidate-side: ищу работу/в поиске работы; spam: курс/вебинар/буткемп; remote: удалённо/дистанционно). **Delivery mechanism replaced entirely** (owner: "this is just another job source, like the other 21" — the earlier M3 design of a direct plain-text Telegram notification was rejected as not matching how every other source works): `linkedin_scout/queue_writer.py` (new) writes matches to `linkedin_scout/pending_candidates.json` instead of calling Telegram directly; new `hunter/sources/linkedin_scout_relay.py` (inside the bot, zero scraping) drains that queue on the bot's own hunt cycle into normal `Job` objects (synthetic dedup-key URL, since no real LinkedIn post permalink is reachable), registered in `ALL_SOURCES`/the fetch-dispatch roster behind `LINKEDIN_SCOUT_RELAY_ENABLED` (default true) — 21→22 sources. New `BaseSource.manual_only: bool = False` attribute + a `hunter/main.py` ACT-step partition (manual-only jobs always get a Telegram card before the `AUTO_APPLY` branch) were built for this initially, then explicitly NOT applied to this source per a follow-up owner call ("we dropped confirmation cards long ago, I never wait for them — I want these candidates to go through the same check pipeline as other job-board postings, not a manual gate"): the doomed-vacancy gate + central filters are what catches a bad heuristic match now, not a human looking at a card. Paste-flow wiring (no real URL to fetch) was added on BOTH code paths since either could run depending on `AUTO_APPLY`: `hunter.services.apply_service.run_apply_agent_subprocess` (writes `job.raw["post_text"]` to a temp file, `--paste-file`, cleanup in `finally`) and `hunter/commands/url_message.py::_handle_apply` (same, for the manual-card path). Confirmed the doomed-gate's HARD-finding paste-mode downgrade only applies to genuine `/force` (a prior fix, already landed before this session), so routing through paste does NOT weaken the location/authorization/language screen for these auto-applied heuristic matches. 40 new/updated tests across `queue_writer`, the relay source, `main.py`'s manual_only partition mechanism (kept as a generic, currently-unused feature), `url_message`'s paste routing, `apply_service`'s paste-file wiring, plus 2 fetch-roster-count fixups (21→22) and 1 direct-Telegram-send test switched to a queue-file assertion. Full suite (1871) green; ruff clean; compileall clean. |
| 2026-07-07 | sonnet | LinkedIn Posts Scout M1-M5 (standalone `linkedin_scout/`, docs/LINKEDIN_POSTS_SCOUT_TASK.md, branches feat/linkedin-posts-scout-impl-v2 (M1, PR #120 merged) + feat/linkedin-posts-scout-m2 (M2-M5, PR #122)). Replaces the earlier server-side design (`docs/LINKEDIN_POSTS_SOURCE_PLAN.md`, branch feat/linkedin-posts-source, PR #114 — rejected: datacenter IP + no display + container fingerprint risked flagging the shared `LINKEDIN_STORAGE_STATE` session that also powers the bot's own LinkedIn detail fetches) with a script that runs on the owner's own Windows desktop, his residential IP, via Task Scheduler. **M1** (pure logic, no browser): `heuristics.py` (`is_hiring_post` — stack/hiring-signal/candidate-side/spam/US-staffing regex families + Angular-prominence gate + `szukam`≠`szukamy` distinction; `check_location` — three-way gate reusing `hunter.filters._is_unwanted_onsite_location`), `parser.py` (`parse_posts` — splits captured innerText on "Feed post" markers), `seen_store.py` (dedup + atomic JSON). **M2** (Playwright): `state.py` (`ScoutState` circuit breaker + keyword rotation), `browser.py` (persistent Chrome profile, stealth flags, shadow-DOM extraction, `AntiBotDetected`). Mid-M2, owner requested a SECOND independent track: `scout_feed()`/`run_feed_once()` scrolls the plain home feed (no keyword) alongside the original `scout_keyword()`/`run_once()` search track — separate profile dirs + state files so a trip on one never silences the other. **M3**: `notify.py` — direct Telegram `sendMessage` (no bot Application/polling), message formatting, dedup-before-send. **M4**: `run.py` — CLI (`--track {search,feed}`, `--reset`, `--dry-run`, `--no-jitter`), skip-chance (~30%) + jitter (0-45min) per task spec §3.5, UTF-8 stdout reconfigure (Windows cp1252 crashed on the 🔎/👤/🕒 in dry-run output). **Post-M4 real-Chrome verification** (owner asked to verify as thoroughly as possible before M5): ran the full launch→seed→navigate→scroll→extract pipeline against REAL Chrome (`channel="chrome"`) using local `file://` HTML fixtures with actual open shadow DOM — zero network, no LinkedIn contact. Found and fixed 2 bugs no mocked unit test could have caught: (1) the source plan's claim that `document.body.innerText` renders shadow DOM content is FALSE on real Chrome — the shadow-walk in `_EXTRACT_JS` is the primary extraction mechanism now, not a "safety net"; (2) the shadow-walk fallback used `.textContent` (no line breaks — would have made every post unparseable by the "Feed post"-marker splitter), rewritten to call `.innerText` per subtree; (3) cookies injected via `context.add_cookies()` on a persistent Chrome context do NOT survive to the next process launch (checked the on-disk SQLite Cookies DB directly — 0 rows after close+reopen, despite being correctly sent on real requests during the live session) — `seed_profile_if_needed`'s "seed once" design renamed to `seed_profile_cookies`, now re-seeds every run. New `tests/test_linkedin_scout_extract_integration.py` (real-Chrome, auto-skips if Chrome unavailable) pins both fixes as regressions. **M5**: `linkedin_scout/README.md` (prerequisites, exact `schtasks` registration commands for both tracks, safety-rail summary), this CLAUDE.md section + Repository Layout entry, note in `docs/LINKEDIN_POSTS_SOURCE_PLAN.md` that PR #114's server-side variant is superseded (not closed/merged). 106 total linkedin_scout tests (heuristics/parser/seen_store/state/browser/notify/run/real-Chrome-integration) green; ruff clean; compileall clean. Explicitly NOT verified: the actual live LinkedIn DOM shape and its anti-bot behavior against a real session — that step is the owner's own machine, per the task spec. |
| 2026-07-07 | sonnet | Doomed-gate paste-path extension (branch fix/doomed-gate-paste-path, docs/DOOMED_GATE_PASTE_PLAN.md, on top of merged PR #116/#117). Owner audit of tracker rows 2026-07-02…07-06 found 3 "should have been filtered" applies that slipped through specifically because they were manually pasted: `screen_job_text`'s "deliberately does NOT enforce the title-keyword whitelist" contract (manual paste = intentional override) let Santander (`.NET Developer (Angular)`, 72% ATS) and QuantumBlackMcKinsey (`Software Engineer - QuantumBlack, AI by McKinsey`, 82%) through untouched, and Comarch ×3 (`гибрид не вроцлав`) slipped past the location gate because the body never says "hybrid"/"onsite" at all — only "Comarch Warsaw, Mazowieckie, Poland" in the header. Fixes in `hunter/filters.py`: new HARD `title_exclude_pattern` (reuses listing-level `_matches_exclude_pattern` against the known/guessed title — catches Santander) and new SOFT `off_domain_title` (reuses `_matches_title_keywords` inverted — catches QuantumBlack), both fed by a new best-effort `_guess_title_from_text()` (first meaningful line, skips nav boilerplate) used only when no explicit title is known. Comarch is NOT caught: an earlier `header_location_anti_hybrid_city` SOFT rule (bare anti-hybrid city near the top of the text) was implemented then immediately reverted after calibration showed it also fired on Fairmarkit — a real, Sent, 98%-ATS Warsaw-office EU role with no hybrid language of its own; a bare city mention can't be told apart from "this is just the office address" even at SOFT, so it was dropped rather than shipped as noise. `hunter/apply_shared.py`: `run_doomed_gate`'s `is_manual_override` param split into `is_force_override` — only `/force` (`skip_dedup`) still degrades a HARD finding to warn; a plain manual paste is no longer an automatic override (real $ was wasted on pasted postings a HARD rule would have caught). Incidental but real bug fix in `hunter/services/apply_service.py`: `--company`/`--title` used to be passed to the apply subprocess ONLY for `jobleads.com` URLs, so `run_doomed_gate` always saw `title=""` for every other auto-hunt job — meaning the pre-existing title-dependent `_is_unwanted_fullstack` check (and now `title_exclude_pattern`) had silently never fired for any non-JobLeads job in production. Now passed for any job with a known title. Recalibration (`tools/screen_calibrate.py --live`, extended with a `_title_index` so offline replay uses real Sheet titles instead of always guessing — docs/DOOMED_GATE_PASTE_CALIBRATION.md) found 0 HARD false positives; the apply_service.py fix retroactively activated `is_unwanted_fullstack`/`title_exclude_pattern` on 3 old Sent rows (Unide ×2, BCFSoftware) that only ever got through via the plumbing gap — documented as pre-existing, already-owner-approved policy (`_PRE_EXISTING_POLICY_RULES`, same treatment as the M4 Micro1 pre-policy bucket) rather than "fixed" by loosening a correct pattern. 14 new/updated tests across `test_doomed_gate.py`, `test_doomed_gate_wiring.py`, `test_apply_service.py`, `test_filters_unwanted_2026_06.py`; full suite 1721 green; ruff clean. |
| 2026-07-07 | fable | Public-repo prep 2: personal data out of git (same branch). All candidate-personal prompt files untracked + gitignored (`prompts/candidate_profile.md`, 5× `base_cv_*.md`, `prompts/candidate/`, `prompts/examples/`); repo now ships `.example` templates (`candidate_profile.example.md`, `base_cv_angular.example.md` — same section structure the resume_sanitizer parses) + `prompts/README.md` (system-vs-personal split, setup). Safe because tests never read the real files (they patch PROMPTS_DIR) and loaders degrade gracefully (missing base CV/examples → warning + empty string; missing profile → clean exit at apply time). **Deploy impact:** the GHCR image no longer contains personal prompts — docker-compose.yml now mounts them from `./prompts/` on the host (`:ro`, file-must-exist caveat documented); VPS needs the files copied once BEFORE next `docker compose up` (see docs/PUBLIC_RELEASE_CHECKLIST.md §5). New `docs/PUBLIC_RELEASE_CHECKLIST.md`: full go-public runbook incl. `git filter-repo` history-scrub commands (NOT run — owner action), verification greps, force-push caveats, GitHub settings. README quick-start updated with `cp *.example.md` step. |
| 2026-07-07 | fable | Public-repo prep: README + root cleanup (branch claude/charming-goldwasser-ad5c3b). New root **README.md** (badges, mermaid architecture diagram, 7-layer quality-pipeline table, quick start, docs links — written for a public audience) + **LICENSE** (MIT). Root decluttered: `DEPLOY.md`→`docs/archive/DEPLOY_V1.md`, `DEPLOY_V2.md`→`docs/DEPLOY.md` (current guide), `BOOTSTRAP_DEDUP_PLAN.md`/`DUPLICATE_INVESTIGATION.md`→`docs/archive/` (code-comment references updated in telegram_bot.py / test_bootstrap_dedup.py / tools/dedup_sheet.py), scratch `smoke_test_cl.py` deleted (ruff extend-exclude removed from pyproject.toml — gate now truly whole-repo), `start_hunter.bat`→`tools/` (hardcoded `D:\LearningProject\Claude` path replaced with `%~dp0..` so it works from any checkout), `.mcp.json` untracked + gitignored (personal machine paths, unrelated MCP server). NOTE for going public: `prompts/` (candidate_profile.md, base CVs, examples/, candidate/) contains real personal data throughout git history — owner decision needed before flipping visibility. |
| 2026-07-07 | fable | Mill-in-body HARD gate rule + verdict gap_report in Telegram (branch fix/mill-body-gate-verdict-gap). (1) Owner asked to filter micro1 links outright: on 2026-07-06 two micro1 fronts (QuikHireStaffing, HireFeed) reached generation despite both being in `exclude_companies` since PR #110 — root cause: `_is_ai_training_or_mill` only checks `job.company`, which is BLANK for Gmail-alert stubs (linkedin.com is in `GMAIL_ENRICH_SKIP_HOSTS`, so the stub never gets a company), and the doomed gate reused the same company-only check. New HARD gate rule `_assess_mill_body` in `hunter/filters.py` (wired into `assess_job_text` next to `_assess_work_authorization`): scans the FULL job text for every `exclude_companies` entry (word-boundary regex, spaces → `\s+`; "micro1" also matches "micro1.com" apply links) — a mill named anywhere in the body now SKIPs generation for $0.00 regardless of the company field. No new config: reuses `exclude_ai_training`/`exclude_companies`. (2) Verdict gap_report → Telegram (owner thought this already shipped — it hadn't; only the bare score line was sent): `format_verdict` now appends the judge's `gap_report` (new `format_gap_report`: trimmed to 350 chars, HTML-escaped for parse_mode=HTML) — the CLI pipeline gets it automatically via `pdf_summary`, and apply_api adds a dedicated `{gap_line}` to the success message, so the owner sees WHY the verdict isn't higher (per the 94%-P2Recruitment analysis: the judge's reasons were invisible). 10 new tests (ai_mill_body ×5, format ×4, apply_api wiring ×1); ruff clean. |
| 2026-07-07 | fable | Refine-loop 3 rounds + true cost accounting (branch feat/refine-3rounds-cost-restamp). Owner reported 2026-07-06 real spend ~$6 vs ~$2 in the Sheet. Root cause: the tracker row's Cost $ is priced at Step 6.5 — BEFORE the independent verdict and the whole refine loop; the post-loop re-pricing only reached content.json/Telegram (the "one Haiku call ~$0.02 drift" comment predated PR #115), and with `ATS_VERDICT_TARGET=95` vs real verdicts of 72–94 the loop fired on 100% of that day's generations (rollbacks cost the same as accepted rounds), so ~⅔ of spend was invisible. Fix 1 (accounting): new `tracker.set_cost(url, cost_usd)` (same post-hoc-UPDATE-by-URL contract as set_ats_verdict), wired into apply_api right after the post-verdict re-pricing — Sheet column M picks it up automatically because cost_writer reads cost_usd from the DB at mirror time; paste flow (no URL) skipped. Fix 2 (escalation, owner decision): `ATS_VERDICT_MAX_REFINES` default 2→3 and the stretch threshold moved — rounds 1–2 honest visibility passes, round 3+ stretch (`verdict_refine.STRETCH_FROM_ROUND=3`): only the final round openly adds posting tech to score points. Note: CLI-pipeline refine API calls remain unpriced (CLI mode has no usage-log frame — pre-existing gap, Cost $ stays blank there by design). 10 new/updated tests (set_cost ×6, escalation threshold ×3, apply_api cost-restamp wiring ×1); ruff clean. |
| 2026-07-06 | sonnet | Doomed-vacancy gate M4 — calibration on real data (branch feat/doomed-vacancy-gate, docs/DOOMED_GATE_PLAN.md; M1–M3 landed earlier this session). Ran `tools/screen_calibrate.py` (already written in M1) against 375 offline `Applications/**/job_posting.txt` files + a 20-URL live Google Sheet spot-check (394 postings total); the acceptance bar is 0 HARD findings on rows the owner actually Sent. First pass surfaced 6: fixed 2 real gate false positives in `hunter/filters.py` — (1) `foreign_onsite_hybrid` tripped on a BitPanda perks bullet ("Fuel and focus on-site – Pandas in Vienna, Bucharest, Barcelona, and Berlin can enjoy free onsite dining") because four foreign cities sat within the 120-char proximity window of "onsite"; new `_onsite_signal_positions()` drops an on-site/onsite occurrence immediately followed by a perks/benefits word (dining/lunch/snacks/gym/cafeteria/coffee/parking…), shared by both the HARD foreign-location rule and the existing SOFT PL anti-hybrid-city check; (2) `is_german_language_required` tripped on a real SENT theprotocol.it posting (DHCBusinessSolutions) listing German under an explicit "Nice to have — Optional" heading; new `_is_optional_context()` vetoes a language-required match preceded within 150 chars by an explicit nice-to-have/optional/bonus/a-plus/mile-widziane/dodatkowym-atutem marker, while a genuine requirement elsewhere in the same posting still fires. The remaining 4 (`is_ai_training_or_mill` on Micro1) are not a regex-precision issue — that rule is an exact-name lookup against the owner-curated `exclude_companies` list added in PR #110 (2026-06-30), and the Micro1 Sent dates (13 May / 19 Jun) predate that policy; `screen_calibrate.py` now reports these separately (`pre-policy`) instead of counting them as false positives, so loosening the list to chase a clean number doesn't silently undo the owner's own decision. Final live run: 0/394 false positives, exit code 0. 4 new regression tests in `test_doomed_gate.py`. Full report in `docs/DOOMED_GATE_CALIBRATION.md`. Full suite green except the 3 known pre-existing tracker.xlsx-migration failures (test_cost_writer ×2 + test_verdict_writer ×1); ruff clean. |
| 2026-07-04 | sonnet | Verdict refine loop (branch feat/verdict-refine-loop, spec docs/VERDICT_REFINE_PLAN.md). The independent PDF verdict (Phase 2) was computed once and just recorded — half its "missing keyword" feedback is presentational (a real skill the resume didn't surface), so this closes the loop into rewrite → re-render → re-verdict. **M1** new `hunter/verdict_refine.py`: `build_refine_feedback` deterministically drops unfixable recommendations (location/relocation/hybrid/on-site/cover-note/LinkedIn/years-of-experience) before they reach the rewrite prompt; `refine_loop` runs up to `ATS_VERDICT_MAX_REFINES` escalating rounds — round 1 honest (candidate_profile.md facts only), round 2+ stretch (may add posting tech absent from the profile as plain skills/summary entries, logged to `to_learn`, optionally woven into one flexible Altoros project 2018–2022, never into Atruvia/Fairmarkit/Intel/SII/SolbegSoft) — re-running sanitize/scrubs/judge/language-gate each round and keeping a round only on a strict verdict improvement (otherwise content.json + docs roll back — regression impossible by construction). **M2/M3** wired into `apply_api.py`/`apply_cli.py` right after the first verdict, before the tracker stamp (CLI skips with a log line when `LLM_API_KEY` is unset). **M4** `tracker.set_ats_verdict` now also overwrites `ats_status`/"ATS %"; Telegram drops the `| self: NN%` suffix — only the independent verdict is user-facing anywhere (self-score stays in content.json for diagnostics). **M5** `ATS_VERDICT_TARGET`/`ATS_VERDICT_MAX_REFINES` config (default 95/2; 0 = old one-shot behaviour byte-for-byte). 12+ new tests (test_verdict_refine.py) covering feedback filtering, accept/rollback, language-gate block, exception best-effort, escalation prompts, to_learn tracking, and the tracker/Telegram format changes. **Post-review fixes (same day):** independent review flagged 4 findings, all fixed before merge — (1) round-2 `to_learn` stretch additions never reached the tracker row (created earlier, in Step 7, with the pre-loop value): new `tracker.set_to_learn(url, to_learn)` (same shape as `set_ats_verdict`), wired into both pipelines right after `refine_loop` returns, gated on the value actually differing from its pre-loop snapshot; (2) every refine-round regen reused the Step 7 `gen_cmd` (built with `force=skip_dedup`), so a force-mode apply DELETE+INSERTed the tracker row on every round/rollback (new sync ID, false Re-application flag) — both `_regen_for_refine` callbacks now build their OWN `build_generate_docs_cmd(..., force=False, no_tracker=True)`; (3) default `ATS_VERDICT_MAX_REFINES` was `1` (honest-only) instead of the plan's owner-approved `2` (honest+stretch) — fixed in config/.env.example/CLAUDE.md; (4) PL mirroring ran inside every round before the keep-best decision, wasting a translate call on rolled-back rounds — moved to run once, after the loop, only if a round was actually accepted, followed by one extra local re-render. 12 new/updated tests (to_learn stamp × 6, no-tracker-regen wiring × 2, PL-mirror-once × 3, config-default × 1); full suite green except the 3 known-preexisting tracker.xlsx-migration failures (test_cost_writer/test_verdict_writer, unrelated); ruff clean. |
| 2026-07-02 | fable | ATS verdict Phase 2 (same branch/PR as Phase 1 below, spec in docs/ATS_VERDICT_PHASE2_PLAN.md). **M1** `ats_verdict REAL` DB column (lazy migration) + `tracker.set_ats_verdict(url, score)` post-hoc stamp. **M2** `hunter/verdict_writer.py` — Sheet column **N** "ATS Verdict" (cost_writer/column-M pattern: cell mirror + lazy header + one-batch backfill via `tools/sync_verdicts.py`), wired into `gsheets_sync.mirror_new_row` after the cost poke; timing works because the apply subprocess stamps the DB before exiting and the A–K append runs later in the bot process. Four non-overlapping Sheet writers: A–K push, L sent_normalizer, M cost_writer, N verdict_writer. **M3** both pipelines stamp the tracker row in the verdict block (paste flow skipped — no URL key). **M4** dual-apply shadows get their own verdict on the shadow PDF (same Anthropic judge regardless of `set_override`, so the A/B is like-for-like); `_ats_suffix` prefers verdict over `ats_check` for the `_ats{NN}` filenames. 26 new tests across M1–M4; suite 1603 green; ruff clean. |
| 2026-07-02 | fable | ATS loop made deterministic + final independent PDF verdict (branch feat/ats-deterministic-loop-pdf-verdict). Data-driven root cause from 713 content.json on Drive: 88% of June–July runs burned ALL 5 ATS rewrite rounds with keyword_score already 100% — the 95% combined threshold was mathematically unreachable because the post-round-1 formula (`keyword×0.75 + TF-IDF×0.25`) is capped by TF-IDF (median 51, needs ≥80), which no rewrite moves. Avg 8.3 LLM calls / $0.38 per vacancy, ~5 of them wasted. Fixes: (1) `_ats_check_loop` exits as soon as the blocklist-filtered missing-keyword list is empty (rewrites can only ADD keywords); the in-loop LLM reviewer (attempt-1, 30% weight) removed — the loop is now pure regex+TF-IDF. (2) New final verdict: `ats_checker.llm_verdict()` (wider caps: job 6k / resume 9k chars) called by `ats_pdf_roundtrip.run_llm_verdict()` — ONE cheap `JUDGE_MODEL` (Haiku) call scoring the text extracted from the **rendered EN CV PDF** (what a real ATS parses), by a model that didn't write the resume. Wired into apply_api (Step 7.7, re-prices cost so content.json/Telegram include the verdict call; tracker row keeps pre-verdict figure, ~$0.02 drift) and apply_cli (after roundtrip; line rides pdf_summary). Telegram now leads with the verdict (`ATS: 91% (independent, PDF) | self: 97%`) instead of the generator's self-score. Config: `ATS_VERDICT_ENABLED` (default true). Expected: ~8.3 → ~3-4 calls/vacancy, ~$0.38 → ~$0.13-0.17. 10 new tests (deterministic-exit ×4, verdict ×6); suite 1577 green; ruff clean. |
| 2026-06-30 | sonnet | Dual-apply shadow → Google Drive (branch from `claude/gifted-einstein-d1c3df`). Owner reported shadow CVs (dual-apply A/B comparison) never appeared on Drive — by design the shadow has no tracker row, and Drive upload always rode the tracker-row hook, so it was structurally unreachable, not broken. Two-part fix: (1) `gdrive_sync.upload_shadow_folder(primary_folder, shadow_subfolder)` nests the upload under the primary's own company folder (`Job Hunter/{date}/{company}/{shadow_name}/`) instead of writing to tracker; wired into `dual_apply._generate_shadow()` as a best-effort call right after doc rendering, gated by `GDRIVE_ENABLED`. (2) `/gdrive_upload_missing` (`gdrive_sync.upload_missing_folders`) extended with `_upload_shadow_subfolders()` — scans every locally-present company folder (regardless of the company's own already-uploaded status, since shadow has no Drive-URL column to check) for a subfolder matching a known `llm_profiles.PROFILES` name and uploads it; idempotent via Drive's upsert-by-name, so safe to re-run and backfills shadow sets generated before this existed. Reply text shows a new "Shadow (dual-apply) uploaded" count + separate shadow error list. 16 new tests (gdrive_sync shadow helpers + missing-folders integration + dual_apply upload/failure paths); full suite 1567 green; ruff clean. |
| 2026-06-28 | sonnet | Reliability fixes (branch `fix/reliability-fixes`; plan in docs/RELIABILITY_FIXES_PLAN.md). Investigated a reported tracker FAIL flood — prod log `2026-06-28.log` + `git diff` cleared PR #107 (it never touched tracker/filters/main/sources; active model was sonnet) and pinned the real, pre-existing causes: LinkedIn fetch 429 without a session, pracuj Cloudflare 403, a 237× gmail_enricher 429 storm, flaky remote boards — all at the fetch stage. **Fix A** (`fix(dual)`): the dual-apply shadow used to run inline inside the primary's apply subprocess under the 900s bot timeout, so a slow shadow could get an already-successful apply killed + marked FAIL. Now launched fire-and-forget detached (`dual_apply.launch_detached` + `python -m hunter.dual_apply` entry with a `DUAL_SHADOW_TIMEOUT_SEC` watchdog) — the shadow can never touch the primary's exit code/timeout. **Fix B** (`fix(gmail)`): `GMAIL_ENRICH_SKIP_HOSTS` (default `linkedin.com,pracuj.pl`) — `_enrich_one` keeps the email stub for hard-blocking hosts instead of fetching (kills the 429 storm; dedup unaffected). **Fix C** (`docs`): documented `LINKEDIN_STORAGE_STATE` setup (biggest FAIL source) + new vars in `.env.example`/CLAUDE.md. **Fix D** (`fix(apply)`): new `is_transient_fetch_error()` — 403/Cloudflare blocks on known anti-bot hosts (pracuj/linkedin/theprotocol) now classify as transient like 429, so they retry quietly and clear on success instead of escalating to permanent "gave up" dead rows (plain 403 on other hosts stays permanent). 15 new tests across the four fixes; full suite 1505 green; ruff clean. apply_agent.py stays a ≤200-line shim. |
| 2026-06-28 | sonnet | Dual-apply A/B comparison (Phase D, branch `feat/deepseek-provider`). User wants every new generation produced by BOTH Sonnet (boevoy) and DeepSeek-V3 (shadow) side by side to compare quality in production. New `hunter/dual_apply.py`: `run_shadow(folder)` — after a successful primary apply, generates a second set with the shadow profile into `{Company}/{shadow}/` reusing the saved `job_posting.txt` (no re-fetch) and the same building blocks (call_llm → `_ats_check_loop` → scrubs → lang gate → `generate_docs`), forcing the shadow model on every step via new `llm_profiles.set_override()`. Comparison-only: NO tracker (`generate_docs --no-tracker` flag added), NO Telegram, NO Sheets/Drive; rendered doc filenames suffixed with the shadow's ATS score (`..._EN_ats88.pdf`). `main_api`/`main_cli` now **return the output folder** on success so a single hook in `apply_agent.main()` (`_maybe_run_shadow`) covers both CLI (Sonnet via Pro) and API. Runtime toggle: `llm_profiles.dual_enabled()`/`set_dual()`/`shadow_profile()` (DB keys `dual_apply_enabled`/`dual_shadow_profile`, env `DUAL_SHADOW_PROFILE` fallback). New `/dual [on|off]` command + dual/LLM line added to `/status`. Best-effort throughout — shadow failure never touches the real application. 18 new tests (test_dual_apply); full suite 1488 green; ruff clean. **Also (earlier this session):** re-ran 3 vacancies through R1 + 11 through V3 for the user's quality comparison (Applications_DeepSeek_R1 / _V3). |
| 2026-06-28 | sonnet | OpenRouter + DeepSeek R1 provider (Phase A) + runtime LLM profiles (Phase B) + ChatGPT profiles (Phase C), branch `feat/deepseek-provider`. **Phase A:** `_call_openrouter()` in `llm_client.py` (OpenAI-compat SDK, JSON mode, DeepSeek R1 usage mapping), `deepseek-r1`/`deepseek-chat` pricing in `llm_cost.py`, `LLM_API_KEY` fallback extended to `OPENROUTER_API_KEY`, `JUDGE_PROVIDER`/`JUDGE_API_KEY` added so Haiku judge calls Anthropic even when main provider=openrouter. Live-verified: $0.0851/vacancy vs ~$0.50 Sonnet (6× cheaper), ATS 98%, lang_guard clean. **Phase B:** `hunter/llm_profiles.py` — named Profile registry (`sonnet`/`deepseek-r1`/`deepseek-v3`), DB-persisted active choice in `tracker.db` config table, `get_active()` resolution chain (DB→LLM_DEFAULT_PROFILE→LLM_PROVIDER+LLM_MODEL match→first available→sonnet fallback). `apply_api.py`/`apply_shared.py` route all LLM calls through `get_active()` (removed direct config imports). New `/llm` Telegram command shows current profile + cost estimate + available/unavailable profiles; `/llm <name>` switches runtime (no restart). **Phase C:** `gpt-4.1`/`gpt-4.1-mini`/`gpt-4o` profiles (provider=openai, env_key=OPENAI_API_KEY); pricing in `llm_cost.py`; `OPENAI_API_KEY` added to LLM_API_KEY fallback chain. All 1471 tests pass; ruff clean. CLAUDE.md config table updated with new env vars. |
| 2026-06-29 | opus | Filter hardening from tracker rows 670–767 audit (owner flagged ~45 unwanted CVs that got generated; reasons noted in the Sheet's Sent column). Root causes traced in `hunter/filters.py`: (1) `_is_fullstack_without_angular` *intentionally* let through any fullstack title containing "Angular"; (2) the `\bc#\b` exclude pattern never matched "C#" (`#` is non-word → trailing `\b` fails); (3) `_matches_location` only saw title+location field, so hybrid/on-site cities buried in the body slipped through, and Cyprus cities (Limassol/Nicosia/Larnaca) weren't in the anti-hybrid set; (4) `exclude_patterns` (.NET/Vue/WordPress/backend…) ran on the title only; (5) no rule for AI-data-labeling / staffing-mill roles (micro1 fronts: QuikHireStaffing, HireFeed); (6) manual URL/paste bypasses the filter entirely. Fixes: replaced `_is_fullstack_without_angular`→`_is_unwanted_fullstack(job)` (no-Angular always blocked; Angular+heavy-backend Java/Spring/.NET/C#/Python blocked via title OR body; **Node/Nuxt fullstack kept** per owner); fixed `\bc#`; new `_has_body_disqualifier` (body_exclude_patterns: blazor/mendix/wordpress/drupal/magento/sharepoint), `_is_unwanted_onsite_location` (on-site/hybrid signal within 120 chars of an anti-hybrid city in the body, vetoed by fully-remote signal or a Wrocław location), `_is_acceptable_weekly_hybrid` (KEEP a ~1-day/week hybrid but ONLY for Warsaw/Kraków — commutable from Wrocław; requires a low-frequency "1 day a week"/"raz w tygodniu" signal, no other far city, gated by `allow_weekly_hybrid_warsaw_krakow`), `_is_ai_training_or_mill` (exclude_companies); new title patterns (mendix/low-code/email developer/ui designer/ai training/data annotation); Cyprus added to `extra_anti_hybrid_cities`. New `filters.screen_job_text()` powers a **"warn but allow"** Telegram heads-up on the manual apply path (apply_api Step 1.5e + apply_cli) — pasted URLs that would normally be filtered still generate, but the owner is warned. New config keys: `exclude_fullstack_with_backend`/`fullstack_backend_stacks`, `exclude_body_disqualifiers`/`body_exclude_patterns`, `exclude_body_onsite_city`, `allow_weekly_hybrid_warsaw_krakow`, `exclude_ai_training`/`exclude_companies`. 52 new tests (test_filters_unwanted_2026_06), full suite 1557 green; ruff clean. **Deferred (needs owner decision):** re-application/"повторка" dedup — reposted roles get a new URL + tweaked title so the exact URL+company+title dedup misses them (rows 674/698/700/713/723/727/731/738/749/762). |
| 2026-06-17 | opus | NoFluffJobs detail-fetch fix (branch fix/nofluffjobs-posting-schema). Root cause traced from a prod "Job text too short — skipped" (XTB Senior Angular, 252 chars < MIN_JOB_TEXT_LEN 300): NoFluffJobs changed the `/api/posting/{slug}` response schema — the `sections` dict `_format_posting_text` read is gone, so only title/company(N/A)/location/seniority/musts made it into the text and the real body was dropped. Content moved to `details.description` / `requirements.description` (HTML) + `specs.dailyTasks` (list); salary to `essentials.originalSalary.types.<emp>.range`; company name to `company.name`; seniority to `basics.seniority`. Rewrote `_format_posting_text` multi-path (new `_dig`/`_first`/`_coerce_text`/`_extract_company`/`_extract_seniority`/`_extract_salary` + `_SECTION_SPECS` table, first-non-empty path wins, legacy `sections.*` kept as fallback, dup blocks deduped). Live-verified on the XTB URL: 252 → 3279 chars, company resolved, all body sections present. 1 new test (new-schema payload), 15 in test_sources_json_fetch_text; ruff clean. |
| 2026-06-15 | opus | Scraper health audit + fixes. Ran every NEEDS-ATTENTION source's real `search()` live (not WebFetch — cloudscraper sources need their own code path): theprotocol(38)/pracuj(29)/bulldogjob(13)/linkedin/workingnomads(47)/builtin(21)/remoteleaf(84)/inhire(19) all OK; only **jobleads broke** (0 cards). Root cause: jobleads renamed the listing card `data-testid` `seo-search-list-job-card-{N}` → `search-job-card` AND switched hrefs to relative (`/pl/job/...`) — `_parse` then rejected them on the `startswith("http")` guard. Fixed both in `hunter/sources/jobleads.py` (`_parse_cards` exact testid match; `_extract_card` prefixes BASE) + refreshed the test fixture to the new markup. Note: jobleads' server ignores `q=` (returns generic results) so few survive the frontend filter — data-quality limit, not a scraper bug; detail pages still Cloudflare-blocked (MANUAL flow unchanged). Also fixed a linkedin cosmetic bug: titles/company/location were never HTML-unescaped (`Java &amp; Angular`) — added `html.unescape` (imported as `html_unescape` to avoid the local `html` var shadow). 54 jobleads+linkedin tests pass; ruff clean. |
| 2026-06-12 | opus | LLM cost optimization (branch feat/llm-cost-optimization). (1) `LLM_MODEL` default `claude-3-5-haiku-20241022` (retired Feb 2026) → `claude-sonnet-4-6`; prod `.env` moved off the deprecated dated `claude-sonnet-4-20250514` (retires Jun 2026) to the same-price `claude-sonnet-4-6`. (2) `llm_client._call_anthropic` now sets `output_config.effort=low` + `thinking={"type":"disabled"}` on effort-capable models (Sonnet 4.6 / Opus 4.5+ / Fable 5) to keep the structured generation fast/cheap — both **model-gated** (`_supports_effort`/`_supports_disabled_thinking`) so judge calls on Haiku 4.5 (no effort param) never 400. (3) Prompt caching: the large system prefix (candidate profile + generation_rules + base CV — byte-identical across every call in a CV and across CVs) is wrapped `cache_control=ephemeral`, so repeated multi-pass calls (ATS loop, CL review, repair) read at ~0.1x. Pricing per-token unchanged ($3/$15) — savings come from caching (+ the existing CLI option). `effort` threaded through `call_llm(effort="low")`. Live-verified against the API on Sonnet 4.6 (effort+thinking+cache → no 400). 11 new tests (test_llm_client_anthropic), 1362 total; ruff clean. |
| 2026-06-12 | opus | OAuth token-expiry alerts E.3 + E.2 doc cleanup (Phase E, branch feat/oauth-token-alerts; roadmap docs/PROJECT_REVIEW_2026-06.md). **E.3:** a dead Sheets OAuth token (`invalid_grant`) once caused a false-EXPIRED cascade and was only noticed by its damage. New `hunter/oauth_alert.py`: `is_oauth_error()` classifies RefreshError/invalid_grant/expired-or-revoked/missing-token (vs transient 5xx); `refresh_or_alert(creds, request, token_file, service, reauth_cmd)` wraps the `creds.refresh()` at each client's auth boundary — on an auth error it fires a cooldown-deduplicated (6 h per service) Telegram "re-auth needed" alert naming the service + re-auth command, then re-raises so existing best-effort handling is unchanged. Wired into all three Google clients (gsheets_client, gmail_client, gdrive_client), incl. the missing/invalid-token branch. Telegram send is direct/sync (requests), no heavy imports. 9 new tests (test_oauth_alert, 1292 total); ruff clean. **E.2:** verified Known Issue #7 stale — tracker.py no longer imports openpyxl (Phase 5 SQLite migration); marked resolved. **E.1** (rebuild prod image for Playwright/Inhire) is a deploy-host action, not doable from dev — left as an ops note. |
| 2026-06-12 | opus | Hygiene A.2 (Phase A, branch chore/hygiene-ruff-mypy; roadmap docs/PROJECT_REVIEW_2026-06.md). Widened the ruff CI gate from `hunter/` + entry scripts to the whole repo (`tests/` + `tools/` no longer excluded; only the scratch `smoke_test_cl.py` stays out). Auto-fixed the 65 pre-existing lint issues that the exclusion had hidden (59 F401 unused-import + 6 F541 f-string-without-placeholder), all `ruff --fix`-safe. `ruff check .` green across the repo; full suite 1283 still green (no behaviour change). A.1 (split apply_shared.py) is DEFERRED until PR #91 (claim_judge, which extends apply_shared.py) merges — splitting it now would guarantee a large conflict. A.3 (mypy gate) DEFERRED: mypy isn't installed and gating untyped tracker.py/sources/ needs a large annotation pass, out of scope for one clean commit. |
| 2026-06-12 | opus | Funnel analytics D.1 (Phase D, branch feat/funnel-analytics; roadmap docs/PROJECT_REVIEW_2026-06.md). The bot applied jobs but never showed conversion. New `hunter/funnel.py`: `compute_funnel(days?)` aggregates tracker.db into tracked→generated→sent→responded both overall and per source. Source isn't stored on the row (tracker predates it) so it's inferred from the URL via each registered source's `matches_url` (cached) with a registered-domain fallback. Stage rules: generated = ats_status holds a numeric % (CV built); sent = `sent` column is a real value (not blank/dash/EXPIRED); responded = `answer` or `confirmation` non-empty. Optional day-window filters by the `date` column (undated rows excluded from a window). New `/funnel [days]` command (`commands/funnel.py`) renders overall counts + sent/response rates + per-source breakdown (tracked/gen/sent/resp, sorted by sent). 14 new tests (test_funnel, 1297 total); ruff clean. Read-only over tracker.db — no schema change, no CV generation. |
| 2026-06-12 | opus | Funnel analytics D.2 (same branch). Split the conflated terminal stage into two: **Confirmed** (ATS/board automated acknowledgement — the `confirmation` column already stamped by `/check_responses`→`email_response_checker.run_confirmation_check`→`tracker.set_confirmation`) vs **Answered** (human reply: rejection/interview/offer — the `answer` column). `FunnelCounts` now tracks `confirmed`/`answered` with `confirm_rate`/`answer_rate` (both over sent); `/funnel` shows both stages + per-source `tracked/gen/sent/conf/ans`. The /check_responses→tracker link already existed (set_confirmation), so the Confirmed stage is populated end-to-end with no new wiring. Tests updated (14 in test_funnel; 1297 total). |
| 2026-06-12 | opus | Scraper health monitoring (Phase B, branch feat/scraper-health-monitoring; roadmap docs/PROJECT_REVIEW_2026-06.md). A source returning 0 jobs was indistinguishable from "no new vacancies" — breakage was silent. New `hunter/source_health.py` (`source_runs` table in tracker.db, created lazily; ring-buffered to SOURCE_HEALTH_KEEP per source): `record_run(source, yield, ok, error)` after each `source.search()` in the hunt loop (main.py Step 1, best-effort); `source_health()`/`health_report()` classify OK/IDLE/BROKEN?/ERROR/NODATA over the last 20 runs; `newly_broken()` fires exactly once when a *previously-working* source (ever_positive) hits SOURCE_HEALTH_ALERT_STREAK=3 consecutive 0/error runs → `run_hunt` posts a "scraper may be broken" Telegram alert. New `/health` command (`commands/health.py`) groups the live ALL_SOURCES roster into attention/healthy/idle/no-data. Config: SOURCE_HEALTH_ENABLED/ALERT_STREAK/KEEP. 15 new tests (test_source_health, 1298 total); ruff clean. No CV generation involved (telemetry only). |
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
| 2026-06-11 | opus | Per-email Gmail hunt report (branch feat/gmail-per-email-report). Root cause of "/hunt gmail doesn't check all emails/vacancies": the report only showed jobs that survived filter+dedup, grouped by the enriched title *mislabelled as the email subject*, with no email date/sender — so an email whose vacancies were all filtered/deduped, or one where the regex extracted 0 URLs, was invisible. Fix: **Phase A** `Job.email_meta` (msg_id/date/subject/sender/aggregator) threaded from `gmail._parse_message` through `gmail_enricher` (preserved on Job recreate); `GmailSource.last_email_log` records one entry per email incl. 0-URL + skipped confirmations, `last_capped` flags the `GMAIL_MAX_RESULTS` ceiling; `LOOKBACK_HOURS`/`maxResults` → `GMAIL_LOOKBACK_HOURS`/`GMAIL_MAX_RESULTS` env. **Phase B** extracted `filters.classify_job(job)→reason|None` (apply_filters_with_stats now aggregates it) so the report gets the exact per-job filter reason. **Phase C/D** new `hunter/gmail_report.py:build_gmail_report()` renders per email `📧 date · aggregator · subject (found→taken)` + ✅ taken (title@company) + ♻️ dup · ✂️ filtered (human-labelled reasons); surfaces 0-URL emails (regex miss) + ceiling warning; chunked under 4096 and sent as own message(s). main.py tags every gmail Job (taken/dup_url/dup_ct/cooldown/filtered) across filter+dedup. 26 new tests (provenance 8 + classify 7 + report 11). Full suite 1262 pass. |
| 2026-06-11 | fable | Prestige-claim scrub + skills gloss dedup (branch fix/resume-prestige-and-gloss). Root cause (2 prod CVs, PeopleVibe 2026-06-11 + Shimi 2026-06-10, diffed against user's manual fixes): (1) the LLM fabricated "Fortune 500 clients" into BOTH EN and PL summaries despite the generation_rules.md RED LINE — prompt-only rule, nothing enforced it post-generation; (2) ATS keyword mirroring left "term / synonym" slash-gloss pairs in skills ("Performance Optimization / Performance optimisation" — literally US/UK spelling, "technical documentation / High-quality technical documentation"). Fix in `apply_shared.py`, wired into BOTH pipelines (API after compliance scrub; CLI before lang gate, any fix → content.json rewrite + doc regen): `_strip_prestige_claims(content, job_text)` removes Fortune 50/100/500/1000, top-tier, blue-chip claims from summary/skills/bullets/about-me EN+PL via tempered clause regex (can't swallow the honest "300+ German banks" clause; EN+PL connectors), sentence-drop fallback, posting-exception (term present in job text → allowed); `_dedup_skill_glosses` collapses "A / B" where sides are near-dups (crude stem + UK→US + PL-diacritic fold; equal/subset/Jaccard≥0.6 → keep first side), paren-aware comma split, compact UI/UX / CI/CD untouched, distinct "OpenShift / container platforms" kept. New gloss-pair rule in generation_rules.md. Verified against both real content.json: PeopleVibe output now byte-matches the user's manual edit; Shimi collapses all 4+3 gloss pairs, keeps "Security by Design / Security best practices". 17 new tests (1283 total). |
| 2026-06-12 | opus | CV claim-judge (Phase C, branch feat/cv-judge-verification; see docs/CV_JUDGE_PLAN.md). Replaces the regex-scrub whack-a-mole (each prestige/compliance scrub was added after one broken prod CV) with a systemic LLM-as-judge pass: new `prompts/judge_rules.md` + `hunter/claim_judge.py`. `judge_content(content, job_text, base_cv)` flattens the judged fields (summary/skills/bullets/cover-letters/about-me, `_en`+`_pl`; verbatim-locked company/title/education excluded), asks `JUDGE_MODEL` (Haiku) to list claims absent from the candidate profile + posting as `{field, quote, reason, severity}`; every finding's `quote` is verbatim-validated against the named field so judge hallucinations are dropped deterministically. `repair_content()` fixes actionable findings (fabrication/exaggeration): connector-aware clause-drop keeps the honest preceding clause ("...300+ German banks and Fortune 500 firms" → "...300+ German banks"), single targeted LLM rewrite for fields a drop would empty, rejected if it worsens `validate_content` (7-role guard). Wired into BOTH pipelines after the scrubs + before the language gate (apply_api Step 4.72 + `judge_report.json` artifact; apply_cli post-process, fixes join `_scrub_fixes` → rewrite+regen). `JUDGE_MODE` stages rollout report→warn→block (block aborts on surviving fabrication: API `sys.exit(0)`, CLI delete-docs+return). Best-effort throughout (never fatal). Config: JUDGE_ENABLED/MODEL/MODE/MAX_REPAIR_ROUNDS. 28 new tests (test_claim_judge), 1311 total green; ruff clean. Ships in `JUDGE_MODE=warn` — flip to `block` after a precision-review period (see plan M4). |
| 2026-06-10 | opus | PL/EN language routing + enforce-gate (branch fix/pl-en-language-routing). Root cause (traced from 2 prod CVs, RTVEuroAGD/theprotocol + DCG/solid.jobs): for Polish postings the EN CV shipped riddled with Polish ("responsywne interfejsy (responsive interfaces)", "monolitycznych to mikroserwisach", "(7+ lat doświadczenia)") because (a) `lang` was detected but never used, (b) the ATS loop mirrors the Polish posting's keywords verbatim into resume_en, (c) `resume_sanitizer`/`content_qa` only *warn*, never block — the broken EN PDF (the one delivered in short mode) was sent anyway. Fix: new `hunter/lang_guard.py` (deterministic `detect_posting_language` + Polish-in-EN / English-in-PL detection via diacritics+lexicon+suffix+bilingual-gloss, dependency-free, Polish place-name allowlist so "Wrocław" isn't flagged); new `apply_shared.enforce_language_separation` enforce-gate wired into BOTH `apply_api` and `apply_cli` after sanitize — repairs by *translating from the clean opposite-language counterpart* (role-count guarded) + up to 2 in-place cleanup passes, and BLOCKS delivery (no broken doc: API `sys.exit(0)`, CLI deletes docs+returns) if strong Polish survives. ATS rewrite prompts now forbid foreign words/glosses. Delivery routing: `content["primary_lang"]` makes short mode also render the clean PL CV for PL postings (so a Polish vacancy ships BOTH PL+EN CV and CL). **Live-verified** on both prod URLs (theprotocol + solid.jobs): EN resume now fully clean (en_strong/soft/pl all empty), full bilingual set generated, gate logs show active repair each run; full suite run 4× green. 32 new tests (test_lang_guard 21 + test_lang_enforce_gate 5 + ATS-prompt/routing ... 1232 total). |
