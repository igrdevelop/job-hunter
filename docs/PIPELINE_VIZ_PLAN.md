# Pipeline Visualization Plan

**Status:** in progress. M0 run on prod 2026-09-22 (result below); M1 in flight.
**Date:** 2026-09-22
**Motivation:** The owner keeps asking the same question — "do we have
independent processing queues, and where is each vacancy right now?" — and the
honest answer today is spread over six Telegram commands (`/status`, `/queue`,
`/health`, `/schedule`, `/unsent`, `/fails`), each showing one slice with no
common time window. The mental model the owner has (found → filtered → queued →
first generation → second generation → verdict → done) does not match what the
code actually does (one hunt queue, one apply queue with ONE worker, and every
generation stage running sequentially inside a single subprocess per vacancy).
A page that shows the real pipeline, with real numbers, answers the question
permanently and replaces the six-command tour. An n8n-style re-architecture was
considered and rejected (see Non-goals).

## Problem

1. **The shape of the pipeline is invisible.** There are exactly two
   independent queues: the hunt (`_hunt_lock`, FIFO, held for seconds when
   `APPLY_QUEUE_ENABLED`) and the apply queue (`PENDING` rows, drained by one
   `apply_worker_loop`). Inside an apply there are no queues at all —
   `fetch → gates → generate → ATS loop → judge → lang gate → render → verdict
   → refine (≤5 rounds) → delivery` is one process, one exit code, one
   `_resolve_outcome`. Nothing shows this, so the owner reasons about
   "queues" that don't exist and can't see the one bottleneck that does
   (one worker × ~30 min per CLI-served vacancy).
2. **Counts exist but live in different windows.** `/status` shows PENDING /
   IN_PROGRESS now; `/health` shows per-source yield over the last 20 runs;
   `/funnel` shows tracked/sent/answered over N days; `/unsent` shows the ready
   stack. None of them can answer "of the 412 listings found today, how many
   became documents?" because the hunt loop's own funnel numbers
   (`len(all_jobs)`, `filtered_out`, `dup_url`, `dup_ct`, `dup_cooldown`,
   `len(new_jobs)`, `filter_reasons` — `hunter/main.py:236-330`) go to one
   Telegram message and are never persisted.
3. **"Where is the current vacancy" cannot be read at all.** `hunter/metrics.py`
   (`generation_runs` + `pipeline_events`, shipped 2026-09-11 in #267) records
   stage transitions, but only at the END of a stage (`event="ok"|"error"|
   "blocked"`). There is no `start` event, and the refine loop — the longest
   part of a run — records nothing per round (only `refine_rounds` on the run
   row after the loop ends). So the best a reader can do is "last `ok` was
   `render`, therefore probably on verdict or refine for the last 25 minutes".
   Nothing reads these tables today anyway: `metrics.count_runs_since` exists
   for a `/status` line, and that is the whole read side.
4. **No timestamp on a PENDING row.** `add_pending` stamps `date`
   (YYYY-MM-DD) and `pending_meta`, not an insertion time, so "oldest job has
   waited 38 min" is not computable; `claimed_at` exists only from
   IN_PROGRESS on.

## Non-goals

- **No change to how the pipeline executes.** No per-stage queues, no N
  workers, no stage-level state machine. At ~6 applies/day the bottleneck is
  the wall time of one run, not parallelism, and the one-process-per-vacancy
  shape is what every outage/timeout/abort rule in `apply_worker` and
  `abort_after_generation` relies on. If throughput is ever needed, the
  cheaper lever is already designed: `apply_worker_loop(worker_id=N)` over the
  same PENDING queue (docs/HUNT_APPLY_SPLIT_PLAN.md M2, deferred).
- **No n8n / external orchestrator.** 90% of the pipeline is gates, scrubs,
  keep-best rollbacks and the tests that pin them; in n8n they become Code
  nodes with the same Python and none of the pytest/mypy/golden-E2E coverage.
  It would also add a second runtime and state store to a 4 GB VPS that
  already filled its disk once (2026-09-12). The picture n8n is wanted for is
  exactly what this plan builds, read-only, on top of the existing tables.
- **No LLM calls.** Every number on the page is a SELECT.
- **No writes from the page.** Read-only in every milestone; a future "cancel
  queued job" button is a separate plan (it needs `delete_pending_row` behind
  auth and a Telegram echo).
