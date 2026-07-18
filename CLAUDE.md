# CLAUDE.md — Project Context for AI Agents

This file is the single source of truth for any agent working on this codebase.
Read it fully before making changes. Update it when you learn something new.

---

## What This Project Is

**Job Hunter Bot** — an autonomous system that:
1. Scrapes 25 Polish/European/global IT job boards for Senior Frontend (Angular) vacancies
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
                              /gsheets_status /gsheets_resync /llm /dual /tracks
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
                            Also: run_retry_failed() (own schedule, not per-hunt),
                            hunter/delivery.py -> instant Sheets+Drive after apply.
                            |
         +------------------+--------------------+
         v                  v                    v
hunter/sources/        hunter/tracker.py     apply_agent.py (thin CLI entry)
  24 sources             tracker.db r/w         |
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
With 24 sources, a full cycle spans ~15h20m from the base time, so the
13:00 and 19:00 cycles run well into the night and overlap each other.

Hunts are serialized through a global `_hunt_lock`; a hunt that fires while
another is running **waits (FIFO)** instead of being skipped — the old
skip-on-busy policy silently lost slots for hours (15 exact-minute collisions/
day between the 13:00/19:00 cycles + long auto-apply batches; see
docs/HUNT_QUEUE_AND_DELIVERY_PLAN.md). Scheduled hunts queue silently; the
manual `/hunt` command replies "⏳ queued" once. FAILed-row retries do NOT run
after every hunt anymore — they have their own slots (`RETRY_FAILED_TIMES`,
default 07:45/18:45; minutes :45 never collide with the :00/:20/:40 hunt grid).

---

