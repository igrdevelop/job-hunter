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
                              /gsheets_status /gsheets_resync /llm /dual /tracks /retry_reset
                              /link /unlink (multi-user B3 — hunter/bot/auth.py gates
                              every handler: operational commands owner-only, URL paste
                              any linked user, /start + /link + /unlink open)
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
        sources.fetch_job_text(url)        # full job posting text
        expired_check.is_job_expired()     # skip if offer expired
        llm_client.call_llm()              # -> JSON (resume, cover letter, about me)
        cover letter self-review loop      # up to 3 LLM rounds
        generate_docs.py(content.json)     # -> DOCX + PDF + tracker.xlsx row
  --> Telegram notification + PDF/DOCX files
```

### Schedule

Base times: **02:00, 05:00, 08:00, 13:00** (`SCHEDULE_TIMES`, Europe/Warsaw).
Each source offset by `SCHEDULE_SOURCE_OFFSET_MIN` (default 40 min).
With 25 sources, a full cycle spans 16h40m from the base time, so cycles
overlap each other and a daytime base wraps deep into the evening.

**Quiet hours (`SCHEDULE_BLACKOUT`, default `18:00-00:00`, added 2026-08-16):**
no hunt slot ever fires inside that window. Dropping the old 19:00 base time
is NOT sufficient on its own — because of the 16h40m span, the 08:00 and 13:00
cycles reach the evening by themselves. `hunter/schedules/grid.py::fire_minute`
therefore walks the per-source offsets through ALLOWED minutes only, jumping
over the window: every source keeps a slot (skipping the ones landing in the
gap would silently starve whichever sources sit at those indices) and their
relative order is preserved; sources on either side of the gap just sit closer
together in wall-clock time. A malformed value, or one covering the whole day,
logs a warning and degrades to the plain `(base + i*offset) % 1440` grid — a
typo in `.env` must never leave the bot unscheduled.

**Why the schedule is also the generation schedule:** with `APPLY_QUEUE_ENABLED`
(true in prod) the hunt only writes `PENDING` rows, but `apply_worker_loop`
claims one within ~15 s, so moving the hunt grid moves when documents get
generated. Measured before the change (87 applies / 14 days on prod): 6.2
applies/day, peak 14, ~30 min each, with peaks at 10:00 and 19:00 mirroring the
old 08:00/19:00 bases and only ~21% falling inside 02:00-08:00. The new grid
puts 32% of slots there and 0% between 18:00 and 00:00 (owner ask 2026-08-16).
Slot count rises 75 → 100/day, which does not raise spend: dedup means a hunt
only queues genuinely new URLs, and `MAX_JOBS_PER_RUN` still caps each run.

Hunts are serialized through a global `_hunt_lock`; a hunt that fires while
another is running **waits (FIFO)** instead of being skipped — the old
skip-on-busy policy silently lost slots for hours (15 exact-minute collisions/
day between the 13:00/19:00 cycles + long auto-apply batches; see
docs/HUNT_QUEUE_AND_DELIVERY_PLAN.md). Scheduled hunts queue silently; the
manual `/hunt` command replies "⏳ queued" once. FAILed-row retries do NOT run
after every hunt anymore — they have their own slots (`RETRY_FAILED_TIMES`,
default **02:45/07:45**; minutes :45 never collide with the :00/:20/:40 hunt
grid, and the blackout jump lands on a segment boundary, which is :00, so the
grid stays aligned). Both slots moved into the night window with the rest of
the schedule — a retry runs the same apply pipeline as a hunt, and the old
18:45 slot fell squarely inside the quiet hours.

---

## Job Sources (25 active)

| Source | Module | Strategy | Notes |
|--------|--------|----------|-------|
| JustJoin.it | justjoin.py | JSON offers API (`from`-offset pagination) | Polish market leader |
| NoFluffJobs | nofluffjobs.py | POST JSON search API | No auth |
| LinkedIn | linkedin.py | Guest HTML search API | Paginated 10/call up to `MAX_PAGES_PER_KEYWORD` (see Scraper Health Notes — the endpoint's page size is 10, NOT 25) |
| Bulldogjob | bulldogjob.py | `__NEXT_DATA__` JSON | |
| Pracuj.pl | pracuj.py | cloudscraper + `__NEXT_DATA__` | Cloudflare-protected |
| theprotocol.it | theprotocol.py | cloudscraper + dehydratedState | Cloudflare-protected |
| SolidJobs | solidjobs.py | RSS feed | |
| Arbeitnow | arbeitnow.py | JSON API | EU/remote |
| Remotive | remotive.py | JSON API | Remote only |
| Working Nomads | workingnomads.py | Elasticsearch `/jobsapi/_search` | Remote, worldwide |
| Jobspresso | jobspresso.py | RSS feeds (`?feed=job_feed` + `search_keywords=` per keyword) | Remote; ~10 latest per feed |
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
| Gmail | gmail.py | Gmail API email alerts | Parses LinkedIn/NoFluff/JustJoin/Pracuj alerts. A digest links the job named in the subject PLUS several "similar" ones — per-job title/company come from each alert's own card (`gmail_parsers._linkedin_cards`), never from the subject |
| LinkedIn Scout relay | linkedin_scout_relay.py | Drains a JSON queue file | No scraping — reads what the standalone LinkedIn scout (external PRIVATE repo) found; behaves like any other source (not `manual_only`). Two record kinds: `post` (feed posts, synthetic dedup url + `post_text` paste flow) and `job` (jobs track, REAL `linkedin.com/jobs/view/<id>` url, description fetched normally). See below |
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
                            LLMOutageError (docs/LLM_OUTAGE_RESILIENCE_PLAN.md M1): account-
                            level failures (drained balance 400/quota-429/401/402/403, detected
                            by is_outage_signature) raise a distinct LLMError subclass, never
                            retried; apply exits 46 (APPLY_LLM_OUTAGE_EXIT_CODE) → outcome
                            "llm_outage" → batch loops stop immediately, write NO FAIL row and
                            never escalate fail_count (a billing outage is global state, not the
                            vacancy's fault). Dead rows from before: /retry_reset revives them.

hunter/
  candidate.py               Loader for candidate/candidate.yaml (docs/
                            CANDIDATE_YAML_PLAN.md, docs/SETUP_NEW_USER.md): the
                            single source of truth for the candidate's identity,
                            home city, languages and employer history, so a
                            different person can run the bot without editing any
                            source file. `load()` is a cached reader (`@lru_cache`,
                            `_set_path()`/`CANDIDATE_YAML_PATH` for tests) that
                            returns `{}` — never raises — when candidate.yaml is
                            absent; `get(dotpath, default)` reads a nested key
                            (`get("identity.full_name")`). Defaults are NEUTRAL
                            placeholders, not the owner's real values (changed
                            2026-08-12 — see the Agent Work Log): the old
                            "default reproduces the owner's original hardcoded
                            value" rule meant a second person running this repo
                            without a candidate.yaml silently generated CVs
                            carrying the owner's name and phone. Identity
                            (`REQUIRED_IDENTITY_FIELDS` = full_name / contact /
                            cv_filename_prefix) is now GATED rather than
                            defaulted: `require_identity()` raises
                            `CandidateIdentityMissing` and `generate_docs.main()`
                            exits 1 before writing a file — an aborted apply is
                            retryable, a CV mailed under the wrong name is not.
                            Personal-but-not-identity keys (employers,
                            profile_titles, school_keyword) default to EMPTY and
                            their consumers self-skip as "unmeasured" instead of
                            flagging every real role as unknown. Consumed by
                            generate_docs.py (identity), verdict_refine.py/
                            content_qa.py/apply_api.py/lang_guard.py (employers +
                            education), filter_config.py/filters.py/apply_shared.py
                            (location + languages), and the pracuj/theprotocol/
                            jobleads sources (listing-URL city slug).
  config.py                 ALL config: env vars, schedule, paths, source toggles.
                            FILTER re-exported from filter_config.py (below) for
                            backward compat — `from hunter.config import FILTER`
                            still works everywhere.
  filter_config.py          Shim: `FILTER = load_profile()` so every existing
                            `from hunter.config import FILTER` import keeps
                            working. Values + WHY-comments live in
                            filter_profile.builtin_defaults()
                            (docs/FILTERS_YAML_PLAN.md M1).
  filter_profile.py         Per-user filter profile loader. Layer 1 =
                            `builtin_defaults()` (today's FILTER, shared);
                            Layer 2 = optional user YAML merged on top
                            (replace / extend_only per knob). Cache keyed by
                            (resolved path, mtime_ns) so an external writer
                            (API PUT /filters, M5) is picked up without a
                            restart. Missing file ⇒ byte-for-byte Layer 1.
                            Home-city carve-out subtracts the candidate's own
                            aliases from `extra_anti_hybrid_cities`. Invalid
                            regex patterns dropped with a warning, never raise.
                            `FILTER["locations"]` still derived from
                            candidate.yaml home_city_aliases.
  models.py                 Job dataclass
  filters.py                Central filter: keywords, level, location, patterns, React-only, German.
                            Public APIs (`classify_job` / `apply_filters_with_stats` /
                            `screen_job_text` / `assess_job_text`) take optional
                            `flt=` (docs/FILTERS_YAML_PLAN.md M2); default is the
                            module FILTER so callers are unchanged. Helpers read
                            the profile via `_resolve_flt`. `_anti_hybrid_cities(flt)`
                            is the per-profile cached builder (module constant
                            `_ANTI_HYBRID_CITIES` kept for test imports).
                            `require_title_terms` / `exclude_stacks_without`
                            generalize the old require_angular /
                            exclude_react_without_angular knobs (loader still
                            honors the legacy keys). React-only exclusion is
                            gated by `_react_track_active()` (CANDIDATE_TRACKS/`/tracks` —
                            docs/quality/09-multi-track-react.md): a no-op when the react track
                            is active, unchanged (today's behavior) otherwise
  main.py                   Hunt loop: fetch -> filter -> dedup -> act. The ACT step's
                            AUTO_APPLY branch is gated by `APPLY_QUEUE_ENABLED` (docs/
                            HUNT_APPLY_SPLIT_PLAN.md M1): off (default) calls
                            `_auto_apply_all` inline exactly as before; on writes a
                            `PENDING` row per new job (`tracker.add_pending`) and
                            returns — `_hunt_lock` is held for seconds, not however
                            long a CLI-fallback apply batch takes. `apply_worker.py`
                            is the other half
  apply_worker.py           M1 apply-queue worker (docs/HUNT_APPLY_SPLIT_PLAN.md):
                            `apply_worker_loop(context, worker_id=0)` — a long-running
                            background task (started from `telegram_bot._post_init`
                            via `app.create_task`, gated by `APPLY_QUEUE_ENABLED`) that
                            drains the PENDING queue forever, independent of
                            `_hunt_lock`: `tracker.claim_pending()` (atomic
                            `UPDATE…RETURNING`) → the same `apply_agent.py` subprocess
                            the old inline `_auto_apply_all` used
                            (`run_apply_agent_subprocess`) → `_resolve_outcome` →
                            `deliver_apply_now` on success → sleep `APPLY_DELAY_SEC` →
                            repeat. Outcome handling mirrors `_auto_apply_all`:
                            `llm_outage`/`cli_timeout` call `tracker.release_claim`
                            (row goes back to `PENDING`, no FAIL row, `llm_outage` also
                            arms the M2 pause) instead of escalating `fail_count`;
                            `fail`/`rate_limited` write a normal FAIL row
                            (`tracker.add_failed`); `manual` (JobLeads) just notifies.
                            Same 3-consecutive-fail breaker as the batch loop, but
                            backs off `_BACKOFF_SEC` (5 min) and keeps draining instead
                            of exiting — this loop has no finite batch to "finish".
                            Respects `llm_outage.pause_remaining()` at the top of every
                            iteration. `worker_id` exists for a future N-worker
                            rollout (M2, deferred) — today only worker 0 runs
  llm_outage.py             M2 auto-apply pause after an LLM account outage (docs/
                            LLM_OUTAGE_RESILIENCE_PLAN.md): arm_pause/pause_remaining/
                            clear_pause over DB key `llm_outage_until` (config KV table —
                            survives the apply-subprocess boundary). Armed by main.py's
                            batch loops on outcome "llm_outage" (exit 46); hunt/retry
                            slots then skip their apply step SILENTLY (log only — the one
                            Telegram alert goes out at arm time) until expiry
                            (LLM_OUTAGE_PAUSE_MIN, default 60 min) or /llm outage clear.
                            Fetch/filter/dedup still run; skipped jobs return next hunt
  apply_failures_log.py     M4 fail-audit log (docs/HUNT_APPLY_SPLIT_PLAN.md): 
                            `log_apply_failure()` appends one JSON line
                            (`{ts, url, company, title, outcome, exit_code, error,
                            duration_sec, cli_mode}`) to `logs/apply_failures.jsonl`
                            (RotatingFileHandler, 5MB x5 backups, `propagate=False` so it
                            never duplicates into `hunter_errors.log`) for every "fail" /
                            "cli_timeout" / "rate_limited" outcome from
                            `hunter.services.apply_service`; "llm_outage" is deliberately
                            excluded (global account state, already tracked by
                            `hunter.llm_outage`). `read_last_failures(n)` is the read side,
                            used by `/fails [N]` (hunter/commands/fails.py). Best-effort —
                            a logging failure never breaks an apply run
  repost_gate.py            Same-vacancy re-post gate (Step 1.5g, $0): TF-IDF text match
                            of the fetched posting vs recent applied rows' job_posting.txt
                            + fuzzy company-name agreement → reuse the existing CV (copy
                            donor docs into `{Company}_reused_{donor-date}/`,
                            Re-application tracker row, cost $0, no LLM, no
                            shadow). Calibrated thresholds 0.94/0.90/0.85, min 1500 chars.
                            See "Pipeline Flow" step 3c + tools/reuse_calibrate.py
  telegram_bot.py           Thin dispatcher shim (~200 lines): imports all handlers, owns _post_init + build_application
  tracker.py                tracker.db (SQLite) CRUD: dedup, skip, fail, applied, manual (~1250 lines).
                            M1 PENDING queue (docs/HUNT_APPLY_SPLIT_PLAN.md): `add_pending(job)`
                            writes a placeholder row (`ats_status="PENDING"`, `pending_meta`
                            = the full `Job` JSON-serialized — `_serialize_pending_meta`
                            stringifies anything non-JSON-native, e.g. `datetime`) so
                            `is_known()` (status-agnostic by design) blocks the hunt loop
                            from re-queuing the same URL on the next cycle.
                            `claim_pending()` atomically claims the OLDEST PENDING row
                            (`UPDATE … SET ats_status='IN_PROGRESS', claimed_at=now
                            WHERE rowid = (SELECT rowid … ORDER BY rowid LIMIT 1)
                            RETURNING *` — `ORDER BY rowid` for real FIFO, since `id` is
                            a random UUID) and `job_from_pending_row()` reconstructs the
                            `Job` from `pending_meta`. `release_claim(url)` reverts
                            IN_PROGRESS → PENDING (outage/timeout retry, no data loss);
                            `reset_stale_claims(timeout_min)` does the same in bulk for
                            claims older than `APPLY_CLAIM_TIMEOUT_MIN` (crashed worker
                            recovery). `count_pending`/`count_in_progress`/`list_pending`
                            feed `/queue` and `/status`. Every terminal writer
                            (`add_applied`/`add_failed`/`add_skipped`/`add_react_skipped`/
                            `add_expired`) now checks `_is_known_terminal()` (True only for
                            a genuine non-placeholder row — `is_known()` itself is
                            unchanged and still counts placeholders, since its OTHER
                            caller is the hunt-loop dedup check) instead of `is_known()`,
                            and calls `_clear_own_placeholder()` first to delete its own
                            PENDING/IN_PROGRESS row before inserting the final status row
                            — otherwise the queue's own bookkeeping row would collide with
                            the real outcome row. `_COOLDOWN_SKIP_STATUSES` (feeds
                            `is_in_cooldown`/`company_cooldown_active`) and
                            `iter_unsent_rows`/`read_all_tracker_rows` (feeds
                            `expired_marker` + Sheets/Drive sync) all exclude PENDING/
                            IN_PROGRESS — a queued-but-not-yet-applied job isn't a real
                            application yet and must stay invisible to every downstream
                            consumer until the worker resolves it.
                            `convert_own_applied_row(url)` is the opposite direction
                            (2026-08-24): it turns THIS user's applied row back into a
                            terminal SKIP in place, for the CLI pipeline's
                            post-generation aborts (see "Aborting AFTER generation"
                            under Doc generation modes). In place, so an already-
                            mirrored Sheet row is corrected rather than duplicated;
                            SKIP/FAIL/EXPIRED/MANUAL rows and PENDING/IN_PROGRESS
                            placeholders are deliberately left alone.
                            Retry-vs-dead-postings (2026-08-10): `_convert_own_fail_row`
                            lets add_expired/add_skipped CONVERT this user's live FAIL row
                            to SKIP/EXPIRED (resp. SKIP/'—') in place — same id/sheets_row,
                            sheets_dirty=1 — instead of no-op'ing via _is_known_terminal;
                            add_applied deletes the user's own FAIL/SKIP/blank row before
                            its INSERT (the unique (user_id, url_norm) index otherwise made
                            a successful RETRY crash at the tracker write with
                            IntegrityError); `classify_retry_outcome(url)` tells the retry
                            loop what an exit-0 subprocess actually wrote
                            ("applied"/"expired"/"skipped"/"noop") — see hunter/main.py
                            _retry_failed, which no longer reports "Retry OK" for a dead
                            posting and escalates fail_count on a no-write exit 0
  tracker_cache.py          In-memory tracker cache (asyncio.Lock, O(1) dedup + stats)
  tracker_backup.py         Timestamped daily snapshots of tracker.xlsx
  lang_guard.py             Language routing + contamination guard: detect_posting_language()
                            (PL/EN by token density) + Polish-in-English / English-in-Polish
                            detection (diacritics + lexicon + suffix + bilingual gloss). Feeds
                            the apply enforce-gate (enforce_language_separation in apply_shared)
  resume_sanitizer.py       Strip LLM artifacts/foreign-language leakage from generated resume text
  text_repair.py            Repairs the SEAM left when a content-safety stage cuts a span out
                            of finished prose — shared by claim_judge._drop_quote, the prestige
                            scrub and the compliance scrub, all three of which used to tidy up
                            with their own global regexes over the whole field. Two defect
                            classes this closes reached a delivered PDF (2026-08-21):
                            `re.sub(r"\s{2,}", " ", text)` collapses NEWLINES, so cutting one
                            clause out of a cover letter flattened every paragraph break in the
                            whole letter — `collapse_spaces()` uses `[^\S\n]` instead; and the
                            junction itself was only patched for a few hard-coded pairs, leaving
                            ", - from a real-time…", "…decisions; across a team of 10+" and
                            bullets opening lowercase. `repair_junction(left, right)` fixes the
                            seam where the cut ACTUALLY happened (the caller knows both sides)
                            rather than guessing at it across untouched prose — safer AND more
                            complete. `cut_span()` = junction + edges + capitalization;
                            `drop_sentences()` is the paragraph-preserving sentence drop.
                            NOTE when editing: never interpolate a dash into a character class
                            (`[,;:.{_DASHES}]` makes ".-–" a RANGE over every ASCII letter) —
                            use the pre-escaped `_SEPARATOR_CLASS`; there is a regression test.
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
                            the scrubs and the language gate in both pipelines. The cut itself
                            is stitched back up by `hunter.text_repair` (above) — `_drop_quote`
                            owns WHICH span goes, not how the seam reads afterwards. See
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
                            cost_writer.py, verdict_writer.py, and apply_shared.ensure_pl_resume
                            ("apply.pl_mirror" — a silently dead PL mirror recreates exactly the
                            bug it closes, Polish employers receiving an EN CV, which went
                            unnoticed for a month the first time). Existing try/except are NOT
                            removed — the wrapper goes around them; a block that already
                            returns None/False on error re-raises from its except clause so the
                            failure still reaches best_effort() for counting
  gsheets_sync.py           High-level Sheets mirror (push/pull/resync/bootstrap)
  gsheets_client.py         Low-level Sheets API v4 wrapper
  gdrive_sync.py            High-level Drive upload (upload_application_folder). Every folder
                            resolution goes through `_resolve_folder()`, which serializes
                            get_or_create_folder behind `_DRIVE_LOCK` — the delivery hook and the
                            upload-missing backfill routinely overlap in this one event loop, and
                            interleaved list-then-create is what duplicated the date folders.
                            Deliberately NOT memoized: a cached id goes stale the moment a folder is
                            trashed/moved by hand and would silently absorb uploads into the trash.
                            `upload_missing_folders` is re-entrancy-guarded (module-level
                            `_backfill_running` bool, docs/GDRIVE_SSL_RACE_PLAN.md M1): the delivery
                            fallback and the scheduled backfill routinely fire within the same
                            second, and a second full pass over the identical folder list is not
                            just unsafe but pointless — the first pass already covers it. A second
                            concurrent call returns immediately with `"skipped_busy": True` and zero
                            counters (never an exception, so `best_effort` never counts it as a
                            failure); `/gdrive_upload_missing` reports "a backfill is already
                            running — skipped" instead of a misleading "Uploaded: 0". The guard sits
                            in a try/finally around the actual pass (`_upload_missing_folders_locked`)
                            so a mid-pass exception still releases it.
                            `_DRIVE_LOCK` + `_drive_call()` (M2, renamed from the narrower
                            `_FOLDER_LOCK`, which only ever covered folder resolution — the file
                            uploads it didn't cover are exactly where the SSL race lived) serialize
                            EVERY Drive API call in this process, not just folder resolution: the
                            cached googleapiclient service sits on one httplib2.Http object with one
                            keep-alive TLS socket, and httplib2 is not thread-safe — two calls in
                            flight at once write into the same TLS stream and read each other's
                            bytes, surfacing as `[SSL] record layer failure`. `_drive_call(fn, *args,
                            timeout=...)` is the one place every Drive call routes through: acquires
                            the lock, runs `fn` in a worker thread, optional wall-clock cap. Acquired
                            PER CALL, not per pass, so a post-apply targeted upload waits at most one
                            call behind a backfill pass. `upload_missing_folders`'s per-row
                            `asyncio.wait_for` timeouts (root resolve, per-row upload) call
                            `_invalidate_service()` on `asyncio.TimeoutError`: `asyncio.to_thread` is
                            not cancellable, so a timeout abandons a worker thread still holding the
                            service's socket — invalidating means the NEXT call gets a fresh service
                            and a fresh, distinct socket instead of racing the abandoned thread's
                            lingering one. Cross-*process* concurrency (detached dual-apply shadows)
                            is unaffected — each process has its own service/socket;
                            `gdrive_client._resolve_create_race` remains the guard there.
                            `_upload_shadow_subfolders` (M3, docs/GDRIVE_SSL_RACE_PLAN.md) consults
                            `hunter.drive_ledger` before each shadow subfolder upload and skips it
                            when unchanged since its last successful upload — dual-apply shadow sets
                            have no tracker row, so without this every one of them was re-uploaded on
                            EVERY backfill pass forever (measured on the live corpus: 86 distinct
                            shadow folders, ~1192 uploads/day, ~14 full passes). `force=True`
                            (threaded from `/gdrive_upload_missing force`) bypasses the ledger check
                            for one pass — the escape hatch for a folder deleted on Drive by hand,
                            which the ledger can't see — while still recording afterwards.
  drive_ledger.py           Content-signature ledger for dual-apply shadow uploads (M3): a small,
                            self-contained module (mirrors `hunter.source_health`'s own lazy-ensure
                            pattern for `source_runs` — not part of `hunter.db`'s `init_db()`) over a
                            lazily-created `drive_uploads` table in the same tracker.db:
                            `path TEXT PRIMARY KEY, signature TEXT NOT NULL, drive_url TEXT NOT NULL
                            DEFAULT '', uploaded_at TEXT NOT NULL`. `signature(folder)` is content-
                            derived (`file_count:max_mtime_ns:total_size` over direct files only,
                            matching `gdrive_client.upload_folder`'s own flat-folder assumption) —
                            deliberately NOT a marker file inside the folder, which would itself be
                            uploaded to Drive and lives in the gitignored, periodically-pruned
                            `Applications/` tree; the DB is the durable, already-mounted store.
                            `is_current(path, sig)` / `record(path, sig, url)` / `forget(path)` round
                            out the API; `record()` is called only after a successful upload so a
                            failed one is retried next pass, never silently dropped.
  gdrive_client.py          Low-level Drive API v3 wrapper. Drive allows same-named siblings, so
                            get_or_create_folder (a) converges on the OLDEST copy when duplicates
                            exist, so uploads stop scattering, and (b) re-lists after create and
                            yields to an older concurrent winner, trashing its own loser copy —
                            closing the cross-PROCESS race (detached dual-apply shadows) that no
                            in-process lock can see. tools/dedup_drive_folders.py merges historical
                            duplicates. `build_service` (M2) builds with an explicit
                            `httplib2.Http(timeout=GDRIVE_HTTP_TIMEOUT_SEC)` via
                            `google_auth_httplib2.AuthorizedHttp` (`build()` accepts either
                            `credentials=` or `http=`, never both) so a hung read dies inside its
                            worker thread instead of blocking it — and the shared TLS socket it
                            holds — forever. `google-auth-httplib2`/`httplib2` are already transitive
                            deps of `google-api-python-client` (no new dependency, no lock
                            regeneration)
  gmail_client.py           Gmail API wrapper
  oauth_alert.py            Detect Google OAuth token expiry (invalid_grant/RefreshError) at the
                            gsheets/gmail/gdrive auth boundary; refresh_or_alert() fires a
                            cooldown-deduplicated Telegram "re-auth needed" alert then re-raises
                            (a dead Sheets token once caused a false-EXPIRED cascade).
                            `invalid_scope` is classified too, with distinct alert wording — it
                            means a scope upgrade shipped without re-auth (2026-08-06 Gmail
                            incident), not a revoked token
  gmail_parsers.py          Parse job alert emails from various boards. A digest email links
                            SEVERAL jobs, so per-URL metadata must never come from the email
                            subject: `_title_from_url` derives it from the slug, and for
                            LinkedIn (slug = bare id) `_linkedin_cards` reads each card's own
                            title + company out of the HTML (<a> to /jobs/view/<id> whose text
                            is the title, followed by "<Company> · <City> (Remote|Hybrid)").
                            Where nothing is available the subject is still the fallback, but
                            then `_stub_company` tags the placeholder company per-URL
                            (`[linkedin]#<id>`) — a shared company + shared subject title made
                            `tracker.dedup_key` identical for every job of one email, so the
                            hunt dropped all but the first as "Dup company+title"
  gmail_report.py           Per-email hunt report: build_gmail_report() renders
                            [date · aggregator · subject → taken/dup/filtered]
                            per alert email (chunked under Telegram 4096). Fed by
                            GmailSource.last_email_log + per-job JobOutcome tags
  sent_parse.py             Parse the messy Sent column into a real date (parse_sent_date/classify)
  sent_normalizer.py        Build/write the clean "Applied Date" Sheets column L from Sent
  bot/
    state.py                Shared mutable state (_pending_jobs, _active_apply_urls, _force_waiting).
                            `_active_apply_urls` doubles as an in-flight concurrency guard
                            (`try_mark_apply_active`/`mark_apply_done`, keyed by
                            `tracker.normalize_url`): the auto-hunt loop
                            (`hunter.main._run_apply_agent`), manual paste/Apply-button
                            (`hunter.bot.apply_runner._run_apply_agent`) and the LinkedIn
                            batch flow all claim a URL before starting generation and
                            release it in a `finally`. Closes a race where tracker-based
                            dedup (`add_applied`, only checked at the END of a run) let
                            the SAME vacancy generate twice when a hunt-found job and a
                            manually pasted URL for it overlapped (owner report
                            2026-07-30: Billennium "Mid Frontend Developer" generated
                            twice, ~$0.64 combined — one copy silently orphaned since
                            `add_applied()` rejected its late tracker write). A second
                            request for an in-flight URL is skipped for $0 (new `duplicate`
                            outcome in `hunter.main._run_apply_agent`, handled in
                            `_auto_apply_all`/`_retry_failed` like `rate_limited` — no FAIL
                            row, job returns next hunt/retry slot) or told via Telegram
                            "Already generating" (manual paths).
    keyboards.py            _make_keyboard() — InlineKeyboardMarkup factory
    notifications.py        send_text(), send_job_cards(), _tg_notify()
    paste.py                _looks_like_paste(), _extract_url(), URL_RE
    formatters.py           _build_schedule_text(), _format_check_responses_report(), _format_daily_summary()
    apply_runner.py         _run_apply_agent(), _run_linkedin_batch(), _handle_paste()
  commands/                 One file per Telegram command handler
    start.py                /start
    schedule.py             /schedule
    unsent.py               /unsent
    status.py               /status — shows PENDING/IN_PROGRESS apply-queue counts too
                            when `APPLY_QUEUE_ENABLED` (docs/HUNT_APPLY_SPLIT_PLAN.md M1)
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
    retry_reset.py          /retry_reset [all|URL] — report/revive FAIL rows that hit
                            MAX_FAIL_RETRIES and were dropped from the retry loop forever
                            (tracker.get_gave_up_failed/reset_fail_counts; report-first —
                            no-arg form never mutates. docs/LLM_OUTAGE_RESILIENCE_PLAN.md M3)
    fails.py                /fails [N] — last N (default 10, max 30) entries from the
                            apply-failure audit log (hunter.apply_failures_log, M4 —
                            docs/HUNT_APPLY_SPLIT_PLAN.md); read-only
    queue.py                /queue [limit] — M1 apply-queue introspection (docs/
                            HUNT_APPLY_SPLIT_PLAN.md): PENDING/IN_PROGRESS counts +
                            the oldest `limit` (default 10) PENDING jobs FIFO
                            (`tracker.count_pending`/`count_in_progress`/`list_pending`);
                            reports "queue disabled" when `APPLY_QUEUE_ENABLED` is false.
                            Read-only
    health.py               /health — per-source scraper yield report (source_health)
    llm.py                  /llm [name] — show/switch active LLM profile (hunter.llm_profiles);
                            /llm outage [clear] — show/lift the M2 auto-apply outage pause
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
    grid.py                 Hunt-slot time arithmetic (pure ints, no PTB/pytz imports):
                            parse_hhmm / parse_blackout / blackout_covers_day /
                            fire_minute. Owns the `SCHEDULE_BLACKOUT` quiet-hours walk —
                            offsets accumulate through ALLOWED minutes only, jumping over
                            the window, so every source keeps a slot and their order is
                            preserved. No blackout ⇒ byte-for-byte the old
                            `(base + i*offset) % 1440` grid
    hunt.py                 scheduled_hunt
    retry_failed.py         scheduled_retry_failed (RETRY_FAILED_TIMES, default 02:45/07:45)
    check_expired.py        scheduled_check_expired
    tracker_backup.py       scheduled_tracker_backup
    gdrive.py               scheduled_gdrive_upload_missing (every GDRIVE_UPLOAD_MISSING_INTERVAL_MIN)
    gsheets.py              scheduled_gsheets_resync + scheduled_gsheets_pull
    pending_report.py       scheduled_pending_report
    email_responses.py      scheduled_check_email_responses
    daily_summary.py        scheduled_daily_summary
    normalize_sent.py       scheduled_normalize_sent (daily 00:20, refreshes Sheets column L)
    apply_queue.py          scheduled_reset_stale_claims (every 15 min, no-op unless
                            `APPLY_QUEUE_ENABLED`) — M1 crashed-worker recovery:
                            `tracker.reset_stale_claims(APPLY_CLAIM_TIMEOUT_MIN)` moves
                            any `IN_PROGRESS` row claimed longer ago than the timeout
                            back to `PENDING` so a killed worker never strands a job
                            forever. See docs/HUNT_APPLY_SPLIT_PLAN.md
    __init__.py             register(app, tz) — wires all callbacks into the Application
  services/
    apply_service.py        Subprocess wrapper for apply_agent + generate_docs cmd builder.
                             ApplyOutcome includes "cli_timeout" (docs/HUNT_APPLY_SPLIT_PLAN.md
                             M3): when a subprocess `asyncio.TimeoutError` fires AFTER
                             `_effective_timeout` widened the caller's budget (CLI-eligible
                             run), that's an infrastructure timeout, not the vacancy's fault —
                             distinct from a plain "fail" so callers never write a FAIL row or
                             escalate fail_count for it. `_auto_apply_all`/`_retry_failed`
                             (hunter/main.py) and `bot/apply_runner._run_apply_agent` all treat
                             it like `llm_outage` (no FAIL row, Telegram notify, URL/job returns
                             next hunt) EXCEPT it does NOT stop the batch or arm any pause —
                             it's per-job infrastructure flakiness, not a global account state.
                             M4 (docs/HUNT_APPLY_SPLIT_PLAN.md): every non-ok, non-manual outcome
                             ("fail"/"cli_timeout"/"rate_limited" — NOT "llm_outage", which is
                             global account state tracked separately by hunter.llm_outage) also
                             calls `hunter.apply_failures_log.log_apply_failure()`, appending one
                             JSON line to `logs/apply_failures.jsonl` (RotatingFileHandler, 5MB x5).
                             Read via `/fails [N]` (hunter/commands/fails.py, default 10/max 30).
                             M6 (docs/STACK_PRESCREEN_PLAN.md): outcome `"cli_no_output"`
                             (exit 47, `APPLY_CLI_NO_OUTPUT_EXIT_CODE`) — the Claude CLI
                             exited 0 with no usable package, almost always because the
                             skill asked a question nobody could answer. Handled exactly
                             like `cli_timeout` everywhere (no FAIL row, no fail_count
                             escalation, no outage pause, row back to PENDING) but
                             reported distinctly: folding it into exit 46 armed an
                             hour-long auto-apply pause on a perfectly healthy API
                             account. The one place it still reports an outage is the
                             API-outage CLI fallback, where the account really IS down.
    tracker_service.py      High-level: should_skip_url(), record_successful_apply()
  sources/                  24 scrapers (see table above) + per-site detail-page fetchers
    base.py                 BaseSource ABC: search() / matches_url() / fetch_text()
    __init__.py             ALL_SOURCES registry + fetch_job_text(url, use_session=False)
                            dispatcher (use_session=True → LinkedIn session fetch in apply)
    html_fallback.py        Generic HTML -> text fallback + clean_url() helper.
                            extract_text(html) is the text step on its own, for a
                            source that fetches the HTML itself to read markup
                            get_text() discards (LinkedIn's apply CTA)
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

prompts/                        System-level LLM instructions (see prompts/README.md).
                                Candidate-personal files moved to candidate/ (see above).
  README.md                     What lives here vs candidate/
  generation_rules.md           LLM instructions for resume/CL generation [tracked]
  judge_rules.md                Claim-judge instructions [tracked]

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
tests/test_handoff_readiness.py CI gate that keeps ONE person's personal data out of
                            shared code (added 2026-08-12). Three checks: (a) no owner
                            name / phone / email / LinkedIn handle / employer / school /
                            VPS address as a literal anywhere in `hunter/` +
                            the root entry scripts — docs/ and tests/ are exempt
                            (AGENT_LOG legitimately quotes past incidents, and this file
                            must name the strings it forbids); (b) every variable
                            docs/SETUP_NEW_USER.md tells a user to set is actually present
                            in `.env.example`; (c) every `candidate.get("a.b")` dotpath in
                            production code exists in `candidate/candidate.yaml.example`.
                            Exists because the manual readiness audit was run three times
                            in three weeks and found NEW owner defaults every time — not a
                            regression each time, but a rule (`default` reproduces the
                            owner's value) that kept legitimately producing them. A
                            continuously-regenerating source needs a gate, not an audit.
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
                            them) left behind by the pre-fix list-then-create race in gdrive_client.
                            RECURSIVE: each dup date folder holds its own copy of the SAME company
                            subfolders (Santander in 4 of 6 "2026-07-06" copies on the live data), a
                            company's files split across them — so it walks the tree, keeps the OLDEST
                            copy of each name (the one the bot now picks), and merges every other copy
                            INTO it down to the files. Only a file-vs-file name clash is a conflict,
                            left in place (never overwritten) and its parent kept; a folder emptied by
                            the merge is trashed. The root-level pass groups purely by name, so the
                            duplicated "Logs" folders (upload_log_file's target; Drive-for-Desktop
                            renders the same-named siblings as "Logs (1)"…) merge the same way.
                            Loads the whole tree into memory once and plans against that, so the dry
                            run (default; --apply to merge) predicts --apply byte-for-byte. Trash only,
                            never a hard delete. Tested against an in-memory fake Drive in
                            tests/test_dedup_drive_folders.py (incl. the same-company-across-copies
                            scatter + dry-run==apply plan equivalence)
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
tools/reuse_calibrate.py    CV-reuse calibration (measure-first gate for the "reuse a past
                            generated CV for a new vacancy" idea): offline replay over
                            Applications/** — for each vacancy, rank STRICTLY EARLIER
                            applications by TF-IDF similarity of the postings, score the
                            top-K donors' resume_en against the NEW posting with the same
                            deterministic checker production uses (ats_checker.check,
                            run_llm_review=False), compare with the actual CV re-scored the
                            same way. Reports hit rate / donor-adequacy per similarity
                            threshold + a cost projection from tracker.db cost_usd.
                            Read-only, zero LLM calls, zero network; shadow subfolders
                            excluded. Decides whether a warm-start/reuse gate is worth
                            building at all — run on the deploy host where the corpus lives

.claude/                    Claude Code tooling for this repo (tracked). Agents live in
                            .claude/agents/*.md, skills in .claude/skills/<name>/SKILL.md,
                            slash commands in .claude/commands/*.md, hooks in .claude/hooks/*.py
                            (wired to events by .claude/settings.json — a script sitting in
                            hooks/ runs only if that file references it; both scripts were
                            dead config until 2026-08-11). Every agent/skill/command file
                            carries YAML frontmatter: agents+skills `name`+`description`,
                            commands `description`+`argument-hint` (without it the command
                            list shows the file's first line as its description).
  agents/
    scraper-health-checker.md   Audit all enabled scrapers, PASS / NEEDS ATTENTION per source
    project-invariants-review.md Review the branch diff against THIS repo's invariants — the
                            CLAUDE.md rules no linter can check (CLAUDE.md kept in sync,
                            best_effort() wrapping, requirements.lock regenerated, all five
                            source-registration points, tracker column constants, English-only
                            commits, no protected files staged, mypy baseline 223 not grown,
                            speculative-LLM-layer question, work-log entry). Finds no bugs and
                            no style issues BY DESIGN — /code-review and ruff own those
    fail-forensics.md       Why one vacancy produced no application: reconstructs the run from
                            tracker.db + logs/hunter_errors.log + the Applications/ folder,
                            pins the pipeline stage (fetch / expired / doomed gate / re-post
                            gate / LLM / judge / lang gate / render / verdict / delivery) via a
                            signature table, and rules on whether a retry is worth real money
  skills/
    debug-scraper/          Diagnose a broken scraper against live HTML/JSON
    release-notes/          Changelog master..develop, appended to DEPLOY.md
    mutation-verify/        Prove a test has teeth: mutate the production line it guards, run
                            ONLY that test, require a RELEVANT failure (an ImportError proves
                            nothing), restore, confirm green. Formalises the "mutation-verified"
                            convention already used in the Agent Work Log
    cost-audit/             Where the LLM money goes: cost_usd totals/median/tail, spend by
                            ats_verdict band, funnel correlation via tools/verdict_funnel_corr.py,
                            and whether the verdict-refine loop earns its cost
  commands/
    add-source.md           Add a new job source (all five registration points)
    apply.md                Generate a tailored application package. NOT just a desktop
                            convenience — this file is the CLI pipeline's live prompt
                            (`hunter/apply_cli.py` runs `claude -p "/apply …"` with
                            cwd=PROJECT_DIR, which is why `.dockerignore` re-includes
                            `.claude/commands/` into the image). Repo files are addressed
                            relative to the root; the candidate's own files are resolved
                            from `CANDIDATE_YAML_PATH` (per-user since the multi-user
                            migration — `/app/candidate` is empty in prod).
                            **Opens with a "decide, never ask" rule** (2026-08-24):
                            `claude -p` is non-interactive, so a clarifying question
                            does not pause the run, it ENDS it — no output folder,
                            `main_cli` raises, and the vacancy comes back with an
                            alert after burning the full 600 s timeout. Measured on
                            the deploy host: 5 of 60 retained runs died that way.
                            Every deterministic screen already ran before the skill
                            starts and the post-generation gates run after it, so the
                            skill is not a gate — it states its concerns in the Step 6
                            summary and generates anyway
    pr.md                   Open a PR with this repo's pre-flight: fetch → verify the branch is
                            cut from CURRENT origin/master (new branch, never a rebase) → ruff
                            check + format + pytest → project-invariants-review → English-only
                            body, no Co-Authored-By → gh pr create
    plan-doc.md             Write docs/<NAME>_PLAN.md BEFORE code: check AGENT_LOG for an
                            already-rejected version of the idea, then M0 = a free, read-only
                            measurement with its decision rule stated up front
  hooks/
    block_protected.py      PreToolUse (Edit|Write|NotebookEdit) — refuse edits to
                            .env / tracker.xlsx. Exits 2, not 1: only exit code 2 is a
                            BLOCKING hook error, any other non-zero merely prints and
                            lets the edit through (which is what it used to do)
    syntax_check.py         PostToolUse (Edit|Write) — py_compile after every .py write;
                            exits 2 so the SyntaxError is fed back to the model
  settings.json             Team-scoped Claude Code settings (tracked): the `hooks` block
                            that maps the two scripts above onto PreToolUse/PostToolUse.
                            settings.local.json (untracked) stays for personal permissions

.githooks/pre-commit        Git pre-commit hook (tracked, sh): refuses staged never-commit
                            files (.env, tracker.xlsx, *token*.json, Applications/, backups/,
                            candidate/notes/) and runs `ruff check` + `ruff format --check`
                            on the staged .py files — the same two gates CI enforces.
                            NOT active by cloning; enable once per clone with
                            `git config core.hooksPath .githooks`. Bypass: `--no-verify`.
                            .gitattributes forces LF here — core.autocrlf=true would
                            otherwise hand sh a CRLF shebang on Windows checkouts

                            NOTE — `cost-audit` and `fail-forensics` read LIVE data that does
                            not exist in a dev checkout: `tracker.db` here is a stale 14-row
                            fixture WITHOUT the `cost_usd` / `ats_verdict` / `fail_count`
                            columns (they arrive via hunter/db.py's startup migrations), and
                            `logs/` is absent entirely. Both tools probe first and degrade to
                            "unmeasured" instead of dying on `no such column`, but real numbers
                            require the deploy host: `docker compose exec job-hunter python ...`.

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
candidate/                  All candidate-personal files (see candidate/README.md):
                            candidate.yaml (identity/location/languages/employers, gitignored),
                            candidate_profile.md (career narrative for LLM, gitignored),
                            base_cv_*.md (pre-polished bullets per stack, gitignored),
                            *.example.md / *.example (placeholder templates, tracked),
                            examples/ (few-shot cover letters / about-me),
                            notes/ (private interview notes, gitignored)
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
| `TELEGRAM_CHAT_ID` | — | Required. Since B3 this is the ADMIN chat: it always counts as the owner (hunter/bot/auth.py) and receives system/health alerts; regular users are identified via `/link` + `telegram_links` instead. |
| `DEFAULT_USER_ID` | — | The owner's `users.id` from the API's app.sqlite (multi-user, docs/MULTI_USER_UPDATE.md). Stamps every tracker write in the bot process; `JOB_HUNTER_USER_ID` (injected into per-user apply subprocesses by `hunter.users.user_env`) wins over it — see `config.current_user_id()`. Unset = single-user dev mode. |
| `USERS_ROOT` | `./users` | Per-user storage root, `users/{userId}/{candidate,Applications,templates}` — shared with job-hunter-api via the compose mount. `hunter.users.user_paths()` is the accessor. |
| `AUTO_APPLY` | `false` | Auto-generate docs without manual button press |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, or `openrouter`. **Prefer `/llm <profile>` in Telegram** — the profile system (`hunter/llm_profiles.py`) is the recommended way to switch models at runtime without restart. |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model for API mode (effort `low` + thinking disabled on supporting models). **Source of truth is this `config.py` default — leave `LLM_MODEL` unset in `.env` so model upgrades ship as a commit, not a manual prod edit.** Set it in `.env` only to override (experiment/temporary). Dated snapshots retire (`claude-sonnet-4-20250514` → 2026-06-15, `claude-3-5-haiku-20241022` → 2026-02-19); prefer non-dated aliases. |
| `LLM_DEFAULT_PROFILE` | — | Pin a named profile as default (e.g. `deepseek-r1`). Overrides `LLM_PROVIDER+LLM_MODEL`. Persisted per-vacancy selection via `/llm <name>` wins over this. |
| `DUAL_SHADOW_PROFILE` | `deepseek-v3` | Profile used for the dual-apply shadow comparison run. DB key `dual_shadow_profile` wins over this env fallback — set it at runtime via `/dual shadow <name>` in Telegram (e.g. `/dual shadow deepseek-v4-pro`). Toggle dual mode itself with `/dual on`/`/dual off` (DB key `dual_apply_enabled`). |
| `CANDIDATE_TRACKS` | `angular` | Which stacks the candidate is applying for (docs/quality/09-multi-track-react.md). Default is today's behavior unchanged — React-only vacancies are filtered at three points (listing filters, apply Step 1.5c pre-LLM check, apply Step 4.5 post-generation check). Set `angular,react` to also apply to React-only roles (uses `candidate/base_cv_react.md`, already-existing infra). Runtime override without a bot restart: `/tracks angular\|react\|both` (DB key `tracks_enabled` wins over this env var, same DB-wins-over-env pattern as `DUAL_SHADOW_PROFILE`). `hunter.config.active_tracks()` is the read helper. |
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
| `ATS_VERDICT_MAX_REFINES` | `5` | Max escalating rewrite rounds the refine loop runs when the verdict is below target (rounds 1–3 honest, round 4+ stretch — `verdict_refine.STRETCH_FROM_ROUND`). Default `5` (owner decision 2026-08-10: three honest passes + two openly-add-skills rounds — prod serves refine calls through the flat-cost CLI subscription, so extra rounds are ~free; supersedes the 3-round default of 2026-07-07). `0` disables the loop (old one-shot verdict). See docs/VERDICT_REFINE_PLAN.md. |
| `OUTREACH_ENABLED` | `true` | After each successful apply (both pipelines, Step 7.8), write `outreach.md` into the application folder next to the CV: recruiter contact parsed from `job_posting.txt` (`hunter/contact_extract.py`, regex, $0) + a ready-to-paste ≤300-char LinkedIn message in the posting's language (+EN version for PL postings; one `JUDGE_MODEL` call grounded only in the already-judged content.json — no fresh fabrication surface). Rides the existing Drive folder upload. Best-effort — never blocks/fails the apply; the bot NEVER sends the message anywhere (owner sends manually). No Telegram/Sheets changes (owner decisions 2026-07-10). See issue #138. |
| `DOOMED_GATE_ENABLED` | `true` | Deterministic (regex-only, zero LLM cost) full-text screen (`hunter.apply_shared.run_doomed_gate` → `hunter.filters.assess_job_text`), run right after expired-check and before the first LLM call in both pipelines (Step 1.5f). HARD findings (non-Poland onsite/hybrid, non-EU work authorization, unsupported required language, foreign stack / game engine with no Angular or React anywhere) write a SKIP tracker row and abort generation for $0.00; SOFT findings (e.g. a Vue/Svelte-first web role) warn in Telegram and generation continues. Force-mode/manual-paste always degrades HARD to warn. See docs/DOOMED_GATE_PLAN.md. |
| `DOOMED_GATE_HARD_ACTION` | `skip` | `skip` aborts generation on a HARD finding; `warn` is an emergency lever to downgrade every HARD finding to a warning without disabling the gate entirely (e.g. if live-data precision turns out worse than calibration). |
| `REPOST_GATE_ENABLED` | `true` | Re-post gate (`hunter/repost_gate.py`, Step 1.5g, $0): when the fetched posting is a near-verbatim re-post of a vacancy applied to in the last `REPOST_WINDOW_DAYS` days (new URL — re-listed after expiry, cross-board dup, agency name variation), REUSE the existing CV: copy the donor folder's docs, write a Re-application tracker row at cost $0, stamp the donor verdict, skip generation and the dual-apply shadow. Ambiguous band (sim 0.85–0.90, agency-boilerplate territory) only warns. `/force` bypasses. Thresholds calibrated 2026-07-20 (tools/reuse_calibrate.py) live as module constants. |
| `REPOST_WINDOW_DAYS` | `60` | How far back the re-post gate looks for donor applications. |
| `APPLICATIONS_DIR` | `Applications/` | Output folder override (useful for preview/testing) |
| `CV_GDPR_CLAUSE` | `both` | GDPR/RODO consent clause at CV bottom: `both` (PL+EN), `pl` (PL CV only), `none` |
| `MAX_JOBS_PER_RUN` | `40` | Cap per hunt cycle (auto-apply only, applied after filter+dedup; raised 20→40 2026-07-10 — a lower value in the prod `.env` overrides this default) |
| `APPLY_DELAY_SEC` | `30` | Pause between auto-apply jobs |
| `APPLY_QUEUE_ENABLED` | `false` | Hunt / apply split (docs/HUNT_APPLY_SPLIT_PLAN.md M1): with the flag off (default), the hunt loop calls `_auto_apply_all` inline exactly as before — `_hunt_lock` stays held for the whole apply batch. When on, `_run_hunt_impl`'s AUTO_APPLY branch writes a `PENDING` row per new job (`tracker.add_pending`) and returns immediately (`_hunt_lock` held for seconds, fetch+filter+dedup only); a background `apply_worker_loop` task (`hunter/apply_worker.py`, started from `_post_init`) drains the queue independently — claim (`tracker.claim_pending`, atomic `UPDATE…RETURNING`) → run the same `apply_agent.py` subprocess → resolve the outcome → `deliver_apply_now` → sleep `APPLY_DELAY_SEC` → repeat, forever. `llm_outage`/`cli_timeout` release the claim back to `PENDING` (no FAIL row, same M2/M3 semantics as the old inline path, now living in the worker instead of `_auto_apply_all`); `fail`/`rate_limited` write a normal FAIL row. `/queue` (hunter/commands/queue.py) lists PENDING jobs; `/status` shows PENDING/IN_PROGRESS counts when the flag is on. M2 (N>1 workers) is explicitly deferred — `apply_worker_loop(context, worker_id=0)` already takes a `worker_id` so a future rollout is a config change, not a rewrite. |
| `APPLY_CLAIM_TIMEOUT_MIN` | `60` | A `PENDING` row claimed by a worker (`ats_status` → `IN_PROGRESS`, `claimed_at` stamped) but never resolved within this many minutes means the worker crashed mid-run — `hunter.schedules.apply_queue.scheduled_reset_stale_claims` (every 15 min, no-op unless `APPLY_QUEUE_ENABLED`) resets it back to `PENDING` (`tracker.reset_stale_claims`) so it isn't stuck forever. |
| `SCHEDULE_TIMES` | `02:00,05:00,08:00,13:00` | Base trigger times for a full sweep of every source (comma-separated HH:MM, Warsaw). Night-weighted since 2026-08-16 — two of the four cycles start inside 02:00–08:00 and the old 19:00 base is gone. Was a hardcoded list in `config.py`; now env-overridable. With `APPLY_QUEUE_ENABLED` the apply worker claims a queued job within ~15 s, so this grid decides when documents are GENERATED, not just when vacancies are found. |
| `SCHEDULE_BLACKOUT` | `18:00-00:00` | Quiet hours: no hunt slot fires inside this window (`HH:MM-HH:MM`, may wrap midnight, end `00:00` = end-of-day; empty disables). Enforced by `hunter/schedules/grid.py::fire_minute`, which walks the per-source offsets through allowed minutes only rather than skipping slots — see the Schedule section for why picking base times cannot express this. Malformed or whole-day values warn and fall back to the plain modulo grid. |
| `RETRY_FAILED_TIMES` | `02:45,07:45` | When to retry FAILed tracker rows (comma-separated HH:MM, Warsaw). Used to run after EVERY per-source AUTO_APPLY hunt (72×/day), which kept `_hunt_lock` busy past the 40-min slot spacing. Minutes :45 never collide with the hunt grid (fires only at :00/:20/:40). Moved into the night window 2026-08-16 with the rest of the schedule — a retry runs the same apply pipeline, and 18:45 sat inside the new quiet hours. |
| `LLM_OUTAGE_PAUSE_MIN` | `60` | How long auto-apply pauses after an LLM account outage (drained balance / bad key → `llm_client.LLMOutageError` → exit 46 → outcome `llm_outage`). Time-boxed, not sticky: after expiry the next slot probes with ONE job/API call; still dead → re-arms. One Telegram alert at arm time; paused slots skip silently (fetch/filter/dedup still run, jobs return next hunt). `/llm outage [clear]` inspects/lifts; shown in `/status`. See docs/LLM_OUTAGE_RESILIENCE_PLAN.md M2. |
| *(CLI outage fallback — no env var)* | login = switch | M4/M4b: on an LLM account outage, calls are served through the Claude CLI (Pro subscription) instead of failing. **Live in prod since 2026-07-29** (OAuth login done, `claude -p` smoke-tested in the container). **No feature flag** (owner decision 2026-07-18): the switch is the CLI login itself — credentials in the mounted `./.claude-cli/` volume (`llm_client.cli_credentials_present`) make the fallback live; an empty dir disables it (`claude /logout` in the container to turn off). Two layers: (1) inside `llm_client.call_llm` — ANY call (generation, judge, verdict, refine, translate, outreach) hit by `LLMOutageError` gets ONE `claude -p` retry with the same prompt via stdin, **pinned `--model <requested>`** (2026-08-10 fix: unpinned calls were served by the subscription's DEFAULT model — the verdict judge stopped being Haiku and CLI-served verdicts averaged ~3 points below API-served ones; if the subscription rejects the pinned model, ONE unpinned retry runs — a default-model answer still beats re-raising; no unpinned retry on missing binary/timeout) (dual-apply shadow excluded — a substituted model would poison the A/B; CLI failure re-raises the ORIGINAL outage so exit-46 semantics hold); (2) `apply_agent.main()` pipeline-level retry via `main_cli` as the second line. Dispatch is fixed: `LLM_API_KEY` set → **paid API primary** (the old "CLI detected → try CLI first" auto-preference is REMOVED — desktop bare runs now cost API money; use `--cli`/`APPLY_USE_CLI=true` for subscription-only, `tools/preview_apply.py` passes `--cli` explicitly), switches back automatically once topped up; no key → CLI-only. Activation was a one-time `docker compose exec -it job-hunter claude` OAuth login on the deploy host (done 2026-07-29; the token lives in `./.claude-cli/.credentials.json` there — personal subscription credentials, treat like `gsheets_token.json`, never commit). CLI-served calls record no usage → Cost $ understates outage-window runs. |
| `APPLY_AGENT_TIMEOUT_SEC` | `900` | Subprocess timeout (15 min) |
| `APPLY_AGENT_CLI_TIMEOUT_SEC` | `10800` | Wall-clock cap when the run MAY go through the Claude CLI (explicit `APPLY_USE_CLI`, or a CLI login present — the outage fallback can fire mid-run and the parent can't know in advance). A CLI-served vacancy spawns ~10–20 sequential `claude -p` calls (M4b), far past the 15-min API budget — killing it at 900s would create the very FAIL row the outage work eliminates. Raised 2700→10800 (2026-08-10, owner: "время есть, пускай ковыряется"): the 5-round refine loop alone can burn ~110 min of ≤600s CLI calls (`llm_client.CLI_CALL_TIMEOUT_SEC`) on top of generation. `apply_service._effective_timeout` picks max(base, this). Long CLI batches are absorbed by the existing FIFO hunt queue (`_hunt_lock` — waiting slots run late, never skip). |
| `DUAL_SHADOW_TIMEOUT_SEC` | `3600` | Hard wall-clock cap for the detached dual-apply shadow run (its own watchdog; independent of the primary timeout). Raised from 900 when the shadow gained the judge + verdict-refine stages (2026-07-09), and 1800→3600 (2026-08-10) when the refine loop grew to 5 rounds. |
| `LINKEDIN_STORAGE_STATE` | — | Path to a Playwright session JSON from `python tools/linkedin_login.py`. Used by the apply pipeline only (`fetch_job_text(url, use_session=True)` → `LinkedInSource.fetch_text_with_session`) so the logged-in page reveals "No longer accepting applications" and `expired_check` aborts before any LLM spend (~$0.31 saved per dead LinkedIn URL). **Must be a path INSIDE the container** (`/app/.secrets/...`); a stale value silently degrades to the guest fetch — `_storage_state_path()` returns None for a non-existent file, and `fetch_text_with_session` falls back without raising. Since 2026-08-22 that fallback is no longer blind: `linkedin.guest_html_expired()` reads the closed-posting signature out of guest HTML (see the Scraper Health Notes row), so a dead posting is still a $0 EXPIRED skip. `/check_expired` / gmail enricher intentionally do **not** use the session (they get the same guest-HTML check instead). Once set, drop `linkedin.com` from `GMAIL_ENRICH_SKIP_HOSTS`. |
| `LINKEDIN_TPR` | `r604800` | LinkedIn guest-search recency window (`r86400` = 24h, `r604800` = 7 days). Widened from a hardcoded 24h on 2026-08-12: the two windows return genuinely different sets (only 14 shared ids of 57/69 measured) and the 7-day set is far more on-target (49% of rows carry "angular" in the title vs 11% for 24h). URL dedup in the hunt loop makes the wider window free. Unlike `f_E`/`f_WT` (both silently ignored by this endpoint) this parameter is really honoured. |
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
| `GDRIVE_HTTP_TIMEOUT_SEC` | `60` | Socket-level timeout on the Drive service's underlying `httplib2.Http`. Without it a hung read can block a worker thread — and the shared TLS socket it holds — forever, which is how one abandoned request used to poison every other concurrent Drive call (`[SSL] record layer failure`). See docs/GDRIVE_SSL_RACE_PLAN.md M2. |
| `GDRIVE_UPLOAD_MISSING_INTERVAL_MIN` | `30` | Drive backfill interval for application folders that missed their instant post-apply upload (`hunter/delivery.py`). Was hardcoded 3 h — the "not on Drive yet" lag the owner reported 2026-07-12. Idempotent (skips rows that already have a Drive URL). |
| `GMAIL_LOOKBACK_HOURS` | `25` | How far back the Gmail scan reads the inbox (hours) |
| `GMAIL_MAX_RESULTS` | `100` | Max alert emails per scan; report warns if ceiling hit |
| `GMAIL_ENRICH_CONCURRENCY` | `5` | Global cap on parallel enrichment fetches (all hosts) |
| `GMAIL_ENRICH_DOMAIN_LIMIT` | `2` | Default per-host concurrent enrichment fetches |
| `GMAIL_ENRICH_DOMAIN_DELAY` | `0.0` | Default per-host delay (sec) between enrichment fetches |
| `GMAIL_ENRICH_SKIP_HOSTS` | `linkedin.com,pracuj.pl` | Hosts NOT enriched during the hunt (they hard-block → 429/403 and poison the shared rate budget). The email-derived stub is kept. Comma-separated; remove a host once it fetches reliably. |
| `GMAIL_LABEL_PROCESSED` | `true` | Apply a "Job Hunter/Processed" Gmail label to every alert email the bot reads during a hunt. Requires `gmail.modify` scope — re-run `python tools/gmail_auth.py` after upgrading from a pre-labeling install. A pre-labeling `gmail.readonly` token no longer breaks the source (2026-08-06 fix — `get_gmail_service` loads the token with its OWN granted scopes instead of forcing the code's SCOPES, which made the refresh die with `invalid_scope`): reading keeps working, labeling is disabled with a logged warning until re-auth. |
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
file the standalone LinkedIn posts scout writes — external PRIVATE repo,
see "LinkedIn Posts Scout" below),
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
1. `sources.fetch_job_text(url, use_session=True)` — fetch full job description
   (LinkedIn uses Playwright + `LINKEDIN_STORAGE_STATE` so expired markers are visible)
2. Save `job_posting.txt` to output folder
3. `expired_check.is_job_expired(text)` — skip if expired. Runs BEFORE the
   too-short abort (Step 1.5a vs 1.5b since 2026-08-10): deleted postings are
   often served as a short synthetic marker ("This job posting has expired.",
   29 chars — findmyremote/Lever/thesmartjobs fetchers), and the old order let
   the 300-char floor swallow the marker, so the clean $0 EXPIRED skip never
   happened. An existing FAIL row for the same URL is converted in place to
   SKIP/EXPIRED (`tracker._convert_own_fail_row`), not silently dropped.
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
   low-frequency-hybrid mention; frequent office presence in a Polish city
   outside Wrocław — `pl_onsite_or_frequent_hybrid`, an explicit ≥2-days/week
   phrasing or strict on-site wording near a PL anti-hybrid city, vetoed by
   the same low-frequency exception (Sent-notes audit 2026-08-08: "praca
   stacjonarna Opole"/"hybrid Warsaw 3 days" always wasted generation; bare
   "hybrid"+city with no frequency stays SOFT per M4 calibration); non-EU
   work authorization — W2/C2C/
   H1B/US citizen/green card/security clearance; a required language the
   candidate doesn't speak; an unambiguous foreign stack (PHP/WordPress/
   Joomla/Drupal/.NET/Blazor/Mendix/…) in a posting that never mentions
   Angular or React at all — `foreign_stack_no_angular`, nice-to-have
   mentions vetoed by the optional-context guard, C# deliberately excluded
   (a real .NET job names ".NET" explicitly; a game-dev role listing C# is
   caught by `stack_mismatch_game_engine` below instead); a game-engine-first
   role — `stack_mismatch_game_engine`, Pixi/Cocos/Phaser/Babylon/Haxe/Godot/
   GameMaker/Unreal plus **bare "Unity"** (proved to be the engine, not the
   English word, by a Unity-ecosystem token — C#/WebGL/prefab/GameObject/
   shader/Unity Test Framework/game/3D — within 100 chars;
   `filters._unity_engine_match`), again only when neither Angular nor React
   appears anywhere; a nice-to-have engine mention stays SOFT; RU-market signals — the
   `russia_remote_market` patterns incl. ТК РФ, ruble salaries and
   Moscow/St-Petersburg location tags; a known AI-training/staffing-mill
   name in the
   BODY text — `ai_mill_body`, scans the full text for every
   `exclude_companies` entry incl. micro1.com apply links, because the
   company-field check is blind for Gmail-alert stubs where company is
   empty — exactly how the micro1 fronts QuikHireStaffing/HireFeed reached
   generation on 2026-07-06) writes a SKIP tracker row (`tracker.add_skipped`)
   and aborts generation for $0.00 — `DOOMED_GATE_HARD_ACTION=skip` (default).
   **SOFT** (primary stack isn't the candidate's — a Vue/Svelte/Ember-first
   web role with neither Angular nor React in the requirements; a game engine
   mentioned only under a nice-to-have heading) warns in Telegram and
   generation continues. The game-engine rule was SOFT until 2026-08-12, when
   the owner raised it to HARD after a "Senior Software Engineer - Unity -
   Frontend" role was generated for in full — an engine-first posting with no
   Angular/React anywhere leaves nothing to apply with. The low-frequency-hybrid exception
   (`allow_low_frequency_hybrid`, renamed 2026-08-08 from the narrower
   `allow_weekly_hybrid_warsaw_krakow`): a hybrid role in ANY Polish city is
   acceptable when the text says office visits are ~once a week or less
   (once/twice a month, quarterly, occasional visits — EN+PL phrasing in
   `filters._LOW_FREQ_HYBRID_RES`); the body's frequency phrasing wins over a
   bare "hybrid" header (owner decision 2026-08-08). Force-mode (`skip_dedup`) and manual paste always
   degrade HARD to warn (the owner explicitly said generate this one); a
   HARD-but-degraded or SOFT finding surfaces in one Telegram message with
   the rule + a short evidence quote. `DOOMED_GATE_ENABLED`/
   `DOOMED_GATE_HARD_ACTION` gate/downgrade the whole thing without touching
   listing-level filters. Calibrated against ~450 real postings + a live
   Google Sheet sample — see `docs/DOOMED_GATE_CALIBRATION.md`.
   **Title trust (2026-08-12):** the title-based rules no longer take the
   passed `title` as the last word. `_titles_to_check` also guesses the
   posting's own header line (`_guess_title_from_text`) and, when the two
   disagree (`_titles_agree` — plain containment either way, since a header
   normally CONTAINS the real title), runs the rules against BOTH. An
   explicit title is only as good as its source, and Gmail alert digests
   are wrong by construction — they used to label every job of an email
   with the subject, which is how "Full stack / Front-end developer -
   Scala / Java" reached generation as "Programista Frontend (Angular)".
   Agreement (the common case) changes nothing; `_dedup_findings` keeps one
   line per rule so the owner never sees the same warning twice.
3c. **Re-post gate** (`hunter.repost_gate.run_repost_gate`, Step 1.5g in BOTH
   pipelines, right after the doomed gate, before the first LLM call): a
   vacancy already applied to routinely returns with a NEW URL (re-listed
   after expiry, cross-board duplicate, agency re-post under a name variation
   like "ITDS"/"ITDS Polska") — URL dedup can't see it, and calibration over
   the real corpus (tools/reuse_calibrate.py, 675 applies, 2026-07-20) showed
   ~14% of all generations were exactly this. PRIMARY key is TF-IDF text
   similarity of the fetched posting vs the job_posting.txt of recent applied
   rows (tracker.get_recent_applied_for_repost — folder-bearing rows, last
   `REPOST_WINDOW_DAYS`=60 days); fuzzy company-name agreement (legal/generic
   tokens stripped, containment + difflib — `normalize_company`/
   `companies_match`) only LOWERS the required similarity, never keys the
   match. Matrix (calibrated: same-company re-posts cluster at sim>=0.95;
   agency boilerplate FPs — Hays/UST/emagine posting DIFFERENT roles in
   near-identical words — live at 0.85-0.90): sim>=0.94 → re-post regardless
   of company; sim>=0.90 + name agreement → re-post; sim>=0.85 otherwise →
   warn-only Telegram line, generation continues. Both texts must be >=1500
   chars (April's theprotocol anti-bot stub pages were byte-identical across
   companies — sim 1.0 garbage). On a match: REUSE the existing CV (owner
   decision 2026-07-20) — copy the donor folder's rendered docs+content.json
   into a fresh dated folder named `{Company}_reused_{donor-date}` (the
   provenance is visible at a glance locally, on Drive and in the tracker
   Folder column; chained re-posts re-derive the base name, never stacking
   tags), write a normal applied tracker row for the NEW
   url with the Re-application "+" flag (`add_applied(reapplication=True)`)
   and cost $0.00, stamp the donor's ats_verdict, notify Telegram with the
   files — zero LLM calls; the pipeline returns None so the dual-apply
   shadow is skipped (A/B on a copied CV is meaningless), while Sheets/Drive
   delivery fires through the parent's normal exit-0 hooks finding the new
   row. `/force` bypasses the gate entirely; every failure path (including a
   mid-reuse error) degrades to normal generation. `REPOST_GATE_ENABLED`
   (default true) / `REPOST_WINDOW_DAYS` in config; thresholds are module
   constants in repost_gate.py.
4. LLM call: `candidate_profile.md` + `generation_rules.md` + job text -> `content.json`
   **Company+title dedup gate** (`apply_api.py` Step 4.55 / `apply_cli.py`'s
   post-generation equivalent, added 2026-08-20): the manual entry points (URL
   paste, LinkedIn batch, forwarded text) call this pipeline directly with only a
   URL — they never run the hunt loop's own `dedup_key` check (`hunter/main.py`
   Step 3) before spending on generation, since company/title aren't known until
   the LLM extracts them here. A vacancy re-listed under a new URL (re-post,
   cross-board duplicate — e.g. the same requisition via LinkedIn vs pracuj.pl)
   would otherwise sail through every earlier gate and produce a full duplicate
   application. Runs right after the React-only skip and before the (expensive)
   ATS loop/judge/verdict-refine machinery below: on a `dedup_key` match against
   `get_known_company_titles()`, writes a SKIP row and aborts. `/force` bypasses,
   same as every other dedup gate. In the CLI pipeline the check runs after
   `content.json` is written (docs already rendered by the CLI skill at that
   point) — it still saves the tracker write + Sheets/Drive delivery, just not
   the CLI compute itself. See the 2026-08-20 Comarch incident in the Agent Work
   Log — `_strip_legal_suffixes` (below) was the other half of that fix.
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
   `ATS_VERDICT_TARGET` and `ATS_VERDICT_MAX_REFINES > 0` (default **5** —
   owner decision 2026-08-10: three honest passes, then two stretch;
   supersedes the 3-round default of 2026-07-07), rewrite
   `resume_en` against the verdict's own `missing_keywords`/`recommendations`
   (deterministically dropping unfixable ones — location/relocation/hybrid/
   on-site/cover-note/LinkedIn/years-of-experience — via `build_refine_feedback`),
   re-render, and re-verdict, for up to `ATS_VERDICT_MAX_REFINES` escalating
   rounds: **rounds 1–3 (honest)** — only candidate_profile.md-supported facts,
   nothing new; **round 4+ (stretch)** — may ADD posting technologies absent
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
- **Manual** (`--manual`): the owner asked for THIS vacancy by hand. It degrades
  the STACK gates to warnings — Step 1.5c (React), Step 1.5d (backend-only) and
  Step 4.5 in `apply_api`, their CLI twin in `apply_cli`, all via
  `apply_shared.stack_gate_allows_manual` — and nothing else. It is an explicit
  `is_manual=True` argument to `apply_service.run_apply_agent_for_url` (passed by
  `bot.apply_runner._run_apply_agent`, the Telegram paste/Apply-button runner),
  NOT a property of that function: "reached the manual runner" and "the owner saw
  this vacancy" are different claims, and a bulk expansion — a pasted LinkedIn
  alert fanning out into dozens of job ids nobody read a title for — must not
  inherit it by sharing a code path. The auto-hunt/queue path goes through
  `run_apply_agent_subprocess` and never gets the flag. Owner decision 2026-08-24 (docs/STACK_PRESCREEN_PLAN.md M2): the
  auto-hunt keeps filtering React-only postings, measured at 2 of 38 such
  packages ever sent against a 43% baseline, but a link the owner sends himself
  is generated without argument. Deliberately NOT `--force`: dedup, the doomed
  gate's HARD rules (location / work authorization / language) and everything
  else still apply to a pasted URL — that paste exception was REMOVED on purpose
  after calibration (docs/DOOMED_GATE_PASTE_PLAN.md), and only stack rules are
  being relaxed here.

**Two things have to hold for the PL CV to actually ship, and both silently broke
(fixed 2026-08-22 — 15 of 250 PL applications had shipped an EN CV next to a PL
cover letter, all from July on, as prod moved onto the CLI path):**
1. `content["primary_lang"]` must be set — it gates `generate_docs`'s `_primary_pl`
   routing AND `verdict_refine`'s PL mirror. `apply_api` always set it; `apply_cli`
   stamped it **only as a side effect of a repair**, so a clean CLI run left it
   absent and both consumers silently no-op'd. It is now stamped and persisted
   unconditionally, with a doc re-render when the PL CV is missing from disk.
2. `resume_pl` must be non-empty. `.claude/commands/apply.md` told the CLI skill to
   return `"resume_pl": null` unless `--full` — unconditionally, Polish postings
   included (the token saving is only legitimate for an EN posting, cf.
   `GEN_SKIP_PL_FOR_EN` / `build_pl_skip_instruction`). The prompt now excepts
   Polish postings, and `apply_shared.ensure_pl_resume(content, posting_lang)` is
   the net under it in BOTH pipelines: on a PL posting with no PL resume it mirrors
   one from the already judged + language-gated `resume_en` via `_translate_resume`
   (cheap translate model, role-count guarded, best-effort). Prompt compliance is
   not something a pipeline can assume — the net is what makes it true.

**Aborting AFTER generation (CLI pipeline only) must undo the tracker row, not
just the files** (`apply_shared.abort_after_generation`, added 2026-08-24 --
docs/STACK_PRESCREEN_PLAN.md M1). The CLI skill runs `generate_docs.py` WITHOUT
`--no-tracker` (`.claude/commands/apply.md`), so by the time `apply_cli`'s own
post-processing runs, the documents are rendered AND the applied row exists. Its
four abort stages -- React-only stack, company+title dedup, judge block,
language-gate block -- therefore cannot abort by writing a terminal row:
`add_react_skipped`/`add_skipped` no-op on `_is_known_terminal`, and exit 0 makes
`apply_worker._resolve_outcome` see `has_successful_entry` and DELIVER the package
the stage just rejected. Measured on the live corpus: 6 of 14 `main_cli` runs in
the retained window shipped a row carrying the CLI skill's SELF-reported ATS score
(96%, 96%, 78%...) with no independent verdict and no refine round at all -- the
2026-08-24 Interia incident plus 5 more. All four sites now call
`abort_after_generation(folder, url, reason=..., telegram_text=..., company=...,
title=...)`: drop `*.pdf`/`*.docx`, convert the row in place via
`tracker.convert_own_applied_row` (keeps id + `sheets_row`, sets
`sheets_dirty=1`), notify -- and, when there was no applied row to convert,
write the terminal SKIP row itself so no call site has to remember to.
`job_posting.txt` and `content.json` stay on purpose (diagnostics; a SKIP row can
never become a re-post donor). The API pipeline needs none of this -- its
equivalent gates run BEFORE `generate_docs`. Wrapped in
`best_effort("apply.abort_undo")`: the swallow is correct (an abort must never
become a FAIL) but this path IS the incident fix, and two of the four call sites
cannot see its return value.

Things two adversarial reviews (2026-08-24) showed the first cuts got wrong,
all now covered by tests:
- **Identity comes from the content.json the row was written from**, passed as
  `content=`: `apply_url` and `output_folder` are the literal values
  `add_applied` stored. The pipeline's own `url` is only a fallback — paste mode
  never hands the skill a URL (`url_norm=''`, so the whole paste flow including
  every `linkedin_scout_relay` post was a guaranteed no-op) and
  `.claude/commands/apply.md` lets the skill record the apply-button URL
  instead of the input one. Matching is **exact equality, single row**: an
  intermediate cut also matched a `<date>/<Company>` folder SUFFIX, and a review
  reproduced it converting a SECOND, genuine, already-delivered application to
  SKIP (two runs for one company on one day under different roots share that
  suffix; and `_`, which `_sanitize_folder_company` substitutes for every
  illegal character, is a SQL LIKE wildcard). When more than one row matches,
  `convert_own_applied_row` converts NOTHING and logs — a destructive write does
  not get to guess.
- **The delivery gate belongs in `delivery.py`, not in one parent.** Of the four
  callers of `deliver_apply_now`, only `apply_worker._resolve_outcome` checked
  the tracker; `bot.apply_runner` (manual paste / Apply button), the LinkedIn
  batch and `main._auto_apply_all` all deliver on a plain exit 0. `_is_deliverable`
  now refuses any URL that is KNOWN and not a successful entry, so an aborted run
  reaches neither Sheets nor Drive nor the backfills. An UNKNOWN url still
  delivers (see the identity problem above), and a failed tracker read fails OPEN.
- **`SKIP`/`FAIL`/`EXPIRED` rows never used to carry a folder.** Those producers
  write `folder=''`, so `gdrive_sync.upload_missing_folders` selected purely on
  "has a folder + no Drive URL". The converted row is the first exception, and
  without a status check the next backfill pass (every 30 min) would upload a
  folder holding only `job_posting.txt` + `content.json` and stamp a Drive URL on
  it. **`MANUAL` is deliberately NOT in that set** (and gets its own carve-out in
  `delivery._is_deliverable`): `add_manual_jobleads_pending` has always written a
  folder, the JobLeads flow returns outcome `"manual"` and delivers on purpose,
  and the bot's own message tells the owner to open that Drive folder and paste
  the job text into it. Excluding it stranded the folder on the VPS filesystem.
- **Settling nothing raises**, so `best_effort("apply.abort_undo")` has something
  to count. The real failure mode has no exception of its own: the conversion
  finds no row AND the fallback `add_skipped` no-ops because `_is_known_terminal`
  matched the very applied row the conversion failed to convert. Silence there
  restores the original incident invisibly — two of the four call sites cannot
  see the return value.

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

**Observability (fixed 2026-08-22).** `launch_detached` used to send the shadow's
stdout+stderr to `DEVNULL`. The shadow is a detached process, so that output
reaches no other log — every shadow failure was invisible, and the only evidence
was a missing or malformed `content.json` spotted by hand on Drive days later
(live corpus: 6 malformed shadow folders; 0 of 12 August shadows carried a
verdict, with no trace of why). It now writes a per-run transcript to
`logs/dual_shadow/<date>_<company>.log` (14-day prune, mirrors
`hunter/apply_stdout_log.py`'s layout), falling back to `DEVNULL` if the file
can't be opened. **Structural floor:** a shadow model that returns a different
SHAPE (resume fields at the top level, or only the skills object — 6 real cases,
all `deepseek-v4-pro`) used to sail past the validate+repair pass and leave a
folder with nothing but a broken content.json — no docs, no verdict, nothing to
compare, and one more folder for the Drive backfill to carry forever. The shadow
now aborts cleanly when `resume_en` is still not a populated dict after the
repair pass.

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
| 7 | Folder | Path to Applications/ subfolder. tracker.db stores the FULL path (`/app/users/{uid}/Applications/2026-08-14/CoreView`) — that's what the Drive uploader, the re-post gate and local file lookups resolve. The Sheets mirror shows only `2026-08-14/CoreView`: `gsheets_client.short_folder()` trims everything up to and including the `Applications` component, applied in `_row_to_list` so every write path (append/update/batch) is covered. Rows mirrored before this keep their long value — the owner explicitly does not care about the historical ones (2026-08-15). |
| 8 | Sent | Date sent, or blank/dash. Blank = awaiting send (the owner's Sheets filter view keys on this). Every SKIP/FAIL row is stamped `—` at insert (add_skipped/add_failed, since 2026-08-08; init_db backfills older rows) so non-applications never pollute the send queue; EXPIRED rows carry `EXPIRED` here. On a SKIP row the dash doubles as the react-skip / "deliberately skipped" marker (`get_url_status_flags`): plain re-paste refused, `/force` regenerates. FAIL retries are unaffected (they key on ats_status only). |
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
- Bot wrote a terminal marker (EXPIRED, or the `—` dash every SKIP/FAIL row
  gets at insert since 2026-08-08), Sheets is empty → keep the marker
  (Sheets will be fixed by resync — a blank cell there means the marker
  wasn't pushed yet, not that the user cleared it)
- Sheets has date / was edited → trust Sheets
- To Learn, Re-application → always trust Sheets (user edits there)

---

## Adding a New Job Source

See `.claude/commands/add-source.md` for the full guide. One class owns both the
listing scrape and the detail-page extraction — the separate `job_fetch/` package
was merged into the sources in the Phase 3 refactor (2026-05-26) and no longer
exists. The five registration points:

1. `hunter/sources/yoursite.py` — subclass `BaseSource`, implement
   `search() -> list[Job]`, and override `matches_url(url)` + `fetch_text(url)`
   for detail-page extraction (the base class defaults to claiming nothing and
   falling back to generic HTML)
2. `YOURSITE_ENABLED` toggle in `hunter/config.py`
3. `hunter/sources/__init__.py` — the config import block **and** `ALL_SOURCES`
   (hunt-cycle registration, gated by the toggle)
4. `hunter/sources/__init__.py` — `_fetch_roster()`, the `fetch_job_text` dispatch
   list. Deliberately NOT gated by the toggle: a source excluded from the hunt
   still owns its domain's URLs for apply / expired-check / repost-gate / Gmail
   enricher. Skipping this is the classic half-wired source — listings work, then
   every apply for them quietly degrades to the generic HTML fallback
5. CLAUDE.md — a row in **both** the "Job Sources" table and "Scraper Health
   Notes" (plus the source-toggle list above and `.env.example`)

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

## LinkedIn Posts Scout (external private repo)

The standalone LinkedIn posts scout — a Playwright scraper of LinkedIn
content-search + home feed for "we're hiring" posts — lives in its own
**private** repo: `igrdevelop/linkedin-scout` (desktop checkout:
`D:\LearningProject\linkedin-scout`, owner's Windows desktop, residential IP,
Windows Task Scheduler; four tasks: Search hourly-ish + three Feed slots). It
was moved out per docs/SCOUT_REPO_SPLIT_PLAN.md (Phases 0-3; this repo went
through Phase 3 cleanup on 2026-08-11 — the in-repo `linkedin_scout/` copy,
its 7 test files, `tests/fixtures/linkedin_scout/`, `tools/
telegram_user_login.py` and the `scout` extra (`telethon`) are deleted). A
scraper with stealth flags tied to the owner's own LinkedIn account must not
be public; the scout repo stays private permanently.

**What stays in THIS repo** (zero scraping, runs in Docker):
- `hunter/commands/scoutfound.py` — receives `/scoutfound <base64(json)>`
  sent through the owner's own Telegram USER session (Telethon/MTProto on the
  desktop side; a bot never receives its own outgoing messages, and the two
  machines share no filesystem).
- `hunter/sources/linkedin_scout_relay.py` — drains the queue file
  `scoutfound` writes into normal `Job` objects on the hunt cycle. Behaves like
  any other source — central filters, doomed gate, tracker dedup, normal
  AUTO_APPLY — and handles **two record kinds**:
  - **`post`** (the feed/content-search tracks, v1 shape, `kind` absent ⇒ this):
    a feed post has no fetchable URL, so `job.url` is the synthetic dedup key
    `https://linkedin-scout.internal/posts/p…` (NOT `linkedin.com/scout-posts/…`
    — that older value collided with `LinkedInSource.matches_url`, which claims
    any linkedin.com host regardless of path; see the module docstring and
    `hunter/validation.py`'s legacy marker). Post text rides in
    `job.raw["post_text"]` → paste-flow apply; the real post permalink, when the
    scout captured one, in `job.raw["permalink"]`.
  - **`job`** (the jobs track, v2): a real LinkedIn Jobs posting. `job.url` is
    the canonical `linkedin.com/jobs/view/<id>` and stays that way on purpose —
    dedup against `LinkedInSource`'s own finds, the expired check and FAIL-row
    retries all key on it, and apply FETCHES the description via
    `fetch_job_text(url, use_session=True)`. Here `LinkedInSource` claiming the
    url in `_fetch_roster()` is the desired routing, not a collision. Carries no
    `post_text` (its presence would reroute apply through the paste flow and skip
    the fetch) and no `permalink` (it would equal `url`, which
    `Job.telegram_text()` already renders). `workplace_type` is folded into
    `job.location` by `_job_location()`, because the central location whitelist
    reads that one string and would otherwise drop a genuinely remote posting
    tagged with a non-whitelisted city.
- **Payload contract v1 + v2** (`"v": 1` / `"v": 2`): tolerant, version-checked
  decode in `scoutfound.py` (`MAX_SUPPORTED_PAYLOAD_VERSION`). v2 adds `kind`;
  required fields are per-kind (`post` → `body`, `job` → `url`,
  `_REQUIRED_FIELD_BY_KIND`) and the **version gate runs before that field
  check** — a payload from a newer scout may legitimately lack the fields this
  version looks for, and the owner needs the "update the bot" reply instead of
  silence. Golden fixtures `tests/fixtures/scout_payload_v1.json` and
  `scout_payload_v2_job.json` are shared byte-identically by both repos'
  contract tests, so schema drift fails loudly. Bump the version on any payload
  change and update both sides + the fixture.
- **Deploy ordering is not optional: this repo ships BEFORE the scout starts
  sending a new payload version.** `telegram_relay.send_candidates` (scout side)
  marks a candidate seen after a successful *send*, and a send to a bot that then
  rejects the version still counts as successful — so a scout running ahead of
  the bot burns every vacancy it finds into its own `seen_posts.json` with
  nothing queued here.

Scout-side runtime notes (sessions, circuit breaker, `--reset`, off-screen
search window, Task Scheduler setup) live in the scout repo's own README.
Ops incident 2026-08-10: the scout was silently dead for a month — both
tracks' circuit breakers tripped on anti-bot interstitials (2026-07-09/12,
the one-shot Telegram alert was missed) and its `.env` pointed at a deleted
secrets folder; fixed by repointing `LINKEDIN_STORAGE_STATE` /
`TELEGRAM_USER_SESSION` to `D:\Projects\job-hunterot\.secrets\` and a
manual re-login + `--reset`. If relay yield is 0 for days, check the
breaker state files and session paths on the desktop first.



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
- **Commit messages, PR titles and PR descriptions are English-only** (the repo
  is public). Quoted data may stay in its original language — a Russian regex
  pattern being added, an owner report being cited, a bot UI string — but the
  message's own prose must be English.

---

## Important Rules for Agents

- **Never commit** `.env`, `tracker.xlsx`, `Applications/`, `backups/`, `gmail_token.json`, `gsheets_token.json`, `gsheets_credentials.json`, `candidate/notes/`
- **Personal candidate facts (name, city, employers, languages) go through `hunter/candidate.py` only.** Don't hardcode a name/city/employer/school/contact string in production code — read it via `candidate.get(dotpath, default)`. **The `default` must be neutral (empty, or an obvious placeholder), never the project owner's real value.** This reverses the original CANDIDATE_YAML_PLAN rule ("default reproduces today's behavior"): that rule is exactly why three separate repo-readiness audits each found fresh owner data in the code — every new feature legitimately added one more line of it. A consumer whose key is absent must self-skip ("unmeasured") or gate, not fall back to a stranger's data. Identity fields are gated by `candidate.require_identity()` at the top of `generate_docs.main()`. `tests/test_handoff_readiness.py` enforces all of this in CI. `candidate/candidate_profile.md` and the base-CV files in `candidate/` remain the source of truth for free-text career narrative — this rule is about short, structured facts that filters/QA/prompts compare against, not prose.
- **A new `candidate.get()` dotpath must also be added to `candidate/candidate.yaml.example`** — `tests/test_handoff_readiness.py` fails otherwise. An undocumented key is a setting a new user cannot discover.
- Always test syntax after edits: `python -m compileall .`
- **Hooks:** a script in `.claude/hooks/` runs ONLY if `.claude/settings.json`
  maps it to an event — dropping a file in that folder does nothing by itself.
  A hook that must BLOCK exits with code **2** (stderr is fed back to the model);
  any other non-zero code is a non-blocking error that just prints and lets the
  call through. The git `pre-commit` (`.githooks/`) likewise needs a one-time
  `git config core.hooksPath .githooks` per clone — git never runs hooks out of
  a tracked directory on its own
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
- Candidate profile single source of truth: `candidate/candidate_profile.md`
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
| JustJoin.it | 2026-08-12 | OK | Listing `/api/candidate-api/offers` changed twice. (a) 2026-08-10: `perPage` ignored (server-fixed 10/page) and old `cursor` param ignored — every page returned the same 10 promoted offers, yield fell to 1-2/run for ~3 weeks. Fixed: offset sent as `from` (`meta.next.cursor` still holds the next offset), loop budget counted in offers (PER_PAGE×MAX_PAGES=300/workplace type). (b) 2026-08-12: the flat `skills` key is now always `null` — the stack lives in `requiredSkills` / `niceToHaveSkills`. Both `filters._is_react_without_angular` and the detail fetcher read only the old key, so every JustJoin job was judged on its TITLE alone and `job_posting.txt` lost its stack section; both now read all three keys |
| NoFluffJobs | 2026-07-10 | OK | Sort audit 2026-07-10: default API order is already newest-first (`renewed` desc); NOTE the `page` field in the body is ignored by the server — every request returns the same first ~59 of `totalCount` postings, which is fine since those are the freshest. Listing POST `/api/search/posting`; detail `/api/posting/{slug}` schema changed (no more `sections` — content moved to `details.description` / `requirements.description` / `specs.dailyTasks`, salary to `essentials.originalSalary`, company name to `company.name`). `_format_posting_text` now multi-path with legacy fallback |
| LinkedIn | 2026-08-22 | OK | **Closed-posting detection without a session** (2026-08-22): a guest page NEVER contains "No longer accepting applications" — that banner is logged-in only, so the `HTML_EXPIRED_MARKERS["linkedin.com"]` substrings can only ever fire on the Playwright path. Measured over 14 live postings + one closed one (`jobs/view/4455428397`): the sole guest-visible difference is the apply CTA — live pages put an Apply button inside `top-card-layout__cta-container` (14/14), a closed page renders that container empty. `linkedin.guest_html_expired()` encodes exactly that, conservatively (container must be PRESENT, and any `<a>`/`<button>` inside means alive), and is wired into both `LinkedInSource.fetch_text` (returns the synthetic `"This job posting has expired."` marker) and `expired_marker._check_html_expired` (which previously reported EVERY LinkedIn row as `linkedin-login-wall` skipped). Guest HTML search API. **Page size is 10, not 25** (measured live, control-verified: 3 identical requests → identical id sets). The old `RESULTS_PER_PAGE = 25` + "break when a page < 25" ended pagination on the FIRST call, so the source reported 10 postings per keyword where ~69 were available (410 unique measured across all probed variants; 403 of them invisible to the bot). Now walks `start` on a 10-grid, stops on an EMPTY page only. Also measured: `f_E` and `f_WT` are **ignored** by this endpoint (byte-identical id sets with/without) — both removed/never added; `f_TPR` IS honoured and was widened 24h→7d (the 7d set is 49% angular-in-title vs 11% for 24h) |
| Bulldogjob | 2026-07-10 | OK | `__NEXT_DATA__` JSON. Listing URLs now carry `/order,published,desc` (live-verified: default order pins promoted offers above fresh ones and dropped a job out of the top of the list; the segment makes the main block strictly newest-first). Side observation: `/remote,true` currently does NOT filter (identical list incl. `remote=False` jobs) — kept for when the site fixes it |
| Pracuj.pl | 2026-07-10 | OK | cloudscraper + `__NEXT_DATA__`. Sort audit: default listing order IS strictly `lastPublicated` desc (newest-first) — no sort param needed; none found in the page either |
| theprotocol.it | 2026-07-10 | OK | cloudscraper + dehydratedState. Sort audit: default is `sortType: "relevance"`; `?sort=<x>` is echoed into the state but does NOT change the SSR result order (verified: identical list for relevance vs date on the remote listing) — no working URL sort param found, left as-is. Observed order was near-date-desc anyway on the narrow frontend queries |
| SolidJobs | 2026-04 | OK | RSS feed |
| Arbeitnow | 2026-04 | OK | JSON API |
| Remotive | 2026-04 | OK | JSON API |
| Working Nomads | 2026-06 | OK | Public Elasticsearch `/jobsapi/_search` (5400+ jobs) |
| Jobspresso | 2026-08-10 | OK | Unfiltered feed spans ALL categories — live top-10 held zero frontend roles for 19 straight days (source effectively dead, 0 yield). Fixed: WP Job Manager honors `search_keywords=` on the feed URL — now queries plain feed + one feed per keyword (frontend/angular/react/javascript) and merges; live re-check 0 → 21 jobs |
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
| Gmail | 2026-08-12 | OK | Gmail API alerts. LinkedIn digest cards verified live against 8 alert emails — 31/31 jobs yielded their own title + company (`_linkedin_cards`). Before this the subject was used for every URL of an email, which is how a Java/Scala contract role reached generation |
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
> scout relay, dual_apply, verdict_refine, doomed gate, gsheets sync) —
> it documents rejected alternatives and live-verification findings that
> aren't visible in git log alone.

| Date | Agent | Work |
|------|-------|------|
| 2026-08-24 | opus | **React-only vacancy produced a full application package and was delivered -- the CLI pipeline's gates fire after the documents and the tracker row already exist** (owner: 'why do we make documents for such vacancies?' about `Interia / Regular Frontend Developer/ka / React / 96%`). **Diagnosis (live prod, read-only).** The CLI skill runs `generate_docs.py` without `--no-tracker`, so the applied row is written INSIDE the CLI call; `apply_cli`'s React check sits after it, `add_react_skipped` no-ops on `_is_known_terminal`, and exit 0 makes `apply_worker._resolve_outcome` see `has_successful_entry` and deliver (Sheets row 1339 + Drive folder, 831s of subscription time). The API pipeline is unaffected -- its Step 4.5 runs BEFORE `generate_docs`. Measurement found the same leak on the company+title dedup gate and, by code reading, on the judge/lang-gate blocks: **6 of 14 `main_cli` runs** in the retained window shipped a row carrying the skill's SELF-reported ATS score (96/96/78%) with no independent verdict and no refine round -- exactly the number the project stopped trusting when the verdict was introduced. **M0 (measured, 1333 tracker rows, `sent_parse` to decode the free-text Sent column):** React-only packages were actually sent **2 of 38** against a **43%** baseline, zero confirmations, zero answers -- and the owner's own Sent notes say 'not my stack, should have been filtered out' five times in August. The same measurement KILLED the adjacent seniority-filter idea: mid/regular titles were sent at **46%**, inside the baseline, so filtering them would have cost 6 live applications for 13 rows in 4 months. **M1 fix:** new `tracker.convert_own_applied_row` (turns this user's applied row back into a terminal SKIP in place -- keeps id + `sheets_row`, sets `sheets_dirty=1`, so an already-mirrored Sheet row is corrected instead of duplicated) + new `apply_shared.abort_after_generation` (drop `*.pdf`/`*.docx`, convert the row, notify; returns False when there was no row, and the stack/dedup sites fall back to their old terminal write). All four `apply_cli` abort sites rewired. No parent-side change needed: `_resolve_outcome` already prefers `has_successful_entry`, so after the conversion it takes the soft-terminal branch and does not deliver. 14 new tests, mutation-verified; full suite 2687 green, ruff clean. Plan + remaining milestones (Haiku stack pre-screen behind an offline calibration, `--manual` flag, CLI-prompt fix, golden E2E for `main_cli`, narrow pipeline unification): docs/STACK_PRESCREEN_PLAN.md. **Also verified for the owner:** the verdict and refine loops DO run on the Claude CLI subscription -- 46 of 60 retained runs stay in API mode with only the transport swapped, and full `main_cli` runs refine too (70 -> 78 -> 93...); only `cost_usd` is structurally blank there. **Reported, not fixed:** the CLI skill sometimes asks a clarifying question instead of deciding, and `claude -p` being non-interactive that burns the full 600s timeout and arms the hour-long auto-apply pause (5 of 60 runs). |
| 2026-08-22 | opus | **Closed LinkedIn postings invisible — session path dead in prod, guest path blind by design** (owner: `jobs/view/4455428397` clearly shows "No longer accepting applications"; bot generated a full CV for it — tracker row `Ebiquity / Frontend Developer / 88%`). **Two independent causes.** (1) Prod `.env` has `LINKEDIN_STORAGE_STATE` pointing at a **Windows desktop path** inside the Linux container — `_storage_state_path()` returns None for a missing file and `fetch_text_with_session` falls back to the guest fetch silently, so every LinkedIn apply runs unauthenticated. (2) The guest path could never have caught it: measured live, a logged-out page NEVER carries the "No longer accepting applications" banner (0 hits over 14 live + 1 closed posting), making `HTML_EXPIRED_MARKERS["linkedin.com"]` dead code outside Playwright — and `/check_expired` reported every LinkedIn row as `linkedin-login-wall` skipped, so it has never expired one. **The one guest-visible signal:** live pages put an Apply control inside `top-card-layout__cta-container` (14/14); a closed page renders that container EMPTY. New `linkedin.guest_html_expired()` encodes it conservatively (container must be present; any `<a>`/`<button>` inside = alive, so a markup rename goes silent instead of expiring every LinkedIn posting), wired into `LinkedInSource.fetch_text` (returns the synthetic `"This job posting has expired."` marker → apply Step 1.5a skips for $0) and `expired_marker._check_html_expired`. `fetch_text` now does its own `requests.get` — the signal lives in raw HTML that `get_text()` discards — with `html_fallback.extract_text()` split out so the text path is unchanged. Live re-verified end-to-end: dead URL → `is_job_expired=True`, two live controls → False. 9 new tests, mutation-verified; full suite 2643 green, ruff clean. **Owner action left open:** repoint `LINKEDIN_STORAGE_STATE` to `/app/.secrets/linkedin_storage_state.json` + refresh the session (`tools/linkedin_login.py`) — the session path also catches closures where the CTA does not disappear. See docs/AGENT_LOG.md. |
| 2026-08-22 | opus | **Corpus audit of the live deploy host: Polish employers were getting an English CV, and the dual-apply shadow was unobservable** (owner: 'делай всё что можешь, с сервера и гугл драйва тоже возьми файлы на проверку'). Read-only audit of 761 applications / 323 judge reports on the VPS. **(1) PL CV.** 15 of 250 PL applications shipped an EN CV next to a PL cover letter — 0 in May/June, 3 in July, 12 of 28 in August, tracking prod's move onto the CLI path. Two compounding causes: `.claude/commands/apply.md` told the CLI skill to return `resume_pl: null` unless `--full` UNCONDITIONALLY (the token saving is only legitimate for an EN posting), and `apply_cli` stamped `primary_lang` only as a side effect of a repair — that key gates BOTH generate_docs' PL routing and verdict_refine's PL mirror, so the fallback was disabled exactly when needed. Fixed all three layers: prompt excepts Polish postings, `primary_lang` stamped + persisted unconditionally (re-render when the PL CV is missing from disk), and new `apply_shared.ensure_pl_resume()` mirrors resume_pl from the already judged + language-gated resume_en in BOTH pipelines. **(2) Dual-apply.** `launch_detached` sent the shadow's stdout+stderr to DEVNULL; being detached, that output reached no other log, so 6 malformed shadow folders and 0-of-12 August shadows without a verdict had no diagnostic trace anywhere. Now writes `logs/dual_shadow/<date>_<company>.log` (14-day prune). A shadow whose model returns a different SCHEMA now aborts cleanly instead of leaving a folder holding only a broken content.json. **(3)** New RED LINE in generation_rules.md against promoting the profile's own verb ('participated in' -> 'drove') — the single most repeated judge finding (`Drove frontend architecture decisions` 13x), and `exaggeration` is warn-only so every occurrence ships. **Verification:** replayed old vs new clause-drop over 1495 real fields from the server corpus — visible defects introduced by a cut fell 17.7% -> 0.0%. 11 new tests, mutation-verified x5; full suite 2663 green, ruff clean. See docs/AGENT_LOG.md for the full entry. |
| 2026-08-21 | opus | **Clause-drop artifacts reached a delivered CV — punctuation seams and a flattened cover letter** (found while analysing the Claude vs DeepSeek applications on Drive). Four defects in the shipped Avive Solutions package all reproduce byte-for-byte from `claim_judge._drop_quote`: `', - from a real-time...'`, `'...decisions; across a team of 10+.'`, a bullet opening lowercase, and the whole cover letter rendered as ONE paragraph — that last one because the cleanup line `re.sub(r'\s{2,}', ' ', candidate)` treats a newline as whitespace, so ONE dropped clause flattened every break in the letter. Root cause common to all four: the seam left by the cut was patched with global regexes over the whole field, covering a few hard-coded pairs and missing the rest. **Fix:** new `hunter/text_repair.py` repairs the junction where the cut actually happened (the caller knows both sides), with newline-safe space collapsing and a paragraph-preserving sentence drop; rewired `claim_judge._drop_quote` plus `apply_shared._scrub_prestige_text` / `_scrub_compliance_clause`, which had the same bug class. Self-inflicted bug caught mid-fix: interpolating a dash into a character class made `.-–` a RANGE over every ASCII letter and ate the survivor's first letter — pre-escaped `_SEPARATOR_CLASS` + regression test. 18 new tests, the four artifacts asserted through the production entry point; mutation-verified ×6. Full suite 2652 green, ruff clean. NOT fixed (reported separately): shadow runs write no `ats_verdict`, 2 of 5 recent DeepSeek shadows returned malformed JSON, `resume_pl` null on two recent PL postings, and the recurring un-repaired `'Drove frontend architecture decisions'` exaggeration. See docs/AGENT_LOG.md for the full entry. |
| 2026-08-20 | sonnet | **Comarch dedup gap — company+title dedup never ran for manual entry points; "Comarch SA" vs "Comarch" normalized differently** (owner: repeated Comarch "Senior Angular Developer" rows across 2026-06-20…2026-08-20). **Diagnosis:** 19 Comarch rows, all distinct URLs (URL dedup fine) — the real safety net, `dedup_key(company, title)` (checked in the hunt loop, `hunter/main.py` Step 3), never runs for manual URL paste (`cmd_url`) or the LinkedIn batch flow (`_run_linkedin_batch`), which only check the exact URL. Today's specific duplicate: pracuj.pl reported "Comarch SA", a 3-day-older LinkedIn row "Comarch" — `_strip_legal_suffixes` stripped the dotted "S.A." form but not the no-dot Polish "SA", so the two normalized to different dedup keys. **Fix:** (1) `_strip_legal_suffixes` (hunter/tracker.py) now also strips a bare word-boundary "sa" suffix. (2) New Step 4.55 in both `apply_api.py`/`apply_cli.py`: once the LLM returns `company_name`/`job_title`, check `dedup_key(...)` against `get_known_company_titles()` and abort with a SKIP row on a match — the one check that covers URL paste, LinkedIn batch and forwarded text uniformly, since none of them know company/title before the LLM call. `/force` bypasses. 5 new tests, mutation-verified ×2. Full suite 2634 green, ruff clean. See docs/AGENT_LOG.md for the full entry. |