- **Not a replacement for `/funnel`, `/health`, `/fails`.** They stay as the
  Telegram-side views; the page consumes the same functions where it can.
- **Not the `profile_jobs` queue.** That is the site's own resume-store
  queue, already visible to the site; it is not a vacancy pipeline.
- **No Sheets column.** Nothing here touches the A–K / L / M / N / O contract.

## What the page shows (mockup agreed 2026-09-21)

Three tiers, top to bottom, one time-window toggle (today / 7 days):

| Tier | Stacks | Data source today |
|---|---|---|
| Hunt | found · filtered (with top reasons) · duplicates · new → queued; next slot + source | `source_runs.yield`, `postings_seen.filter_verdict_last`, `applications` rows by `date`+`source`; `schedules.grid.fire_minute` |
| Apply | queued · **in progress** (one card: company, source, pipeline, stage strip with the current stage lit, verdict → target, refine k/5) · cut at $0 (expired / doomed / repost / prescreen) · failures (+ next retry slot) | `applications.ats_status` PENDING/IN_PROGRESS + `claimed_at`, `generation_runs` with `finished_at IS NULL` + its last `pipeline_events` row, `generation_runs.outcome`, `skip_reason`, FAIL rows + `fail_count`, `apply_failures.jsonl` |
| Result | ready (applied, `sent=''`) with mean verdict · sent · outcomes · LLM spend | `applications` (`sent`, `ats_verdict`, `outcome_label`, `cost_usd`) |
| Footer | last N events; LLM-outage banner | `pipeline_events` ⋈ `generation_runs` ⋈ `applications`; config KV `llm_outage_until` |

The stage strip inside the in-progress card is a single row, not a set of
stacks — with one worker, a "vacancies on verdict" stack would read 0 or 1
forever and would lie about the architecture.

## M0 — Measure

`tools/pipeline_snapshot.py` builds the whole snapshot from the tables that
already exist, read-only (`mode=ro`), $0, no network, no LLM, and prints a
coverage section that answers whether those tables are good enough to draw
the page from, or whether M1 instrumentation has to land first.

```bash
docker compose exec -T job-hunter python tools/pipeline_snapshot.py --db tracker.db
docker compose exec -T job-hunter python tools/pipeline_snapshot.py --db tracker.db --days 7
docker compose exec -T job-hunter python tools/pipeline_snapshot.py --db tracker.db --json
```

Decision rules, fixed before the run:

1. **Run coverage.** Of the non-placeholder `applications` rows written in the
   window (`date` in window, `ats_status` not PENDING/IN_PROGRESS, `source`
   not blank — i.e. rows the queue actually produced), the share with a
   matching `generation_runs` row (same `url_norm`, `pipeline != 'backfill'`)
   must be **≥ 90%**. Below that the metrics wiring is leaking (a pipeline
   path that skips `start_run`, or the CLI skill writing the row itself) and
   M1 fixes that first; the page cannot be built on a table that misses one
   run in ten.
2. **Stage resolution.** For finished runs in the window, the share of run
   wall time that falls in the single longest gap between two consecutive
   `pipeline_events` rows. If that share is **> 50%** (expected: yes — the
   refine loop has no per-round events), M1 adds `start` events and one event
   per refine round. If it is ≤ 50%, the page derives "current stage" from
   the last event alone and M1 shrinks to the hunt table only.
