# MARKET_MEMORY Plan — keep what the hunt sees, not only what it applies to

**Status:** draft
**Date:** 2026-09-12
**Motivation:** owner question 2026-09-12 ("what can we do with the information
we have collected over months of searching and keep collecting, and maybe add
fields or aspects of search"). The audit behind this plan found that the bot
keeps almost nothing about a vacancy as a *market object*: structured listing
attributes (salary, location, source, first-seen date) are discarded for every
vacancy, including the ~10% it applies to, and the ~90% that the filters or
dedup reject leave no trace at all. The one Y-variable that makes any of it
analysable (`outcome_label`) shipped the same day in PR #276; this plan is the
X side.

Related plans: `docs/improvement-2026-09/08-DATA_EVAL_PLAN.md` (M3 sketches a
`postings_seen` table for the market-aggregate tier; this plan is the concrete,
owner-first version of it and supersedes that sketch), `07-COMPLIANCE_PLAN.md`
M6 (what an aggregate *product* may store — different from what the owner's own
bot may remember), `docs/ROADMAP.md` §4 (the "waiting for data" rows: 4.1 and
4.3 wait for data this plan would make richer).

---

## Problem

What survives each stage today (verified against the code, 2026-09-12):

| Stage | What the code has in hand | What is persisted |
|---|---|---|
| Hunt fetch (`hunter/main.py` Step 1, 25 sources) | full `Job`: title, company, location, salary, url, source, `raw` API payload | one row per **source** in `source_runs` (count + ok/error), ring-buffered to 50 rows/source |
| Filter + dedup (Step 2–3) | every rejected `Job` + its reason (`apply_filters_with_stats` reasons dict, `classify_job`) | nothing — reasons are summed into a Telegram line; only Gmail-sourced rejects get a per-job tag, and only for the per-email report |
| Queue (`tracker.add_pending`) | full `Job` JSON in `pending_meta` | temporarily — `_clear_own_placeholder` deletes the placeholder when the terminal row is written |
| Terminal row (`add_applied`/`add_skipped`/…) | LLM-extracted company/title/stack, url, folder, verdict, cost | exactly that. **No** salary, location, remote mode, source, first-seen date. Source is re-derived from the URL by `funnel.py` (breaks on ATS-hosted URLs) |
| SKIP rows | the reason is known at the call site (Skip button, doomed-gate rule, dedup, abort, react, prescreen) | `ats_status='SKIP'`, `sent='—'` — identical for every reason (08-DATA_EVAL "blind spots") |
| `Applications/<date>/<Company>/` (applied only) | posting text, content.json, verdict history, judge report, outreach | all of it — the only rich record, and by construction a biased ~10% sample |
| `generation_runs` (#267) | source, model, track, posting lang, verdicts | yes, since 2026-09-10 — a few days of rows |

Consequences, each a question the owner asked or a ROADMAP row that is blocked:

- "Is Senior Angular pay in Poland going up or down?" — unanswerable; salary is
  parsed by every one of the 24 sources into `Job.salary` and thrown away.
- "Is this vacancy a ghost job re-posted for the fourth month?" — unanswerable;
  no first-seen/last-seen. The repost gate only sees vacancies the owner
  already applied to.
- "Which sources feed the funnel?" (ROADMAP 4.3) — `funnel.py` guesses the
  source from the URL; every Greenhouse/Lever/Workable link from any board
  collapses into "ats".
- "Do I lose vacancies to the filters that I would have wanted?" — the
  2026-08-08 Sent-notes audit had to be done by hand over 600 rows because the
  filter verdict is not stored per job.
- "Which skills are in demand that I don't have?" — `to_learn` is a per-CV
  free-text column; the demand side (how often a term appears across ALL
  listings, not just the ones applied to) does not exist. `tools/market_m0.py`
  runs on the applied-only corpus and says so in its own bias caveat.

## Non-goals

- **No new LLM calls.** Every field below is either already in the `Job`
  object or derivable by regex/TF-IDF from it.
- **No posting text for non-applied vacancies.** Only listing metadata + a
  `text_hash`. The applied-only `job_posting.txt` corpus stays as it is.
- **No SaaS aggregate tier.** 07-COMPLIANCE M6 (allowlist of JSON/RSS/ATS
  sources, k ≥ 10 per cell, never company names) governs what a *product* may
  publish from this data; it does not govern what the owner's own bot
  remembers about vacancies it fetched for the owner. The table is designed so
  M6 can be applied as a read-side projection later (see Risks), not baked in
  now.
- **No change to `filters.py` logic or to any gate.** The hunt loop gains one
  best-effort write; what passes and what is rejected is unchanged.
- **No second outcome mechanism.** `outcome_label`/`/outcome` (#276) is the
  Y-variable; this plan only joins to it.
- **Nothing in this plan changes what gets queued, generated or sent.**
  Owner decision 2026-09-12: this plan is about collecting more information
  and reporting on it, nothing else. No new filter reason, no new gate, no
  warning line injected into the apply flow, no cooldown change. The hunt
  loop gains one best-effort *write*; every read is a report (`/market`, the
  digest, `tools/`). If the reports ever justify a search rule, that is a
  separate plan with its own M0, written when the data exists.
---

## M0 — Measure

Read-only, $0, no DB writes. Two halves: what the listings actually carry, and
how big the table would get.

**M0.a — listing attribute coverage (live probe, network only, no writes).**
`tools/market_memory_m0.py` (new, read-only): runs `source.search()` once for
every enabled source exactly as the hunt loop does, then reports per source
and in total:

- raw jobs, unique `url_norm`, how many are already known to `tracker.db`
  (`get_known_urls`) — i.e. how many *new* listings a single sweep sees;
- share with non-empty `salary`; share whose `salary` parses into a
  min/max/currency with a first-cut regex (PLN/EUR/USD, `k`, ranges, `/h`,
  B2B/UoP tokens);
- share whose `location` classifies into remote / hybrid / onsite + city with
  the existing vocabulary (`text_utils.REMOTE_ANY`, `filters._PL_ANTI_HYBRID_CITIES`,
  `_anti_hybrid_cities`), and the share that stays "unknown";
- filter verdict distribution (`apply_filters_with_stats` reasons + passed), so
  the plan can say what share of the market the owner's filters reject and for
  what.

`--json` for the numbers; `--offline` skips the network and reruns the parsers
over `Applications/**/content.json`'s `job_title`/`company_name` + the
`pending_meta` JSON of any PENDING rows present — a smaller, biased sample,
only for developing the parsers without hitting 25 sites.

**M0.b — volume from what already exists (prod DB, one query).**

```sql
-- raw listings per day, last 14 days, all sources (ring buffer permitting)
SELECT substr(ts,1,10) AS day, SUM(yield) AS raw_listings, COUNT(*) AS runs
FROM source_runs WHERE ts >= date('now','-14 days') GROUP BY day ORDER BY day;
```

Run on the deploy host: `docker compose exec job-hunter sqlite3 /app/db/tracker.db "<query>"`.
This gives the upper bound of rows/day before URL dedup; M0.a's "unique and
not already known" share turns it into the expected insert rate.

**Decision rules (stated before running):**

| Metric | Threshold | If below |
|---|---|---|
| New (unique, not-yet-known) listings per full sweep, all sources | ≥ 30 | `postings_seen` adds little over `applications` + `generation_runs`; close M1 as not worth building, keep only M2 (`skip_reason`) |
| Share of listings with a parseable salary (min or max + currency) | ≥ 25% | drop the salary column set (M1.b) and the pay section of the digest; keep everything else |
| Share of listings whose location classifies (remote/hybrid/onsite, not "unknown") | ≥ 60% | keep `location_raw` only; `remote_mode` becomes a best-effort column with a documented "unknown" majority and M4's remote-mode cuts are not built |
| Expected inserts/day (M0.b × M0.a new-share) | ≤ 2 000 | if higher, TTL drops from 180 to 90 days and cloudscraper sources are excluded from the write (they are the noisiest and the ones 07-M6 excludes anyway) |

If M0.a shows fewer than 30 new listings per sweep, the honest reading is that
URL dedup already leaves the owner with a near-complete record of what matters
and the market view is not worth a table. That would close this plan.

---

## M1 — `postings_seen`: one row per vacancy the hunt ever saw

**What changes.** New module `hunter/postings_seen.py` (own lazy-ensure DDL,
same pattern as `hunter/source_health.py` and `hunter/drive_ledger.py` — not
part of `init_db()`'s `applications` migrations, since this table has a
different lifecycle and no `user_id`):

```sql
CREATE TABLE IF NOT EXISTS postings_seen (
    url_norm        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    source          TEXT NOT NULL,          -- Job.source, the real one
    first_seen      TEXT NOT NULL,          -- UTC ISO
    last_seen       TEXT NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    title           TEXT NOT NULL DEFAULT '',
    company         TEXT NOT NULL DEFAULT '',
    company_norm    TEXT NOT NULL DEFAULT '', -- repost_gate.normalize_company
    location_raw    TEXT NOT NULL DEFAULT '',
    remote_mode     TEXT NOT NULL DEFAULT '', -- remote|hybrid|onsite|unknown
    city            TEXT NOT NULL DEFAULT '',
    salary_raw      TEXT NOT NULL DEFAULT '',
    salary_min      REAL,                   -- monthly, in salary_currency
    salary_max      REAL,
    salary_currency TEXT NOT NULL DEFAULT '',
    salary_contract TEXT NOT NULL DEFAULT '', -- b2b|uop|other|''
    lang            TEXT NOT NULL DEFAULT '', -- PL|EN from title (lang_guard)
    skills_listing  TEXT NOT NULL DEFAULT '', -- JSON list when the source gives one (JustJoin requiredSkills, NoFluff requirements, theprotocol, SmartJobs); else ''
    filter_verdict  TEXT NOT NULL DEFAULT '', -- 'passed' | one of FILTER_REASONS, as of first_seen
    filter_verdict_last TEXT NOT NULL DEFAULT '', -- same, as of last_seen (a filters.yaml edit changes it)
    text_hash       TEXT NOT NULL DEFAULT ''  -- sha1 of title+company+location+salary; NOT of posting text
);
CREATE INDEX IF NOT EXISTS idx_postings_seen_last ON postings_seen(last_seen);
CREATE INDEX IF NOT EXISTS idx_postings_seen_company ON postings_seen(company_norm);
```

`record_listings(jobs, verdicts)` is an upsert: a new `url_norm` inserts; a
known one bumps `last_seen`, `seen_count` and `filter_verdict_last` only —
first-seen attributes are never overwritten (a salary that changed on a re-post
is exactly the kind of thing `last_seen`-side columns could carry later, not
now). `prune(ttl_days)` deletes rows with `last_seen` older than the TTL.

**Wiring.** `hunter/main.py`, after Step 2 (filter) and before Step 3
(dedup): one call, `with best_effort("postings.record"):`, over `all_jobs`
with the per-job verdict (`'passed'` if the job is in `filtered`, else
`classify_job(j, flt=flt)` — the same call the Gmail report already makes, now
for every source). Placed after the filter so the verdict is known, before
dedup so URL-known vacancies are still counted as *seen* (that is what makes
`seen_count` a re-post counter). Runs in `asyncio.to_thread` like every other
DB touch in the loop. Manual pastes and `/force` never pass through here — a
pasted URL is not a market observation.

Nightly prune: a daily job in `hunter/schedules/` at 00:40 (`POSTINGS_TTL_DAYS`,
default 180 — or 90 if M0's volume rule says so), registered in
`schedules/__init__.py`.

**M1.b — deterministic parsers**, each its own small module with table-driven
tests, each returning `''`/`None` rather than guessing:

- `hunter/salary_parse.py` — `parse_salary(raw) -> SalaryParse(min, max,
  currency, period, contract)`; normalises hourly (×160) and yearly (÷12) to
  monthly, keeps `raw` when anything is ambiguous. Corpus for tests: the
  distinct `salary` strings M0.a collected (`--json` dumps them), one fixture
  file per source shape.
- `hunter/location_parse.py` — `classify_location(raw) -> (remote_mode,
  city)` using ONLY the existing vocabularies (`REMOTE_ANY`,
  `_PL_ANTI_HYBRID_CITIES`, `_anti_hybrid_cities(flt)`, candidate home-city
  aliases via `candidate.get`), so the market table and the filters agree on
  what "hybrid Warsaw" means. No new city list.

**Config.** `POSTINGS_SEEN_ENABLED` (default `true`), `POSTINGS_TTL_DAYS`
(default 180). Both in `.env.example` (handoff-readiness check (d)).

**Tests.** `tests/test_postings_seen.py`: upsert idempotency (same sweep twice
→ `seen_count` 2, `first_seen` unchanged), verdict-at-last-seen update, prune
by TTL, `best_effort` swallow (a broken DB never fails a hunt — the existing
hunt tests must still pass with the table absent), parser tables.

**Rollback.** `POSTINGS_SEEN_ENABLED=false` — the hunt loop skips the call;
the table stays, harmless. No column is added to `applications`, so nothing
downstream can break.

**Erasure.** No `user_id` column, on purpose: listings are not personal data
of a bot user (`hunter/erasure.py` discovers tables by `user_id` column and
correctly ignores this one). Company name and a recruiter-free title are the
employer's public advertisement, not a data subject's data. If a future
multi-user deployment ever needs per-user filter verdicts, that is a separate
`(user_id, url_norm) → verdict` table, not a column here.

## M2 — `skip_reason` on the tracker row, and the join that replaces new columns

**Why no `salary`/`location`/`source` columns on `applications`.** Every
attribute M1 stores is keyed by `url_norm`, and so is `applications`. A join
at read time (`funnel.py`, the digest, the tools) gives the applied row its
listing attributes for $0 and without a schema change or a second copy that
can drift. `applied_delay_hours` = `generation_runs.started_at −
postings_seen.first_seen`, again a join. Manual pastes have no `postings_seen`
row and show as NULL, which is the truth.

**What does need a column: `skip_reason`.** It is known only at the call site
and never derivable later. One migration in `hunter/db.py`
(`skip_reason TEXT NOT NULL DEFAULT ''`), stamped by:

| Writer | value |
|---|---|
| `commands/url_message._handle_skip` (Skip button) | `button` |
| `pipeline/gates.run_doomed_gate` HARD | `doomed:<rule>` (the finding's rule name) |
| `pipeline/gates.run_prescreen` in `skip` mode | `prescreen` |
| `add_react_skipped` | `react` |
| company+title dedup gate (`apply_api` 4.55 / `apply_cli`) | `dedup_ct` |
| `abort_after_generation` | `abort:<reason>` (the reason it already receives) |
| `add_skipped` from any other path | `other` |

Implementation: `add_skipped(job, reason="")` / `add_react_skipped(...,
reason=)` / `convert_own_applied_row(..., reason=)` gain an optional keyword,
default `''`, so no existing caller changes behaviour until it passes one. The
column is mirrored nowhere (not a Sheet column — the owner reads reasons via
the digest / `tools/`), which keeps the four-writer Sheet contract untouched.

**Tests.** One assertion per writer above that the reason lands, plus
`tests/test_apply_cli_abort.py`/`tests_doomed_gate_wiring.py` extended rather
than duplicated.

**Rollback.** Column stays, writers pass `''` — nothing reads it except
reports.

## M3 — `source` written, not guessed

Small and separable: `add_applied`/`add_skipped`/`add_failed`/`add_expired`
all have a `Job` or a `url` in hand; `add_pending` has the full `Job`. Stamp
`source` (`Job.source`; for `add_applied`, from the `postings_seen` row by
`url_norm` when present, else `''`) into a new `source TEXT NOT NULL DEFAULT
''` column, and make `funnel.py`'s URL-guessing the *fallback* for blank
values instead of the only path. This is the one M2-style duplicate column
worth having, because `funnel.py`'s guess is wrong for exactly the source
class (ATS aggregator) ROADMAP 4.3 most needs to judge. `generation_runs`
already carries `source` for applied rows since #267; this adds it for SKIP/
FAIL/EXPIRED rows and for rows older than #267. Backfill: a one-shot
`tools/backfill_source.py` that fills blanks from `postings_seen` (when M1 has
run long enough) and otherwise from the URL guess, marked `source_guessed=1`
— or simply leave old rows blank; open question 3.

## M4 — What the data is for: the digest and the company view

Everything here is read-side, `$0`, and lands only after M1 has at least four
weeks of rows (the digest compares weeks). None of it feeds back into the
hunt, the queue or the apply pipeline.

**M4.a — `/market [weeks]` + Monday 09:30 Telegram digest** (`hunter/market_report.py`,
`hunter/commands/market.py`, `schedules/market_digest.py`). Sections, each
with `n` and a Wilson 90% CI where it is a rate (reuse the helper from
`tools/funnel_sources.py`):

1. *Market this week vs the 4-week mean*: new unique listings by source;
   share passing the filters; top 3 rejection reasons.
2. *Remote mode split* of passed listings (remote / hybrid / onsite / unknown).
3. *Pay*: median and IQR of `salary_min`..`salary_max` for `filter_verdict =
   passed`, PLN B2B only, `n ≥ 10` or the line says "n too small".
4. *Demand terms*: top rising / falling terms over the last 4 weeks vs the
   previous 4, from `skills_listing` where sources give it (no text needed) —
   the same term-share machinery `tools/market_m0.py` has, on an unbiased
   corpus this time; intersected with `to_learn` terms → "in demand, not in
   your profile" list.
5. *My funnel by listing attributes*: sent → outcome (interview/rejected/
   offer/silence, from #276) by source and by remote mode, joined through
   `url_norm`; `applied_delay_hours` median for replied vs silence.
6. *Ghost jobs*: vacancies with `seen_count ≥ N` spanning ≥ 45 days that the
   filters passed (for the owner to eyeball before any rule acts on them).

**M4.b — Company memory view** (`tools/company_stats.py` first, a `/company
<name>` command second): per `company_norm` — listings seen, applied, sent,
outcomes, median days-to-outcome, agency-vs-direct flag (from the repost
gate's `normalize_company` legal-token stripping plus a small agency-name
list the owner already keeps in his head — open question 4). Read-only until
its numbers say a rule is warranted.

**No M4.c.** An earlier draft carried three "search rules" here (salary
floor, ghost-job warning, company-silence cooldown). Removed 2026-09-12 by
owner decision: this plan collects and reports, it does not act. The digest's
sections 3, 5 and 6 are exactly the data such rules would need; whether to
build any of them is a future plan.

---

## Risks

| Risk | Caught by |
|---|---|
| The hunt loop slows or fails on the new write (25 sources, hundreds of rows per sweep) | `best_effort("postings.record")` — a failure never reaches the loop; write runs in `to_thread` after the filter, one `executemany` per sweep; M0.b's volume rule sets the TTL |
| Table grows without bound | nightly prune by `last_seen`; `POSTINGS_TTL_DAYS`; the index on `last_seen` makes the prune cheap |
| A parser mis-classifies location or salary | Nothing acts on the value — it only appears in reports, always next to `n`; "unknown" is a first-class value; parsers are table-tested per source shape |
| Storing company names + titles for non-applied vacancies conflicts with 07-COMPLIANCE M6 | M6 governs a *published aggregate*; the owner's own bot storing public listing metadata for the owner is the same posture as today's `job_posting.txt`. When an aggregate tier is built, it reads a projection (`role_family × region × term`, `k ≥ 10`, no company) — the same `postings_seen` rows, minus the columns M6 forbids, from the allowlisted sources only. Documented in `docs/SOURCES_POLICY.md` when that day comes, not now |
| `filter_verdict` goes stale after a `filters.yaml` edit | `filter_verdict_last` is re-stamped on every sighting; a listing not seen again keeps its last known verdict, which is the truth as of `last_seen` |
| `skip_reason` values drift into free text | `SKIP_REASONS` tuple in `tracker.py` next to `OUTCOME_LABELS`, same "one definition" rule; `set`-style validation in the writer |
| Digest sends noise with n = 3 | every rate carries `n` and a Wilson CI; lines with `n < 10` print "n too small" instead of a number (same posture as `tools/funnel_sources.py`) |

## Cost

Zero LLM calls in every milestone. Storage: at the M0.b upper bound of ~2 000
inserts/day and ~600 bytes/row, 180 days ≈ 200 MB worst case; the realistic
figure (unique-new share × raw) is expected an order of magnitude lower —
M0 gives the number. One extra `executemany` per hunt sweep; one daily
`DELETE`. Owner time: none — every output is a report.

## Open questions

Owner decisions 2026-09-12 (all five closed):

1. TTL 180 days for `postings_seen` (90 if M0.b says volume is high) — **yes.**
2. Include the cloudscraper sources (pracuj, theprotocol, builtin, jobleads)
   and LinkedIn in `postings_seen` for the owner's own use — **yes.** (07-M6
   still excludes them from any *published* aggregate; that stays a
   read-side projection, see Risks.)
3. Backfill `source` on pre-existing rows — **no, leave old rows blank** and
   let the column fill going forward; `funnel.py`'s URL guess remains the
   fallback for blank values. No `tools/backfill_source.py`.
4. Agency-vs-direct signal for M4.b — **heuristic only** (the legal-token
   stripping already in `repost_gate.normalize_company` plus the agency
   boilerplate similarity the repost gate measures). No hand-kept agency
   list, no new YAML key.
5. Salary floor as filter vs warning — **moot.** Owner decision 2026-09-12:
   the plan does not touch generation, queueing or filtering at all; M4.c
   was removed. The digest still reports the pay distribution, so a floor
   can be chosen from data later, in a plan of its own.