## Job Sources (25 active)

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
| FindMyRemote | findmyremote.py | JSON API (`/api/jobs?query=`) | Remote only; ~21 freshest/query; emits ORIGINAL external ATS URLs; also fetches `findmyremote.ai` links relayed by the `findmyremote_frontend` Telegram channel |
| Smart Jobs | thesmartjobs.py | JSON API (`/api/jobs/search?query=`) | Polish IT board on Traffit ATS; no auth/Cloudflare; detail API `/api/jobs/{slug}`; deleted posting 404→EXPIRED |
| 4dayweek.io | fourdayweek.py | JSON API v2 | |
| WeWorkRemotely | weworkremotely.py | RSS feed | |
| RemoteLeaf | remoteleaf.py | HTML listing parser | Paginated |
| Inhire.io | inhire.py | Playwright + Vuex store | Requires Playwright |
| JobLeads | jobleads.py | HTML scraper | Cloudflare issues; MANUAL flow |
| ATS Aggregator | ats_aggregator.py | Per-company ATS APIs | Workable/Greenhouse/Lever/Recruitee/Ashby |
| Gmail | gmail.py | Gmail API email alerts | Parses LinkedIn/NoFluff/JustJoin/Pracuj alerts |
| LinkedIn Scout relay | linkedin_scout_relay.py | Drains a JSON queue file | No scraping — reads what the standalone `linkedin_scout/` script found; behaves like any other source (not `manual_only`), see below |
| Telegram channels | telegram_channels.py | `t.me/s/{channel}` public preview HTML | No auth/MTProto; owner-curated `telegram_channels.json`; see "Telegram Channels Source" below |

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
  config.py                 ALL config: env vars, schedule, paths, source toggles.
                            FILTER re-exported from filter_config.py (below) for
                            backward compat — `from hunter.config import FILTER`
                            still works everywhere.
  filter_config.py          FILTER dict: title/level/location whitelists, exclude
                            regex patterns, per-rule policy toggles (exclude_ai_
                            training, exclude_body_onsite_city, …). Split out of
                            config.py 2026-07-12 — pure organizational move, no
                            behavior change; see hunter/filters.py for where each
                            key is consumed.
  models.py                 Job dataclass
  filters.py                Central filter: keywords, level, location, patterns, React-only, German.
                            React-only exclusion (`_is_react_only_title`/`_is_react_without_angular`)
                            is gated by `_react_track_active()` (CANDIDATE_TRACKS/`/tracks` —
                            docs/quality/09-multi-track-react.md): a no-op when the react track
                            is active, unchanged (today's behavior) otherwise
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
  contact_extract.py        Deterministic recruiter-contact extraction from job_posting.txt
                            (labeled names PL/EN, signature blocks, emails, conservative phones;
                            precision over recall — feeds outreach.py)
  outreach.py               Post-apply outreach draft (issue #138): run_outreach(folder, url)
                            writes outreach.md next to the CV — contact block + ready-to-paste
                            ≤300-char LinkedIn message (one JUDGE_MODEL call, posting language,
                            +EN for PL). Best-effort; bot never sends anything itself
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
  best_effort.py            Generalizes source_health/oauth_alert's shape to every other
                            best-effort subsystem (docs/quality/03-best-effort-degradation-
                            alerts.md): `with best_effort("subsystem.name"):` swallows the
                            exception (existing contract unchanged) but counts CONSECUTIVE
                            failures per subsystem in SQLite (`subsystem_health` table,
                            hunter/db.py — survives the apply-subprocess boundary, same reason
                            source_health's counters do). At `threshold` (default 3) fires one
                            Telegram alert with a 6h cooldown; a success after an alert sends one
                            recovery message. Wrapped around the existing try/except in:
                            gdrive_sync (upload_application_folder/upload_shadow_folder/
                            upload_missing_folders — the 2026-07-13 stale-token incident this
                            closes), gsheets_sync (mirror_new_row/resync_dirty), delivery.py
                            (both targeted stages), outreach.py, dual_apply.py (shadow),
                            cost_writer.py, verdict_writer.py. Existing try/except are NOT
                            removed — the wrapper goes around them; a block that already
                            returns None/False on error re-raises from its except clause so the
                            failure still reaches best_effort() for counting
  gsheets_sync.py           High-level Sheets mirror (push/pull/resync/bootstrap)
  gsheets_client.py         Low-level Sheets API v4 wrapper
  gdrive_sync.py            High-level Drive upload (upload_application_folder). Every folder
                            resolution goes through `_resolve_folder()`, which serializes
                            get_or_create_folder behind an asyncio.Lock — the delivery hook and the
                            upload-missing backfill routinely overlap in this one event loop, and
                            interleaved list-then-create is what duplicated the date folders.
                            Deliberately NOT memoized: a cached id goes stale the moment a folder is
                            trashed/moved by hand and would silently absorb uploads into the trash
  gdrive_client.py          Low-level Drive API v3 wrapper. Drive allows same-named siblings, so
                            get_or_create_folder (a) converges on the OLDEST copy when duplicates
                            exist, so uploads stop scattering, and (b) re-lists after create and
                            yields to an older concurrent winner, trashing its own loser copy —
                            closing the cross-PROCESS race (detached dual-apply shadows) that no
                            in-process lock can see. tools/dedup_drive_folders.py merges historical
                            duplicates
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
    dual.py                 /dual [on|off|shadow <name>] — toggle dual-apply A/B comparison + switch shadow profile (hunter.dual_apply)
    tracks.py               /tracks [angular|react|both] — show/switch active candidate tracks
                            (docs/quality/09-multi-track-react.md); DB key `tracks_enabled`
                            wins over `CANDIDATE_TRACKS` env, same pattern as `/dual`
    url_message.py          URL/text message handler + button_callback + _handle_apply + _handle_skip
  delivery.py               deliver_apply_now(url?) — instant Sheets mirror + Drive upload
                            after EVERY successful apply (auto/manual/paste/LinkedIn batch);
                            targeted fast path by URL, falls back to the idempotent backfills
                            (push_missing_rows / upload_missing_folders) for no-URL pastes
                            and lookup misses. Best-effort; periodic jobs remain the safety net
  schedules/                One file per JobQueue callback
    hunt.py                 scheduled_hunt
    retry_failed.py         scheduled_retry_failed (RETRY_FAILED_TIMES, default 07:45/18:45)
    check_expired.py        scheduled_check_expired
    tracker_backup.py       scheduled_tracker_backup
    gdrive.py               scheduled_gdrive_upload_missing (every GDRIVE_UPLOAD_MISSING_INTERVAL_MIN)
    gsheets.py              scheduled_gsheets_resync + scheduled_gsheets_pull
    pending_report.py       scheduled_pending_report
    email_responses.py      scheduled_check_email_responses
    daily_summary.py        scheduled_daily_summary
    normalize_sent.py       scheduled_normalize_sent (daily 00:20, refreshes Sheets column L)
    __init__.py             register(app, tz) — wires all callbacks into the Application
  services/
    apply_service.py        Subprocess wrapper for apply_agent + generate_docs cmd builder
    tracker_service.py      High-level: should_skip_url(), record_successful_apply()
  sources/                  24 scrapers (see table above) + per-site detail-page fetchers
    base.py                 BaseSource ABC: search() / matches_url() / fetch_text()
    __init__.py             ALL_SOURCES registry + fetch_job_text() URL dispatcher
    html_fallback.py        Generic HTML -> text fallback + clean_url() helper
    telegram_channels.py    Telegram channels source: t.me/s/{channel} public preview
                            parser (TgPost, br->newline, outbound-link extraction),
                            EN/PL/RU prefilter, title synthesis, job assembly. See
                            "Telegram Channels Source" below.
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

docs/QUALITY_ROADMAP.md     Quality roadmap (2026-07-15): master doc with priorities/sequencing;
                            per-workstream details in docs/quality/01..09-*.md (deps lockfile,
                            best-effort alerts, golden E2E, pipeline unification, mypy/Sonar,
                            public-repo prep, candidate.yaml multi-user, CANDIDATE_TRACKS/React)
tests/                      38+ test files, ~3400 lines (pytest); `pytest tests/ --cov=hunter
                            --cov-report=xml --cov-report=term` for a coverage table (no
                            --cov-fail-under gate yet — docs/quality/04-coverage-and-golden-
                            e2e.md Part A, map the blind spots for a few weeks first)
tests/conftest.py           Shared fixtures: `tracker_db` (isolated tmp tracker.db),
                            `fake_llm` (routes llm_client.call_llm by prompt shape to
                            configurable generation/judge/verdict/outreach responses — a
                            lazy `from llm_client import call_llm` inside each caller means
                            ONE patch of `llm_client.call_llm` intercepts every call site in
                            the pipeline, however deep). Primary consumer:
                            test_golden_apply_e2e.py; reusable by any test that needs a real
                            pipeline without a real LLM.
tests/test_golden_apply_e2e.py  Golden E2E test (docs/quality/04): runs
                            hunter.apply_api.main_api() for REAL, mocking only the external
                            boundaries (LLM via fake_llm, network via fetch_job_text, the
                            generate_docs.py subprocess via a fake that reuses its real
                            filename helper + real tracker-write call, Telegram via list-
                            collecting stubs). Catches the "stages work individually but the
                            wiring breaks" bug class — verified against 3 hand-mutations
                            (comment out the verdict tracker stamp, force --no-tracker onto
                            the primary Step 7 call, disable the verdict entirely) that each
                            make the test fail as expected. 7 scenarios: happy EN (URL +
                            paste-mode variants), expired (no LLM call), doomed-gate HARD (no
                            LLM call), 3 mutation-catch regression guards. Surfaced a real
                            pre-existing bug in hunter/outreach.py (see below) — NOT fixed
                            here (out of scope), the test documents the actual behavior with
                            an explanatory comment instead of asserting a false pass.
tests/fixtures/golden/      Fixture LLM responses (generation/judge/verdict) + one EN job
                            posting for the golden E2E test, loaded by name — not real
                            LLM output, hand-written to exercise the pipeline's happy path.
tests/fixtures/sample_jobs/ Real job postings per track (angular/react/ai/fullstack_*) for preview
tools/                      Utilities: backup, dedup, gmail auth, gsheets auth, LinkedIn login
tools/preview_apply.py      Run apply pipeline against sample fixtures via CLI subscription
tools/preview_judge.py      Run the claim-judge (+scrubs) on an existing content.json without
                            regenerating — one Haiku call; mirrors run_judge_stage (JUDGE_MODE env)
tools/dedup_sheet.py        One-time cleanup of duplicate rows in the Sheets tracker (--apply to delete)
tools/dedup_drive_folders.py Merge duplicate Drive folders ("2026-07-06" x5, files scattered across
                            them) left behind by the pre-fix list-then-create race in gdrive_client:
                            keeps the OLDEST copy of each name (the one the bot now picks), moves the
                            others' children in, trashes the emptied dupe. Same-named children are
                            reported as conflicts and left in place — never silently overwritten.
                            The root-level pass groups purely by name, so it merges the duplicated
                            "Logs" folders (upload_log_file's target; Drive-for-Desktop renders the
                            same-named siblings as "Logs (1)"…) the same way — a day's log present
                            in two copies is a reported conflict, never a guess about which is
                            complete. Dry-run by default (--apply to merge); trash only, never a
                            hard delete. Tested against an in-memory fake Drive in
                            tests/test_dedup_drive_folders.py
tools/normalize_sent.py     Write clean "Applied Date" into Sheets column L from Sent (--apply to write)
tools/stats_sheet.py        Read-only stats over the Sheets Sent column (--write-tab for a Stats tab)
tools/screen_calibrate.py   Doomed-gate calibration (docs/DOOMED_GATE_PLAN.md M4): runs
                            assess_job_text over the offline Applications/**/job_posting.txt
                            corpus + a live Google Sheet sample, read-only/dry-run, reports
                            hard/soft hit rate and flags any HARD finding on a row the owner
                            actually sent (must be zero)
tools/verdict_noise.py      LLM_COST_REDUCTION_PLAN M2: re-scores the same rendered EN CV PDF
                            k times (unchanged input) across the n most recent Applications/
                            folders, reports the judge's own noise (per-folder spread +
                            population sigma) — informs (not decides) an eventual
                            ATS_VERDICT_TARGET change. ~n*k Haiku calls, needs a judge API key
tools/verdict_funnel_corr.py LLM_COST_REDUCTION_PLAN M2: read-only bucket of tracker.db rows
                            with a recorded ats_verdict into score bands (<80/80-84/85-89/
                            90-94/95+), reports sent/confirmed/answered rate per band (reuses
                            hunter.funnel's row classification) — does a higher verdict
                            actually correlate with a better outcome?
tools/judge_stats.py        LLM_COST_REDUCTION_PLAN M6: aggregates Applications/**/
                            judge_report.json violations by (severity, normalized field
                            class, normalized reason), prints top classes + example quotes +
                            severity breakdown, and draft "RED LINE candidate" lines for
                            classes seen repeatedly — read-only, doesn't edit
                            generation_rules.md

linkedin_scout/             STANDALONE — not imported by hunter/, not in Docker, not on the bot's
                            schedule. Runs on the owner's own desktop (residential IP, real Chrome)
                            via Windows Task Scheduler. See "LinkedIn Posts Scout" section below +
                            linkedin_scout/README.md.

telegram_channels.json      Owner-curated channel list for hunter/sources/telegram_channels.py
                            (tracked — see docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md)
pyproject.toml               SINGLE source of truth for dependencies (`[project.dependencies]`
                            + `browser`/`scout`/`dev` extras) and tool config (ruff, mypy,
                            pytest). Build backend `setuptools.build_meta` (was the
                            nonexistent `setuptools.backends.legacy:build` — silently worked
                            only because build isolation pulls a fresh setuptools).
requirements.lock            GENERATED (`uv pip compile pyproject.toml --all-extras
                            --python-platform linux --python-version 3.11 -o
                            requirements.lock`) — never hand-edit. Docker and the CI test-job
                            both install from this file, not from pyproject.toml directly, so
                            prod and CI always run the exact same transitive versions. Replaces
                            the old hand-maintained, mostly-unpinned `requirements.txt`.
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
| `DUAL_SHADOW_PROFILE` | `deepseek-v3` | Profile used for the dual-apply shadow comparison run. DB key `dual_shadow_profile` wins over this env fallback — set it at runtime via `/dual shadow <name>` in Telegram (e.g. `/dual shadow deepseek-v4-pro`). Toggle dual mode itself with `/dual on`/`/dual off` (DB key `dual_apply_enabled`). |
| `CANDIDATE_TRACKS` | `angular` | Which stacks the candidate is applying for (docs/quality/09-multi-track-react.md). Default is today's behavior unchanged — React-only vacancies are filtered at three points (listing filters, apply Step 1.5c pre-LLM check, apply Step 4.5 post-generation check). Set `angular,react` to also apply to React-only roles (uses `prompts/base_cv_react.md`, already-existing infra). Runtime override without a bot restart: `/tracks angular\|react\|both` (DB key `tracks_enabled` wins over this env var, same DB-wins-over-env pattern as `DUAL_SHADOW_PROFILE`). `hunter.config.active_tracks()` is the read helper. |
| `LLM_API_KEY` | — | API key for LLM provider (fallback; prefer provider-specific vars below) |
| `ANTHROPIC_API_KEY` | — | Anthropic key (for `sonnet` profile + judge) |
| `OPENROUTER_API_KEY` | — | OpenRouter key (for `deepseek-r1`, `deepseek-v3`, `deepseek-v4-pro`, `glm-5.2`) |
| `OPENAI_API_KEY` | — | OpenAI key (for `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o`) |
| `APPLY_USE_CLI` | `false` | Use Claude CLI (Pro subscription) instead of API |
| `JUDGE_ENABLED` | `true` | Run the LLM-as-judge CV verification pass |
| `JUDGE_MODEL` | `claude-haiku-4-5-20251001` | Cheap model for the judge (independent of generator). Always Anthropic — uses `JUDGE_PROVIDER`/`JUDGE_API_KEY`, not the main profile. |
| `JUDGE_PROVIDER` | `anthropic` | Judge LLM provider (separate from main provider; Haiku is Anthropic-only) |
| `JUDGE_API_KEY` | — | Judge API key (reads `ANTHROPIC_API_KEY` first; falls back to `LLM_API_KEY`) |
| `JUDGE_MODE` | `warn` | Rollout: `report` (artifact only) / `warn` (+Telegram) / `block` (+abort on surviving fabrication) |
| `JUDGE_MAX_REPAIR_ROUNDS` | `1` | Repair rounds before warn/block |
| `TRANSLATE_PROVIDER` | `anthropic` | Provider for mechanical PL<->EN translation calls (`hunter.apply_shared._translate_resume`/`_translate_plain`, used by the language enforce-gate's repair path and the verdict-refine PL mirror). A Haiku-tier task, not worth the main profile's $/output-token rate. |
| `TRANSLATE_MODEL` | `= JUDGE_MODEL` | Translation model (default same cheap model as the judge). |
| `TRANSLATE_API_KEY` | — | Translate API key (reads `ANTHROPIC_API_KEY` first; falls back to `LLM_API_KEY`; falls back further to the main LLM profile if nothing resolves — a translation call must never fail outright for lack of a dedicated key). See docs/LLM_COST_REDUCTION_PLAN.md M5. |
| `GEN_SKIP_PL_FOR_EN` | `true` | Skip generating `resume_pl`/`cover_letter_pl`/`about_me_pl` on the FIRST generation call for an English-language posting in short mode (~40-50% of that call's output tokens; short mode never delivers them for an EN posting anyway). PL postings and `--full` runs are unaffected. See docs/LLM_COST_REDUCTION_PLAN.md M4. |
| `ATS_VERDICT_ENABLED` | `true` | Final independent ATS verdict: after generate_docs, ONE `JUDGE_MODEL` (Haiku) call scores the text extracted from the rendered EN CV PDF against the posting. Stored as `ats_verdict` on content.json + tracker row (`set_ats_verdict`, which now also overwrites `ats_status`/"ATS %"), mirrored to Sheet column **N** (`hunter.verdict_writer`), and shown as the **only** "ATS:" number in Telegram (generator self-score stays in content.json only), and computed for dual-apply shadows too (verdict-based `_ats{NN}` filename suffix). Informational only — never blocks delivery. |
| `ATS_VERDICT_TARGET` | `95` | Target score (%) for the verdict refine loop (`hunter.verdict_refine`) — a verdict at or above this is left alone. |
| `ATS_VERDICT_MAX_REFINES` | `3` | Max escalating rewrite rounds the refine loop runs when the verdict is below target (rounds 1–2 honest, round 3+ stretch — `verdict_refine.STRETCH_FROM_ROUND`). Default `3` (owner decision 2026-07-07: two honest visibility passes, then one openly-add-skills round). `0` disables the loop (old one-shot verdict). See docs/VERDICT_REFINE_PLAN.md. |
| `OUTREACH_ENABLED` | `true` | After each successful apply (both pipelines, Step 7.8), write `outreach.md` into the application folder next to the CV: recruiter contact parsed from `job_posting.txt` (`hunter/contact_extract.py`, regex, $0) + a ready-to-paste ≤300-char LinkedIn message in the posting's language (+EN version for PL postings; one `JUDGE_MODEL` call grounded only in the already-judged content.json — no fresh fabrication surface). Rides the existing Drive folder upload. Best-effort — never blocks/fails the apply; the bot NEVER sends the message anywhere (owner sends manually). No Telegram/Sheets changes (owner decisions 2026-07-10). See issue #138. |
| `DOOMED_GATE_ENABLED` | `true` | Deterministic (regex-only, zero LLM cost) full-text screen (`hunter.apply_shared.run_doomed_gate` → `hunter.filters.assess_job_text`), run right after expired-check and before the first LLM call in both pipelines (Step 1.5f). HARD findings (non-Poland onsite/hybrid, non-EU work authorization, unsupported required language) write a SKIP tracker row and abort generation for $0.00; SOFT findings (e.g. stack mismatch) warn in Telegram and generation continues. Force-mode/manual-paste always degrades HARD to warn. See docs/DOOMED_GATE_PLAN.md. |
| `DOOMED_GATE_HARD_ACTION` | `skip` | `skip` aborts generation on a HARD finding; `warn` is an emergency lever to downgrade every HARD finding to a warning without disabling the gate entirely (e.g. if live-data precision turns out worse than calibration). |
| `APPLICATIONS_DIR` | `Applications/` | Output folder override (useful for preview/testing) |
| `CV_GDPR_CLAUSE` | `both` | GDPR/RODO consent clause at CV bottom: `both` (PL+EN), `pl` (PL CV only), `none` |
| `MAX_JOBS_PER_RUN` | `40` | Cap per hunt cycle (auto-apply only, applied after filter+dedup; raised 20→40 2026-07-10 — a lower value in the prod `.env` overrides this default) |
| `APPLY_DELAY_SEC` | `30` | Pause between auto-apply jobs |
| `RETRY_FAILED_TIMES` | `07:45,18:45` | When to retry FAILed tracker rows (comma-separated HH:MM, Warsaw). Used to run after EVERY per-source AUTO_APPLY hunt (72×/day), which kept `_hunt_lock` busy past the 40-min slot spacing. Minutes :45 never collide with the hunt grid (fires only at :00/:20/:40). |
| `APPLY_AGENT_TIMEOUT_SEC` | `900` | Subprocess timeout (15 min) |
| `DUAL_SHADOW_TIMEOUT_SEC` | `1800` | Hard wall-clock cap for the detached dual-apply shadow run (its own watchdog; independent of the primary timeout). Raised from 900 when the shadow gained the judge + verdict-refine stages (2026-07-09). |
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
| `GDRIVE_UPLOAD_MISSING_INTERVAL_MIN` | `30` | Drive backfill interval for application folders that missed their instant post-apply upload (`hunter/delivery.py`). Was hardcoded 3 h — the "not on Drive yet" lag the owner reported 2026-07-12. Idempotent (skips rows that already have a Drive URL). |
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
`JUSTREMOTE_ENABLED`, `REMOTEOK_ENABLED`, `HIMALAYAS_ENABLED`, `FINDMYREMOTE_ENABLED`,
`THESMARTJOBS_ENABLED`, `FOURDAYWEEK_ENABLED`,
`WEWORKREMOTELY_ENABLED`, `REMOTELEAF_ENABLED`, `ATS_AGGREGATOR_ENABLED`, `GMAIL_ENABLED`,
`LINKEDIN_SCOUT_RELAY_ENABLED` (default `true` — no scraping, just drains a JSON queue
file the standalone `linkedin_scout/` script writes; see "LinkedIn Posts Scout" below),
`TELEGRAM_CHANNELS_ENABLED` (default `true` — public `t.me/s/{channel}` preview, no
auth/MTProto; see "Telegram Channels Source" below). Also: `TELEGRAM_CHANNELS_FILE`
(default `telegram_channels.json` in the repo root — owner-curated channel list) and
`TELEGRAM_CHANNELS_DELAY_SEC` (default `1.5` — polite pause between per-channel fetches).

---

## Pipeline Flow

### Hunt cycle (`hunter/main.py`)
1. Each source calls `source.search()` -> `list[Job]`
3. `filters.apply_filters_with_stats()` — keywords, level, location, patterns, React-only, German language
4. Dedup: URL (`normalize_url`) + company+title key (`dedup_key`)
5. New jobs -> Telegram cards with Apply/Skip buttons
6. If `AUTO_APPLY=true` -> auto-apply pipeline; after each successful apply,
   `hunter/delivery.py::deliver_apply_now()` mirrors the row to Sheets and
   uploads the folder to Drive immediately (FAILed-row retries run on their
   own `RETRY_FAILED_TIMES` schedule, not per hunt)

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
7c. **Outreach draft** (`hunter.outreach.run_outreach`, both pipelines, Step 7.8,
   gated by `OUTREACH_ENABLED`, issue #138): writes `outreach.md` into the
   application folder — recruiter contact parsed deterministically from
   `job_posting.txt` (`hunter.contact_extract`, $0) + a ready-to-paste
   ≤300-char LinkedIn message (one `JUDGE_MODEL` call, posting language, +EN
   for PL postings, grounded ONLY in the already-judged content.json). No
   Telegram/Sheets delivery — the file rides the Drive folder upload; the
   owner copies + sends manually. Best-effort: never blocks/fails the apply.
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
Switch the shadow model at runtime with `/dual shadow <name>` (persists DB key
`dual_shadow_profile`, which wins over the `DUAL_SHADOW_PROFILE` env fallback,
default `deepseek-v3`; profiles: `deepseek-v3`/`deepseek-v4-pro`/`glm-5.2`/…).
When ON, after the **primary (boevoy)** apply finishes successfully, `apply_agent.main()`
calls `run_shadow(folder)`: a second generation with the **shadow** profile into
`{Company}/{shadow}/`. The shadow reuses the saved `job_posting.txt` (no re-fetch)
and — since 2026-07-09 — runs the **full boevoy pipeline with only the generator
model swapped** (`call_llm` → `_ats_check_loop` → scrubs → **claim judge**
(JUDGE_MODE capped block→warn, no Telegram; violations land in the shadow's own
`judge_report.json`) → lang gate → `generate_docs --no-tracker` → independent
PDF verdict → **verdict refine loop** (same `ATS_VERDICT_TARGET`/`_MAX_REFINES`,
regen always `--no-tracker`, no tracker stamps)), forcing the shadow model for
every generator step via `llm_profiles.set_override()`. It is
**comparison-only**: NO tracker row, NO Telegram, NO Sheets mirror. The
**independent PDF verdict** (`ats_pdf_roundtrip.run_llm_verdict` on the shadow's
rendered EN CV PDF) and the claim judge always use the Anthropic `JUDGE_*`
config, unaffected by `set_override()`, so primary and shadow are scored by the
SAME yardstick; the verdict is persisted in the shadow content.json and
preferred for the filename suffix. Rendered CV/CL
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

**Repo-split status (docs/SCOUT_REPO_SPLIT_PLAN.md, Phase 0 done 2026-07-08):**
`linkedin_scout/` is scheduled to move into its own **private** repo before this
repo goes public (a LinkedIn scraper with stealth flags under the owner's name is
a ToS/reputational liability in a public repo). Phase 0 — decoupling every
`hunter` import out of `linkedin_scout/` while both packages still share one test
suite — is complete: `linkedin_scout/config.py` reads `TELEGRAM_BOT_TOKEN`/
`TELEGRAM_CHAT_ID` straight from `.env`/`os.environ` (no `hunter.config` import);
`linkedin_scout/location_gate.py` is a vendored, plain-text copy of
`hunter/filters.py::_is_unwanted_onsite_location` (no `hunter.filters`/
`hunter.models` import, no `Job` object); the `/scoutfound` payload is now a
versioned contract (`"v": 1` in `telegram_relay.build_payload()`, tolerant/
version-checked decode in `hunter/commands/scoutfound.py`, golden fixture
`tests/fixtures/scout_payload_v1.json` shared by both sides' contract tests) so
schema drift after the split fails loudly instead of silently. `grep -r "from
hunter" linkedin_scout/` now returns zero real import statements. Phases 1-4
(new repo creation, desktop cutover, main-repo cleanup, optional history scrub)
are still pending — see the plan for the full checklist.

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
exposes a real permalink (see "Post permalinks" below), it's carried along (never
used for dedup/fetch/routing — the synthetic `url` stays the tracker key) but IS the
link the owner actually needs to go apply/message on, so it's surfaced everywhere
that matters: the Telegram cards/notifications, `job_posting.txt`, and durably in
`content.json["source_permalink"]` / `outreach.md`.

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
| `browser.py` | Playwright mechanics: persistent Chrome profile, cookie re-seeding, shadow-DOM-aware extraction JS (incl. `LI_PERMALINK::` marker capture — both the older `/feed/update/` share form and the newer `/posts/...-activity-...` vanity form), `...`-menu permalink capture for M1 candidates — live-verified 2026-07-08 (`_fetch_menu_permalinks`/`_copy_link_via_menu`, author-aria-label-first with a container-probe fallback), blocks image/media/font resource loading (memory — a long feed scroll OOM-crashed the tab; the crash surfaced as a generic Playwright "Execution context was destroyed" error, now also caught defensively instead of crashing the whole run), `scout_keyword()` (off-screen window) / `scout_feed()` (long randomized scroll + plateau stop), `run_once()`/`run_feed_once()` (circuit breaker + M1 filter wiring) |
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
dedup-key URL `https://linkedin.com/scout-posts/p...`, deliberately never a real
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
on the bot side (the synthetic `job.url` above is untouched — never used for dedup/
fetch/routing, `tracker.add_applied`'s dedup key must stay stable regardless of link
rot) → shown wherever the owner needs to actually click through and go apply/message
on the post (2026-07-11 fix, owner report: "как я вообще смогу податься через такой
синтетический пайплайн" — the permalink used to flash ONCE, pre-generation, in the
AUTO_APPLY hunt loop's ping, then was lost): `Job.telegram_text()` (the manual-mode
Apply/Skip card, `hunter/models.py`), the pre-apply ping AND the post-generation
"✅ Docs ready" success message (`hunter/main.py::_auto_apply_all` +
`hunter/apply_api.py` Step 8), `job_posting.txt`'s header ("Post: ..." line, both
pipelines), and — durably — `content.json["source_permalink"]` (set in
`apply_api.py`/`apply_cli.py` Step 6 from a new `--permalink URL` CLI flag threaded
through `apply_agent.main()` → `main_api`/`main_cli`, plumbed from `job.raw["permalink"]`
by `apply_service.run_apply_agent_subprocess`/`run_apply_agent_for_url` and
`commands/url_message.py::_handle_apply`), which `hunter/outreach.py`'s `_render()`
now prefers over the synthetic `url` for the "**Posting:**" line in `outreach.md` —
otherwise the one artifact meant for going back and messaging the recruiter pointed
at an unopenable fake link. `notify.py`'s `--dry-run` preview also shows it. The `...`-menu
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

## Telegram Channels Source (`hunter/sources/telegram_channels.py`)

23rd source, INSIDE the bot process/Docker image, on the normal staggered hunt
schedule — unlike LinkedIn Scout above, this needs no session, no desktop
component, no relay: `t.me/s/{channel}` is a plain public HTTP preview, no
auth/login/MTProto. Mechanism inspired by
https://github.com/strelov1/freehire (`docs/telegram-channels.md`), but the
channel list is NOT copied — a live probe (docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md
§1.2) found freehire's RU-market channels yield ≈0 relevant roles, while
frontend/EU channels absent from their list (`findmyremote_frontend`) are the
real source of Angular/frontend candidates. Their LLM-extraction step is also
not copied — the doomed gate + generation LLM already read the full text; a
separate extraction model changes no real decision (owner's standing rule
against speculative LLM layers).

**Channel config:** owner-curated `telegram_channels.json` (repo root,
tracked): `[{"channel": "findmyremote_frontend", "kind": "board", "note":
"..."}]`. `kind: "board"` = one vacancy per post (the source-level hiring-
signal prefilter is skipped — every post is assumed relevant); `kind:
"authored"` = editorial digest (hiring-signal prefilter still required).
Judge starter channels by `/funnel` + `/health` over 2-3 weeks and prune
freely — see the plan's §6/§9 for the current list + first-run yield data.

**`job.url`:** the post's first outbound external link when present (cleaned
via `html_fallback.clean_url`, dispatches through the normal
`fetch_job_text()` roster — an aggregator post's outbound link to e.g. a
NoFluffJobs/ATS page fetches through THAT source's own detail-page code, not
this one). Falls back to the post's own stable permalink
`https://t.me/{channel}/{msg_id}` for self-contained text posts, served by
this source's own `fetch_text()` via the single-post embed page
(`?embed=1&mode=tme`). The permalink is always kept in
`job.raw["permalink"]`/`job.raw["tg_permalink"]` for convenience
(`hunter/main.py::_auto_apply_all` already surfaces `raw["permalink"]`
generically in the pre-apply Telegram notification) — **never**
`job.raw["post_text"]`, which would wrongly reroute the apply through the
scout-relay paste flow (`hunter/services/apply_service.py`); every job here
has a real fetchable URL, so retries/expiry-checks work through the normal
machinery, unlike `linkedin_scout_relay`.

**Title synthesis:** the central filter (`hunter.filters.classify_job`)
checks `job.title` only, and these posts have no title field — `title` =
first non-empty text line (90-char cap), with the matched prefilter keyword
appended if absent from that line, so a garbage-looking synthesized title
(digest posts like "Hey job seekers! Check out a handful of remote
front-end roles...") still carries a real keyword the central whitelist can
see, without bypassing it.

**Cyrillic guard** (`hunter/lang_guard.py::cyrillic_fragments`, M3, blocker
before this source went live): the channel list includes RU boards, and the
ATS keyword loop mirrors posting keywords verbatim into `resume_en` — any
Cyrillic codepoint in an `_en`/`_pl` field is now always treated as strong
contamination (no allowlist needed, unlike Polish detection), folded into
`scan_content()`'s existing `en_strong`/`pl_english` buckets so
`apply_shared.enforce_language_separation`'s repair/block logic needed zero
changes. `detect_posting_language` still only distinguishes PL/EN — a RU
posting correctly produces an EN CV (this project does not generate RU CVs);
the guard only keeps Cyrillic OUT of that EN/PL CV.

**Validation floor:** `hunter.validation.TELEGRAM_POST_URL_MARKER` ("//t.me/")
gives `t.me` permalink jobs the same lower `MIN_SCOUT_TEXT_LEN=80` floor as
scout posts (a real board-style Telegram post is legitimately short);
external-link jobs keep the normal 300-char floor automatically since their
URL isn't `t.me`.

**M4 live-calibration findings** (docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md §9):
a "pinned Deleted message" service post DOES carry a
`tgme_widget_message_text` div (unlike a plain media-only post) and would
have synthesized a garbage job title — real posts carry Telegram's own
`service_message` CSS class regardless of text-div presence, which the
parser now checks. Some channels' raw HTML double-encodes query-string
ampersands (`&amp;amp;`) — BeautifulSoup only unescapes once, so links get a
second `html.unescape()` pass.

## Git Workflow

- **Active branch:** `develop` — all changes go here
- `master` is production-stable (60+ commits behind develop)
- Always commit on `develop`, never force-push `master`

---

## Important Rules for Agents

- **Never commit** `.env`, `tracker.xlsx`, `Applications/`, `backups/`, `gmail_token.json`, `gsheets_token.json`, `gsheets_credentials.json`, and the personal prompt files (`prompts/candidate_profile.md`, `prompts/base_cv_*.md`, `prompts/candidate/`, `prompts/examples/` — gitignored; repo is public, only `.example` templates are tracked)
- Always test syntax after edits: `python -m compileall .`
- Run `ruff check .` AND `ruff format .` before committing — CI gates on both
  (`ruff format --check`). Config in `pyproject.toml`, covers the whole repo:
  `hunter/` + entry scripts + `tests/` + `tools/`. Rule set: F/E/W + B (bugbear)
  + C4 + SIM + S (bandit); deliberate ignores are documented inline in
  `pyproject.toml` — don't silence a new finding without a rationale comment
- `mypy hunter/ llm_client.py generate_docs.py apply_agent.py` runs in CI
  (`typecheck` job) but is `continue-on-error: true` — informational only,
  does not block deploy yet. Baseline as of 2026-07-15: 223 errors in 54
  files (mostly PTB `Message | None`/`JobQueue | None` unchecked attribute
  access — real but pre-existing). Don't let a new change grow that number;
  fixing it down to zero (and flipping the gate to blocking) is tracked in
  docs/quality/06-static-gates-mypy-sonar.md Этап 1–2, not done in this pass
- SonarCloud scan runs as an informational CI job (`sonar-project.properties`);
  it skips itself until `SONAR_TOKEN` is added to the repo secrets and never
  blocks deploy
- **New dependency → edit `pyproject.toml` only, then regenerate the lock:**
  `uv pip compile pyproject.toml --all-extras --python-platform linux
  --python-version 3.11 -o requirements.lock` (fallback: pip-tools
  `pip-compile`). Never hand-edit `requirements.lock` or add a package to it
  directly — Docker and CI both install from the lock, so an un-regenerated
  lock means prod silently keeps running the old version. `--python-platform
  linux` matters: compiling on Windows pulls in Windows-only transitive deps
  (e.g. `colorama`) that don't belong in the Linux deploy image.
- Run `pytest tests/` after changes to tracker, filters, or sources
- Column index constants in `tracker.py` are hardcoded — update carefully
- Candidate profile single source of truth: `prompts/candidate_profile.md`
- LibreOffice path: `C:/Program Files/LibreOffice/program/soffice.exe` (in `generate_docs.py`)
- When changing tracker schema, bot behavior, or adding files — update CLAUDE.md in the same commit
- New best-effort code (a subsystem that must swallow its own errors — Sheets/
  Drive/Telegram/shadow/writer style) wraps its existing try/except in
  `with hunter.best_effort.best_effort("subsystem.name"):` rather than a bare
  swallow, so silent degradation still surfaces as one alert at a threshold
  instead of going unnoticed for hours (see hunter/best_effort.py)

---

## Known Issues and Technical Debt

### Structural

1. ~~**telegram_bot.py is a ~1380-line monolith.**~~ ✅ Resolved (Phase 1–7 refactor, 2026-05-26): split into `bot/` (6 modules), `commands/` (15 files), `schedules/` (9 files). `telegram_bot.py` is now a ~200-line import shim that re-exports everything for backward compat.

2. ~~**job_fetch/ is a separate parallel package (22 files, 2475 lines).**~~ ✅ Resolved (Phase 3 refactor, 2026-05-26): each source now owns its detail-page extraction (`matches_url` + `fetch_text` on `BaseSource`). `hunter.sources.fetch_job_text(url)` dispatches to the matching source. `job_fetch/` deleted.

3. **apply_agent.py is 1297 lines.** Contains two full pipelines (API + CLI mode), Telegram notification, folder management, LLM calling, cover letter review loop, paste flow, force mode, JobLeads MANUAL flow. Could be split.

### Infrastructure

4. ~~**Playwright not installed in Docker — Inhire source always returns [].**~~ ✅ Resolved: `playwright` is in the `browser` extra of `pyproject.toml` (pulled into `requirements.lock` via `--all-extras`) and the `Dockerfile` runs `playwright install chromium --with-deps` (adds ~500MB to image, ~seconds/page at runtime). Inhire is live (verified 2026-06-08: 25 jobs incl. Angular roles). **Ops note:** Inhire only works in prod once the deploy image is rebuilt with the current Dockerfile. Playwright does NOT unblock Wellfound — real headless Chromium still gets HTTP 403 (anti-bot needs a logged-in session + stealth; see `docs/new-sources/QUEUE-3-hard.md`).

### Code Quality

5. ~~**No pyproject.toml / setup.py.**~~ ✅ Resolved (Phase 6, 2026-05-31 + quality-02/06, 2026-07-15): `pyproject.toml` is the single dependency + tool-config source of truth; project installs via `pip install -e .`; `requirements.lock` pins the full transitive graph for Docker/CI. `[tool.mypy]` now runs in CI (`typecheck` job, `continue-on-error: true` — 223-error baseline, informational only until driven to zero; see docs/quality/06-static-gates-mypy-sonar.md).

6. **Filters are 293 lines** with complex German-language detection regex spanning 40+ patterns. Works but hard to maintain.

7. ~~**tracker.py is ~980 lines.** Multiple functions re-open and re-parse the entire Excel file per call.~~ ✅ Resolved by the Phase 5 SQLite migration (2026-05-27): tracker.py no longer imports openpyxl at all — every read/write goes through `hunter.db.get_db()` (SQLite, WAL). No per-call workbook re-parse remains. (tracker.py is still ~1050 lines, but that's surface area, not the Excel-reparse cost the issue described.)

8. ~~**`hunter/outreach.py::_candidate_summary` silently never writes outreach.md.**~~ ✅ Resolved 2026-07-17: it did `(resume.get("skills") or [])[:10]` assuming `resume_en.skills` is a list, but the real schema is a dict everywhere else (`generate_docs.build_resume`, `claim_judge.iter_judged_fields`) — slicing a dict raised `TypeError: unhashable type: 'slice'`, caught by `run_outreach`'s best-effort wrapper, so every normal apply silently skipped outreach.md. Fixed by a new `_flatten_skills()` helper that flattens the dict (or a bare list, defensively) into individual skill items before joining. `tests/test_outreach.py`'s fixture updated to the real dict shape (was a list, which is how this slipped through) + 3 new regression tests; `tests/test_golden_apply_e2e.py::test_golden_happy_path_en` now asserts `outreach.md` IS written instead of documenting the gap.

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
| NoFluffJobs | 2026-07-10 | OK | Sort audit 2026-07-10: default API order is already newest-first (`renewed` desc); NOTE the `page` field in the body is ignored by the server — every request returns the same first ~59 of `totalCount` postings, which is fine since those are the freshest. Listing POST `/api/search/posting`; detail `/api/posting/{slug}` schema changed (no more `sections` — content moved to `details.description` / `requirements.description` / `specs.dailyTasks`, salary to `essentials.originalSalary`, company name to `company.name`). `_format_posting_text` now multi-path with legacy fallback |
| LinkedIn | 2026-04 | OK | Guest HTML search API |
| Bulldogjob | 2026-07-10 | OK | `__NEXT_DATA__` JSON. Listing URLs now carry `/order,published,desc` (live-verified: default order pins promoted offers above fresh ones and dropped a job out of the top of the list; the segment makes the main block strictly newest-first). Side observation: `/remote,true` currently does NOT filter (identical list incl. `remote=False` jobs) — kept for when the site fixes it |
| Pracuj.pl | 2026-07-10 | OK | cloudscraper + `__NEXT_DATA__`. Sort audit: default listing order IS strictly `lastPublicated` desc (newest-first) — no sort param needed; none found in the page either |
| theprotocol.it | 2026-07-10 | OK | cloudscraper + dehydratedState. Sort audit: default is `sortType: "relevance"`; `?sort=<x>` is echoed into the state but does NOT change the SSR result order (verified: identical list for relevance vs date on the remote listing) — no working URL sort param found, left as-is. Observed order was near-date-desc anyway on the narrow frontend queries |
| SolidJobs | 2026-04 | OK | RSS feed |
| Arbeitnow | 2026-04 | OK | JSON API |
| Remotive | 2026-04 | OK | JSON API |
| Working Nomads | 2026-06 | OK | Public Elasticsearch `/jobsapi/_search` (5400+ jobs) |
| Jobspresso | 2026-06 | OK | RSS `?feed=job_feed`; only ~10 latest, no pagination |
| Built In | 2026-07-10 | OK | cloudscraper + BS4 DOM (`data-id="job-card"`); detail via html_fallback. Sort audit: default is relevance (a 7-days-old card above a 10-hours-old one); no working `?sort=` URL param found (`recency`/`recent`/`newest` all no-ops) — left as-is, content is still mostly fresh |
| JustRemote | 2026-06 | OK | JSON API `justremote-api.herokuapp.com/api/v1/jobs?category=developer` (~10 newest); detail via single-job API |
| RemoteOK | 2026-04 | OK | JSON API |
| Himalayas | 2026-07-12 | OK | JSON API for listing; detail fetch fixed 2026-07-12 (was 100% FAIL — see work log) |
| FindMyRemote | 2026-07-12 | OK | Live-verified at build: 3 queries (angular/frontend/react) → 46 jobs after prefilter, incl. a Poland-remote Angular role. API keeps deleted jobs with `dateDeleted` set → clean EXPIRED, not FAIL |
| Smart Jobs | 2026-07-13 | OK | Live-verified at build: 3 queries (angular/frontend/react) → 85 jobs after prefilter, incl. Mid/Senior Angular Developer roles; fetch_text of an Angular role → 4117 chars. Public `/api/jobs/search` + detail `/api/jobs/{slug}`, no auth/Cloudflare; deleted slug → HTTP 404 → clean EXPIRED |
| 4dayweek.io | 2026-04 | OK | JSON API v2 |
| WeWorkRemotely | 2026-04 | OK | RSS feed |
| RemoteLeaf | 2026-04 | OK | HTML listing |
| Inhire.io | 2026-06 | OK | Playwright + Vuex; live-verified 25 jobs (Angular roles). Needs prod image rebuilt with current Dockerfile |
| JobLeads | 2026-06 | PARTIAL | Listing OK (`data-testid="search-job-card"`, relative hrefs — re-verified 2026-06-15); detail pages Cloudflare-blocked → MANUAL flow. Note: server ignores `q=` param (generic results), so few survive the frontend filter |
| ATS Aggregator | 2026-07-13 | OK | Workable/Greenhouse/Lever/Recruitee/Ashby. Lever detail-fetch now uses the public posting API (`api.lever.co/v0/postings/{slug}/{id}`): a deleted posting (HTTP 404 / `{"ok":false}`) returns a synthetic EXPIRED marker → clean $0 skip instead of FAIL (was: `fetch_html` raised on the 404 page before the expiry check ran — 3 real Jobgether FAIL rows 2026-07-13). Live-verified against a deleted + a live Jobgether posting |
| Gmail | 2026-05 | OK | Gmail API alerts |
| Telegram channels | 2026-07-12 | OK | Public `t.me/s/` preview, no auth. Live yield (5 starter channels, 100 posts, 2026-07-11): `findmyremote_frontend` 15/20 prefilter pass (primary source); `rabotafrontend` 10/20; `IT_job_Poland`/`Remoteit` 0/20 (RU-market, expected — matches freehire-list flip in the plan). `it_vakansii_jobs` (1/20, a clickbait digest false positive) pruned 2026-07-12 after the owner independently flagged the same post via `max.ru`. See docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md §9-§10 |

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
> **Full history (all entries) lives in `docs/AGENT_LOG.md`** — moved there
> 2026-07-12 to keep this file small; only the 5 most recent entries stay here.
> Check the full log before touching a module with a non-trivial history (e.g.
> linkedin_scout/, dual_apply, verdict_refine, doomed gate, gsheets sync) —
> it documents rejected alternatives and live-verification findings that
> aren't visible in git log alone.

| Date | Agent | Work |
|------|-------|------|
| 2026-07-17 | fable | Logs-folder dedup coverage (branch chore/dedup-drive-logs-folders, PR #165). Follow-up to PR #163: the Drive root shows "Logs", "Logs (1)" .. "Logs (7)" in the Drive-for-Desktop mirror with daily .log files scattered across them — same same-named-siblings race, hitting the root-level "Logs" folder that `gdrive_sync.upload_log_file` resolves through the same get_or_create path (on Drive every copy is literally named "Logs"; the "(N)" is the desktop client's rendering). **Finding: no code change needed** — `tools/dedup_drive_folders.py`'s root-level pass groups children purely by name (no date filter), so it already merges duplicate Logs folders with the exact required semantics: keep oldest, move children in, trash emptied dupes; a same-named daily log file (one copy truncated, one complete) is a reported conflict left in place with its dupe folder kept — never a guess about which is the good one. What was missing was proof + docs: the tool had NO committed tests at all (PR #163's fake-Drive verification was scratch-only) and its docstring said "date folders". New `tests/test_dedup_drive_folders.py`: in-memory FakeDrive (list/update surface, mutation deferred to execute()) + 6 tests (Logs merge into oldest, log-filename conflict, dry-run provably mutates nothing, combined date+company+Logs sweep, singleton Logs untouched, ASCII-only output); mutation-verified — excluding "Logs" from the sweep fails 4 of the 6. Tool docstring/comment + CLAUDE.md description updated. Full suite 2276 green; ruff check/format + compileall clean. **Ops:** the scattered Logs folders merge via the same PR #163 flow — `docker compose exec job-hunter python tools/dedup_drive_folders.py` (dry run), then `--apply`; any reported log-file conflicts need a manual pick of the complete copy. |
| 2026-07-17 | opus | **Duplicate date folders on Google Drive** (branch fix/gdrive-duplicate-date-folders, owner report: "начали создаваться по несколько папок на одну дату. и файлы разбросаны по ним" — screenshot showed `2026-07-06` .. `2026-07-06 (5)`, i.e. Drive-for-Desktop's rendering of same-named Drive siblings). **Root cause:** Drive enforces NO unique-name constraint, and `gdrive_client.get_or_create_folder` was a plain unsynchronized list-then-create (TOCTOU). The bot has several writers resolving the SAME date folder concurrently — `delivery.deliver_apply_now` (post-apply hook), `scheduled_gdrive_upload_missing` (every `GDRIVE_UPLOAD_MISSING_INTERVAL_MIN`, a PTB JobQueue task in the same loop), `upload_log_file`, and each **detached dual-apply shadow process**. Interleaved, each saw "no folder" and created its own. Amplifier (the "files scattered" half): the lookup used `pageSize=1` with no `orderBy`, so once duplicates existed every writer got an ARBITRARY copy — uploads landed in whichever one Drive returned. **Fix, two layers.** (1) `gdrive_client`: `_list_folders` now orders by `createdTime` (pageSize 100, `createdTime` in fields) and `_pick_canonical` always converges on the OLDEST duplicate + warns — that alone stops the scattering for the folders already duplicated; after a create, `_resolve_create_race` re-lists and, if an older concurrent winner appeared, returns IT and trashes our loser copy (best-effort — closes the cross-PROCESS race the shadow causes, which no in-process lock can see). (2) `gdrive_sync._resolve_folder`: serializes every `get_or_create_folder` behind a lazily-bound `asyncio.Lock` so the second in-process caller's list runs AFTER the first's create and finds it; ALL five call sites routed through it (`_do_upload`, `upload_shadow_folder`, `upload_log_file`, `upload_missing_folders`'s `_resolve_root` + `_upload_row`). Deliberately **not** memoized — a first cut cached (name,parent)→id for the process lifetime, but two existing tests caught the call-count change and the design has a real hazard: a cached id goes stale the moment a folder is trashed/moved by hand and would then silently absorb uploads into the trash; the lock alone fixes the race, re-listing costs one cheap call. **Historical mess:** new `tools/dedup_drive_folders.py` (dry-run default, `--apply`, mirrors `tools/dedup_sheet.py`) merges existing duplicates — keeps the oldest of each name at BOTH levels (date folders under root, company folders inside each date), moves the others' children in, trashes the emptied dupe (trash, never a hard delete). A same-named child is reported as a conflict and LEFT in place (and its parent kept, not trashed) — nothing is silently overwritten. Verified end-to-end against an in-memory fake Drive (3 date copies → 1, nested company dupes merged, a genuine `job_posting.txt` conflict preserved; dry run provably mutates nothing); ASCII-only output after the real run hit `UnicodeEncodeError` on Windows cp1252. 9 new tests; the race tests are mutation-verified (reverting the lock → 4 concurrent creates overlap, `assert 4 == 1`; reverting orderBy/re-check → 2 more fail). Full suite 2267 green; ruff check/format + compileall clean. **Ops:** deploy, then run `docker compose exec job-hunter python tools/dedup_drive_folders.py` (dry run) and re-run with `--apply` to merge the existing scattered folders. |
| 2026-07-13 | opus | Smart Jobs (thesmartjobs.com) job source, 24→25 (branch feat/thesmartjobs-source, owner request "добавим новую платформу?"). New `hunter/sources/thesmartjobs.py`: Polish IT job board on the Traffit ATS, public JSON API, no auth/Cloudflare (live-verified — a plain server-side `requests.get` returns JSON same as the browser). Listing `GET /api/jobs/search?query=<kw>&sort=freshness&limit=100&locale=en` → `{data, meta}`; `sort=freshness` is strictly newest-first, page 1 (100 freshest per query) is all a hunt needs (pagination via `page`/`meta.totalPages` deliberately not walked). Detail `GET /api/jobs/{slug}?locale=en` returns the bare job object with full HTML `description`; `slug` = last path segment of the public url `https://thesmartjobs.com/en/{slugUrl}` (slugUrl == `praca/<slug>`). Queries `("angular","frontend","react")`, merged + url-deduped + coarse-prefiltered. **Deleted posting → HTTP 404 `{"error":"Job not found"}`** (or a non-`published` `status`) → `fetch_text` returns the literal "This job posting has expired." that `expired_check.EXPIRED_PATTERNS` matches → clean $0 EXPIRED skip instead of FAIL (mirrors the findmyremote/Lever pattern). Location built from `workModes` + per-city `locations`: a `remote` workMode injects a "remote" token via `text_utils.ensure_remote_token` so a genuinely-remote offer survives the central whitelist (remote/zdalnie/wrocław); on-site/hybrid other-city roles pass their raw city through and the central location filter drops them (Poland alone is NOT whitelisted, only remote/Wrocław). Salary rendered from `salaries[0]` (`14 000–18 000 PLN B2B`; contractType codes → B2B/UoP/UZ labels). `THESMARTJOBS_ENABLED` (default true) + ALL_SOURCES + `_fetch_roster()` registration. Live-verified at build: 3 queries → 85 jobs after prefilter (incl. Mid/Senior Angular Developer roles), fetch_text of an Angular role → 4117 chars, bogus slug → EXPIRED. 17 new tests (`test_thesmartjobs_source.py`) + 2 roster-count fixups in `test_sources_dispatcher.py` (24→25); full suite 2202 green; ruff check/format + compileall clean. |
| 2026-07-13 | opus | Drive uploads silently die mid-day → self-heal (branch claude/google-drive-sync-delay-27a124). Owner: "файлы не появляются на гугл диске сразу. и вообще нет." Diagnosed against the **live Drive cloud** (via the Drive MCP connector, bypassing the lagging desktop `G:` mirror): today's `Nexters` folder contained ONLY the dual-apply shadow subfolder (`deepseek-v4-pro`), NO primary CV — the folder was created *by the shadow* at 15:48, not by the bot. Earlier companies (HEINEKEN 01:47, NatekPoland 14:26, ITDSPolska 14:33) had full primary PDFs. Signature: for Nexters the sheet mirror ✅ + shadow upload ✅ + bot's own Drive ❌. **Root cause:** the bot is one long-lived process that builds the Drive API client ONCE and caches it (`gdrive_sync._service`). When Google rotates/expires the refresh token mid-day, the bot's *in-memory* Drive creds go stale and every bot-side upload fails silently (best-effort try/except), while (a) the separately-cached Sheets creds keep mirroring and (b) each detached shadow runs in a *fresh* process that reloads the token from disk (and persists it via `oauth_alert.refresh_or_alert`'s expired branch), so the shadow uploads fine. The 30-min Drive backfill reused the SAME stale cached service → the miss never self-healed until a bot restart ("вообще нет"). **Fix:** `hunter/gdrive_sync.py` — new `_invalidate_service()` + `_call_with_reauth(op)` wrapper that, on an OAuth/refresh error (`oauth_alert.is_oauth_error`), drops the cached service so the next call rebuilds it from the (shadow-refreshed) on-disk token, then retries the op ONCE; non-auth errors don't retry; if the on-disk token is itself revoked the retry re-raises and `build_service`'s existing re-auth alert fires. Applied to all three bot-side upload paths: `upload_application_folder` (the instant post-apply path that failed for Nexters), `upload_shadow_folder`, and the per-row loop in `upload_missing_folders` (root + each row re-fetch `_get_service()` so a rebuild takes effect). The "не сразу" (delay) half is partly inherent — the folder uploads only after the full pipeline incl. the verdict-refine loop's re-renders, then Google's desktop client syncs down to `G:`. **Immediate unblock for the owner:** deploy/restart the bot; the 30-min backfill then rebuilds the service and uploads Nexters' primary CV into the existing folder (get_or_create reuses it). 4 new tests (test_gdrive_sync.py: reauth retry/no-retry/re-raise + end-to-end stale-service recovery); full suite 2189 green; ruff check/format + compileall clean. |
| 2026-07-13 | fable | Three table-anomaly fixes the owner flagged (branch fix/lever-expired-gamedev-stack-company-regex). **(1) Lever deleted → FAIL, not EXPIRED**: 3 Jobgether `jobs.lever.co` rows FAILed because those postings are deleted (HTTP 404) and `ats_aggregator.fetch_text` routed Lever to the generic `fetch_html`, whose `raise_for_status()` throws on the 404 page *before* the pipeline's Step-3 expiry check ever runs. Fix mirrors the findmyremote pattern: new `_fetch_lever_text()` queries the public posting API (`api.lever.co/v0/postings/{slug}/{id}?mode=json`); a deleted posting (HTTP 404 **or** an `{"ok":false}` body) returns the synthetic `"This job posting has expired."` marker that `expired_check.EXPIRED_PATTERNS` matches → clean $0 EXPIRED skip; a live posting is rendered to plain text from the API JSON (title/location/`descriptionPlain`/`lists`/`additionalPlain`, HTML-strip fallback); any other error still falls back to `fetch_html`. Routed via a new `jobs.lever.co` branch in `AtsAggregatorSource.fetch_text`. Live-verified against the exact failing deleted row + a live Jobgether posting (4677 chars). **(2) game-engine role reached generation at 82%**: a Nexters "Hero Wars" role (Pixi/Cocos/Phaser/Babylon/Haxe/Spine, no Angular/React) passed every filter — "Frontend Developer" title + TypeScript + REMOTE — because the doomed-gate SOFT stack-mismatch rule (`filters._assess_stack_mismatch`) only knew Vue/Svelte/Ember. Added `_GAME_ENGINE_RE` (specific tokens: pixi/cocos(2d/creator)/phaser/babylon/haxe/`spine sdk`/godot/gamemaker/`unreal engine`/`unity 3d|engine` — deliberately NO bare "spine"/"unity" to avoid English-word false positives) under the same "neither Angular nor React present" guard, emitting a new SOFT `stack_mismatch_game_engine` finding (warn only, never HARD-blocks). **(3) tracker Company = URL path junk**: the teletype.in row showed Company `courierus/7ZGWxSxMZZ7` — `telegram_channels.guess_company`'s `_COMPANY_AT_RE` matched the `@` inside the URL path `teletype.in/@courierus/7ZGWxSxMZZ7`. Regex now requires the `@` at line/text start or after whitespace (`(?:^|(?<=\s))@`) and excludes `/` from the captured name, so a URL path can't be read as a " @ Company" mention (falls back to `@channel`, the intended default). 20 new tests (test_ats_lever_job_text.py ×12, doomed-gate game-engine ×3 param groups, telegram_channels company ×2); full suite 2185 green; ruff check/format + compileall clean. |