3. **Hunt funnel consistency.** `source_runs.yield` counts raw listings per
   sweep (a URL seen in four sweeps counts four times); `postings_seen`
   counts unique `url_norm` (the same URL once). Report
   `found_raw − (passed_unique + rejected_unique)` as a share of `found_raw`.
   If it is **> 30%** the derived funnel would visibly not add up (412 found
   → 371 filtered + 29 dup + 12 new cannot be shown from these tables) and M1
   adds a `hunt_runs` table written once per hunt from the numbers
   `hunter/main.py` already holds in local variables. Below 30% the derived
   version ships and the table waits.
4. **Ready-stack sanity.** The tool's "ready" count (applied rows,
   `sent=''`, user-scoped) must equal `len(tracker.iter_unsent_rows())` minus
   its FAIL/EXPIRED/MANUAL entries. A mismatch means the page would disagree
   with `/unsent` on day one; fix the definition before anything else.
5. **Leaks.** Count `generation_runs` rows with `finished_at IS NULL` older
   than `APPLY_AGENT_CLI_TIMEOUT_SEC` (3 h). Any non-zero count is a run that
   crashed between `start_run` and `finish_run` (the subprocess was killed by
   the worker's timeout, or died on an unhandled exception). The page would
   show those as "in progress" forever, so M1 must either stamp them on the
   worker side (`apply_worker._resolve_outcome` knows the exit code) or the
   snapshot must ignore runs older than the timeout. The number decides which.

The tool also prints, per rule, PASS / FAIL / UNMEASURED with the numbers, and
it never guesses: a table that does not exist yet on the target DB (a
pre-M1 dev checkout) reports UNMEASURED rather than 0.

Also captured for the plan, no rule attached: how many `pipeline_events`
rows the last 7 days produced (row-growth check — `pipeline_events` has no
prune today, and at ~10 events/run × 6 runs/day it is ~2k rows/month, which
is fine, but the number should be on record before a per-round event
multiplies it).

### M0 result (prod, 2026-09-22)

Run locally against the 06:05 Warsaw backup snapshot
(`backups/tracker_db_20260922_040500_*.db`, `integrity_check` ok — never the
live WAL file) plus `logs/apply_failures.jsonl`; one `user_id`, 1,541
`applications` rows, 233 `generation_runs` (169 `api`, 64 `cli`, 0 backfill —
`tools/backfill_runs.py` was never run on prod), 402 `pipeline_events`.

| Rule | 1 day | 7 days | 30 days | Verdict |
|---|---|---|---|---|
| 1 run coverage (≥ 90%) | 1/1 | 48/48 | 64/64 | **PASS** — metrics wiring is complete; nothing to fix |
| 2 stage resolution (median ≤ 50%) | 100% | 99.8% (75 runs) | 99.8% (132 runs) | **FAIL** — 0 `start`, 0 `refine` events; the page would sit on "after verdict" for the whole refine loop |
| 3 hunt funnel gap (≤ 30%) | 13.5% | 94.3% (49,356 raw / 2,825 unique) | 96.5% | **FAIL** — as the rule anticipated, `source_runs` counts a listing once per sweep, so the derived funnel cannot add up over more than one sweep |
| 4 ready stack | 19 vs 24 | same | same | **FAIL** — see below |
| 5 leaked open runs (= 0) | 5 | 5 | 5 | **FAIL** — all `api`, 2026-09-10..15, outcome NULL; one stopped after `render`, the rest after `fetch`/`generate` |

Rule 4 was a definition, not data: the 5-row gap is exactly the APPLIED rows
carrying a dash in `sent` — three web-UI declines (`app_status='Filter
miss'`, `owner_reason=location`) and two old manual dashes. `/unsent`'s SQL
accepts those; "ready to send" must not. Decision: the page's ready stack is
`sent=''` only, owner-declined rows get their own count
(`declined_by_owner_dash` in the snapshot). The tool was fixed the same day
to compare like with like; the old assertion could never pass on real data.

Tool defects the prod run exposed, fixed before merge: the APPLY header
labelled prod's queue mode from the LOCAL `.env` (now inferred from the data
— any row that ever carried `claimed_at`); `cost_usd = 0.0` (every CLI-served
row since 2026-08-04, 42 of 51 in the 30-day window) was counted as "priced"
and printed "$0.0 each" (zero is unpriced now); the FAIL line mixed in-window
and all-time numbers without saying so; rule 1 silently dropped pre-M3 rows
with a blank `source` (152 of 216 in the 30-day window — now reported as
`excluded_blank_source`).

Side observation, not this plan's concern: every `api` run in the window hit
`Anthropic outage (400)` and fell back to the CLI (no `cost_usd > 0` since
2026-08-04), and the 63 `cli_error` outcomes are the 2026-09-10..21 argv
incident fixed in #284 — the same URL was fetched and failed four times on
09-21 alone. The page will make both visible at a glance, which is the point.

**M1 scope, decided by the numbers:** `start` + per-refine-round events
(rule 2), `hunt_runs` (rule 3), `queued_at` (no rule needed), orphan-run
stamping (rule 5). Rule 1 needs nothing.

## M1 — Instrumentation (bot repo)

Four small PRs, one per item, each cut from `origin/master`:

- **`start` events** (`metrics.stage(run_id, <stage>, "start")`) at the top
  of every stage that already has an `ok`/`error`, in BOTH `apply_api` and
  `apply_cli`. Plus one event per refine round from `verdict_refine.refine_loop`
  (`stage="refine"`, `event="accepted"|"rejected"`, payload
  `{round, kind, score}`) — the loop already has every number in hand at the
  point it decides. Test: the golden E2E tests assert the event sequence for
  the happy path (both pipelines). Rollback: events are inside
  `best_effort("metrics")`; deleting the call sites restores today's shape.
- **`hunt_runs` table** (only if rule 3 fails): one row per hunt —
  `ts, sources (JSON list), found, filtered, filter_reasons (JSON),
  dup_url, dup_ct, dup_cooldown, new, capped, queued` — written from
  `_run_hunt_impl` inside `best_effort("hunt.record")`, lazy-ensure DDL in
  the same self-contained style as `source_runs`/`postings_seen`. Test: a
  unit test over a fake job list checks the written row equals the
  Telegram summary's numbers. Rollback: a flag `HUNT_RUNS_ENABLED`, table
  left in place.
- **PENDING insertion time**: `queued_at` column (UTC ISO) stamped by
  `add_pending`, read by the "oldest waits N min" line. Migration is one
  `ALTER TABLE ADD COLUMN` in `hunter/db.py`'s existing column list; NULL for
  older rows, shown as "—".
- **Orphan runs** (only if rule 5 is non-zero): `apply_worker._resolve_outcome`
  calls `metrics.finish_run` for the url's open run when the subprocess
  exited without one (timeout/kill). It already has the exit code and the
  outcome string.

Plus `hunter/pipeline_snapshot.py`: the M0 tool's query layer moved into the
package (`build_snapshot(user_id, days) -> dict`), the tool becomes a thin
CLI over it, and `/status` gains nothing — this module exists so the
contract below has one reference implementation with tests against a fixture
DB.

CLAUDE.md: `hunter/metrics.py` had no Repository Layout entry at all (shipped
in #267 without one) — added with this plan's M0 commit, since the snapshot
tool is its first real reader. M1 adds `pipeline_snapshot.py`'s own entry
when the query layer moves into the package.

## M2 — Contract + API (bot repo docs, job-hunter-api)

The API is NestJS and already reads `tracker.db` directly, so the bot exposes
no endpoint. The contract is the snapshot JSON shape:

- `docs/PIPELINE_SNAPSHOT_CONTRACT.md` in this repo (the same precedent as
  `job-hunter-api/docs/RESUME_PROFILE_STORE.md`, only in the other
  direction): every key, its SQL definition, its window semantics, and the
  user-scoping rule (`applications.user_id = ?`; `source_runs`,
  `postings_seen`, `generation_runs` are global by design — `generation_runs`
  carries `user_id` and is filtered where present).
- A fixture: `tests/fixtures/pipeline_snapshot/{tracker_fixture.sql,
  expected.json}` — the bot's `build_snapshot` must reproduce `expected.json`
  from the fixture DB byte-for-byte, and the API repo's TypeScript port runs
  the same fixture through the same assertion. Schema drift then fails in
  both repos, the way the scout payload fixtures do.
- API: `GET /pipeline/snapshot?days=1|7` for the authenticated user. Polling
  every 10–15 s is enough; no WebSocket (a run changes stage every few
  minutes, not every second).

## M3 — Page (job-hunter-site)

`/pipeline` route, three tiers as in the mockup, a today/7-days toggle, the
in-progress card with the stage strip, an events footer, an outage banner.
Polls M2. Click on a stack expands the list of rows in it (the API returns
ids; the site already has the applications table view to link to). Dark
mode, mobile width (stacks wrap 4 → 2). No new dependency on the site side.

## Later (not planned here)

- The `BLOCKED` queue from docs/APPLY_FAILURE_QUEUES_PLAN.md M3 becomes one
  more stack in the Apply tier — the snapshot contract gets a `blocked` key
  when that ships, not before.
- A second in-progress card for the dual-apply shadow (detached process, no
  tracker row; it has `generation_runs` rows only if `dual_apply` starts
  calling `metrics`, which it does not today).
- A "found / ready per day" sparkline in 7-day mode.

## Risks

- **Reading a WAL database the bot is writing.** `mode=ro` + WAL is safe;
  the API already does this. The snapshot takes a handful of indexed SELECTs
  — `idx_ats`, `idx_generation_runs_url_norm`, `idx_pipeline_events_run_id`
  exist; the `postings_seen` window query scans by `last_seen` (no index;
  180-day TTL keeps the table small — measure in M0, add an index in M1 if
  the query is > 100 ms on prod).
- **A stale IN_PROGRESS row lies for up to `APPLY_CLAIM_TIMEOUT_MIN`.** The
  page shows what `/status` shows; `scheduled_reset_stale_claims` is the
  existing rail. The snapshot adds `claimed_min_ago` so the page can grey the
  card past the timeout.
- **Half-instrumented runs.** Between `start_run` and the first `stage()` the
  page shows "fetch" — correct, since fetch is the first stage.
- **Multi-user.** Every `applications` query is scoped by `user_id`;
  `source_runs`/`postings_seen` are global and shown as-is (the hunt is one
  process for every user). `count_pending()`/`count_in_progress()` in
  `tracker.py` are NOT user-scoped today (they serve the owner's `/status`);
  the snapshot module scopes its own copies and does not change them.
- **Cost of a per-round event.** ~5 extra rows per run; `pipeline_events` has
  no prune. M0 records the current growth; M1 adds a 90-day prune to
  `scheduled_postings_prune`'s sibling slot only if growth warrants it.

## Cost

Zero LLM calls in every milestone. M1 adds ~6–10 SQLite inserts per apply
run (inside `best_effort("metrics")`) and one per hunt. The page is a
poll of a read-only endpoint.

## Open questions

1. Window default: "today" (Warsaw calendar day) or "last 24 h"? The mockup
   says today; `/funnel` uses N days. Proposal: calendar day, Warsaw, matching
   the schedule grid the owner already reads.
2. Should the hunt tier be shown per source (25 rows) behind the totals, or is
   `/health` enough for that? Proposal: totals + "N of 25 sources ran", per
   source on click.
3. Does the API repo want the fixture-DB contract test, or only the JSON
   shape doc? The fixture is more work up front and the only thing that
   catches drift.
4. Is the site page owner-only, or does a linked non-owner user see their
   own Apply/Result tiers with the shared Hunt tier? Proposal: the latter,
   since every query is already user-scoped where the data is.
