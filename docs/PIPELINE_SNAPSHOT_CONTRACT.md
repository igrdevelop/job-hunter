# Pipeline snapshot contract (bot side)

docs/PIPELINE_VIZ_PLAN.md M2, the bot-repo half. This document is the JSON
shape of `tools/pipeline_snapshot.py --json`, key by key, with the SQL that
produces each value. job-hunter-api ports these queries to TypeScript against
this document (`GET /pipeline/snapshot?days=1|7` — it already reads
`tracker.db` directly, so the bot exposes no endpoint); job-hunter-site's
`/pipeline` page (M3) consumes the API. Same precedent as
`job-hunter-api/docs/RESUME_PROFILE_STORE.md` and `docs/ERASURE_CONTRACT.md`,
only in the bot → API direction.

**The contract is derived from the tool, never invented.** Every key below
exists in the tool's `--json` output over the fixture DB
`tests/test_pipeline_snapshot_tool.py::fixture_db` (run 2026-09-22 at
`origin/master` 0cdc330, PRs #292 + #293 included), and every key in that
output is listed here. When the tool and this document disagree, the tool is
wrong OR this document is stale — either way, fix both in the same PR, the
same discipline as tracker.py's column constants. The reference
implementation is the tool itself (`build_snapshot(db_path, *, days, user_id,
failures_log, events_limit) -> dict`); the plan's `hunter/pipeline_snapshot.py`
package move has not happened, and nothing here depends on it.

## What lives where

- `tools/pipeline_snapshot.py` — the reference implementation. Read-only
  (`sqlite3` URI `?mode=ro`), no network, no LLM, no writes. `--json` prints
  `json.dumps(snap, ensure_ascii=False, indent=2, default=str)`.
- `tests/test_pipeline_snapshot_tool.py` — the fixture DB the key set is cut
  from, and the pinned counts.
- Tables read, with their owning module (all in the one `tracker.db`):
  `applications` (`hunter/db.py` + `hunter/tracker.py`), `generation_runs` +
  `pipeline_events` (`hunter/metrics.py`), `hunt_runs` (`hunter/hunt_runs.py`),
  `source_runs` (`hunter/source_health.py`), `postings_seen`
  (`hunter/postings_seen.py`), `config` (KV; key `llm_outage_until`,
  `hunter/llm_outage.py`). Plus ONE file: `logs/apply_failures.jsonl`
  (`hunter/apply_failures_log.py`).
- Consumers: job-hunter-api (M2 port), job-hunter-site (M3 page). The
  Telegram commands (`/status` `/queue` `/health` `/funnel` `/unsent`
  `/fails`) do NOT read this shape and are not changed by it.

## General conventions

### Window

`--days N` (N ≥ 1, default 1) is a window of **N Warsaw calendar days ending
today** (`TIMEZONE = "Europe/Warsaw"`, `hunter/config.py`). With `now` the
UTC instant of the run:

- `start` = local midnight of (Warsaw today − (N−1) days), converted to UTC;
  `window.start_utc` is that instant as `isoformat(timespec="seconds")`
  (`2026-09-21T22:00:00+00:00` for N=1 on 2026-09-22, CEST).
- `dates` = the N local calendar days as `YYYY-MM-DD` strings.
- `label` = `"today"` for N=1, else `"last N days"`.

Two comparison modes, decided per column by how its writer stamps it:

| Mode | Columns | How |
|---|---|---|
| **string ≥ `start_utc`** | `hunt_runs.ts`, `source_runs.ts`, `postings_seen.first_seen` / `last_seen`, `generation_runs.started_at`, `pipeline_events.ts` | All written as UTC `isoformat(timespec="seconds")` → `…+00:00`, so a plain text `>=` against `start_utc` is a correct chronological compare. Never parsed. |
| **`date IN (dates…)`** | `applications.date` | A local `YYYY-MM-DD` string (`date.today()` in the writing process — see caveat below). Compared as a set membership, never as a timestamp. |
| **parsed, `≥ start`** | `applications.outcome_at`, `apply_failures.jsonl` `ts`, `applications.sent` (via `sent_parse`) | Parsed with `_parse_ts` (accepts `…+00:00`, `…Z`, and a naive ISO string treated as UTC; `None` on garbage), then compared as datetimes; `sent` is parsed by `hunter.sent_parse.parse_sent_date` into a date and matched against `dates`. |
| **parsed, "minutes ago"** | `applications.queued_at`, `applications.claimed_at` (`%Y-%m-%dT%H:%M:%SZ`, `tracker._QUEUE_TS_FMT`), `generation_runs.started_at`, `pipeline_events.ts` | `_minutes_ago(v, now) = max(0, floor((now − v) / 60 s))`; `None` when NULL/unparseable. Clock skew clamps to 0, never negative. |

Caveat — RESOLVED 2026-09-24: the container ran in **UTC** until then (`TZ`
unset, verified on prod 2026-09-22), so `applications.date` was the UTC calendar
day while the window is Warsaw calendar days — a row written between Warsaw
midnight and 02:00 CEST carried the previous day. The bot's `docker-compose.yml`
now sets `TZ=Europe/Warsaw`, so every row written after that deploy carries the
Warsaw day and the set-membership comparison above is exact. Rows written
before it keep their UTC day (no backfill): a 1-day window over pre-switch data
can still miss a 00:00–02:00 row, a 7-day window is off by at most that edge. A
port must still compute the WINDOW in `Europe/Warsaw` explicitly (the API's own
process TZ is not this contract's concern).

### User scoping

- `applications`: **every** query carries `user_id = ?` (`--user`, default
  `config.current_user_id()`; an empty string is accepted and then matches
  only rows with `user_id = ''`, and the top-level `user_id` key reads
  `"(unscoped: empty user_id)"`). The API passes the authenticated user's id.
- `source_runs`, `postings_seen`, `hunt_runs`: **global by design** — the hunt
  is one process for every user, and none of these tables has a `user_id`
  column (`hunter/erasure.py` discovers tables by that column and correctly
  skips them). The Hunt tier is the same for every viewer.
- `generation_runs` carries `user_id` but is only PARTLY scoped: `apply.runs`
  uses `(user_id = ? OR user_id = '')` (a pre-multi-user row has `''`);
  `in_progress.cards[].run` matches by `url_norm` only (the card itself
  came from a user-scoped `applications` row); the `coverage` rules and
  `events` read `generation_runs`/`pipeline_events` with no user predicate at
  all. The API should scope `apply.runs` exactly as the tool does and treat
  the rest as global diagnostics.

### UNMEASURED / `null`

A table that does not exist on the target DB (a pre-M1 backup, a fresh dev
DB — the dev checkout's `tracker.db` has no `applications` table at all) makes
its block `null`, never `0`; a `coverage` rule that needs it reports
`{"verdict": "UNMEASURED", "why": "..."}` instead of numbers. Only
`applications` is mandatory — its absence aborts (`SystemExit`, "not a
tracker.db"). Two blocks carry an explicit reason next to the `null`:
`hunt.hunt_runs_unmeasured` (table missing, or present but lacking a column
`HUNT_RUN_REQUIRED_COLUMNS` needs — `"hunt_runs table lacks columns: a, b"`)
and the rule objects' `why`. Optional `applications` columns (`source`,
`claimed_by`, `queued_at`, `skip_reason`, `fail_count`, `ats_verdict`,
`outcome_label`, `outcome_at`) are probed with `PRAGMA table_info` and
substituted by `''`/`NULL` when absent, so a pre-migration DB still snapshots.

### JSON typing

- Integers are Python `int`; ratios/means are `float` rounded to 1 decimal
  (`_pct` → `round(100·part/whole, 1)`, `None` when `whole == 0`); money to 2.
- `Counter.most_common()` results serialise as **arrays of `[label, count]`
  pairs** (JSON has no tuple) — `top_reasons`, `top_filter_reasons`,
  `by_source`, `outcomes`, `by_reason`, `by_outcome`, `outcomes_in_window`.
  Sorted by count descending; ties keep first-seen order (Counter is stable).
  Where the tool caps (`most_common(8)` / `(10)`) it is noted per key.
- Objects built from a `Counter`/`dict` (`by_status`, `by_trigger`,
  `per_source`, `cut_zero_cost`) are **maps** — key order is insertion or a
  presentation sort and carries no meaning.
- `sqlite3.Row` values pass through untouched: a REAL column arrives as
  `85.0`, an INTEGER as `1`, NULL as `null`.
- `*.at` fields are local-time display strings (`HH:MM` for a Warsaw-today
  timestamp, `MM-DD HH:MM` otherwise, `--:--` if unparseable). They are
  presentation helpers for the text report; the API may drop them and format
  from the `ts` it already has.

## Key reference

Top level:

| Key | Type | Definition |
|---|---|---|
| `generated_at` | str | `now` as UTC `isoformat(timespec="seconds")`. |
| `window` | object | `{label: str, days: int, start_utc: str, tz: "Europe/Warsaw"}` — see Window. |
| `user_id` | str | The `--user` value, or `"(unscoped: empty user_id)"` when empty. |
| `hunt` / `apply` / `result` | object | The three tiers, below. |
| `events` | list \| null | Footer, below. `null` when `pipeline_events` or `generation_runs` is missing. |
| `coverage` | object | Diagnostic rules, below (see also "Not in the contract"). |

### `hunt`

| Key | Type | Definition |
|---|---|---|
| `window` | str | Same as `window.label` (duplicate, kept for the text report). |
| `hunt_runs` | object \| null | Totals over `SELECT … FROM hunt_runs WHERE ts >= ? ORDER BY id` with `?` = `start_utc`. `null` when the table is missing OR lacks any of `HUNT_RUN_REQUIRED_COLUMNS = ("ts", "trigger", "sources", "filter_reasons", *COUNT_COLUMNS)`; then `hunt_runs_unmeasured` says why. Empty window ⇒ all zeros, `last: null`. |
| `hunt_runs.hunts` | int | Row count in the window. |
| `hunt_runs.found` … `duration_ms` | int ×10 | `SUM` of each `hunter.hunt_runs.COUNT_COLUMNS` entry, in DDL order: `found`, `filtered_out`, `dup_url`, `dup_ct`, `dup_cooldown`, `new`, `capped`, `queued`, `applied_inline`, `duration_ms` (NULL → 0). Same arithmetic as `hunt_runs.sum_window`. |
| `hunt_runs.last` | object \| null | The newest row (last by `id`): `{ts: str (raw), at: str (display), trigger: str, sources: list[str] (JSON column parsed; non-list/garbage → []), found: int, new: int}`. |
| `hunt_runs.by_trigger` | map | `trigger → count`; `''`/NULL trigger → `"?"`. Values today: `scheduled` \| `manual` (`force` reserved, never written). |
| `hunt_runs.top_filter_reasons` | pairs | `filter_reasons` JSON (`reason → count`) merged across the window's rows, `most_common(8)`; non-dict JSON ignored, non-int values skipped, negatives clamped to 0. |
| `hunt_runs_unmeasured` | str \| null | `"hunt_runs table missing"` / `"hunt_runs table lacks columns: …"`; `null` when `hunt_runs` is populated. |
| `source_runs` | object \| null | `SELECT source, ts, yield, ok, error FROM source_runs WHERE ts >= ? ORDER BY id`. `null` when the table is missing. |
| `source_runs.runs` | int | Row count. One row per (source, hunt slot) — a listing counts once per SWEEP here. |
| `source_runs.found_raw` | int | `SUM(yield)`. |
| `source_runs.sources_ran` | int | Distinct `source` names in the window. |
| `source_runs.sources_last_run_ok` | int | Sources whose LAST row in the window (by `id`) has `ok` truthy. |
| `source_runs.errors` | int | Rows with `ok` falsy. |
| `source_runs.per_source` | map | `source → {runs: int, found: int, errors: int, last_ok: bool}`, presentation-sorted by `found` desc. |
| `postings_seen` | object \| null | `SELECT filter_verdict_last, first_seen, source FROM postings_seen WHERE last_seen >= ?`. `null` when the table is missing. |
| `postings_seen.unique_seen` | int | Row count — one per `url_norm` seen in the window (a listing counts ONCE here, hence rule 3's gap vs `found_raw`). |
| `postings_seen.new_this_window` | int | Rows with `first_seen >= start_utc` (string compare). |
| `postings_seen.passed` | int | `filter_verdict_last == 'passed'`. |
| `postings_seen.rejected` | int | Every other value (NULL/`''` included). |
| `postings_seen.top_reasons` | pairs | For rejected rows, the verdict's prefix before the first `:` (`"location: Berlin"` → `"location"`; blank → `"(blank)"`), `most_common(8)`. |
| `entered_tracker` | object | `SELECT ats_status, source FROM applications WHERE user_id = ? AND date IN (dates…)` — `source` is `''` when the column is absent. |
| `entered_tracker.rows` | int | Row count. |
| `entered_tracker.by_status` | map | `_bucket_status(ats_status) → count`. Buckets: `PENDING`, `IN_PROGRESS`, `SKIP`, `FAIL`, `MANUAL`, `EXPIRED` (upper-cased exact matches), `(blank)` for `''`/`—`/`–`/`-`, else `APPLIED` (any other value is a score string like `"94"`). |
| `entered_tracker.by_source` | pairs | `source → count`, `''` → `"(blank)"`, `most_common(10)`. |
| `next_slot` | object \| null | `{at: "HH:MM", in_min: int, source: str, sources_total: int}` from the local scheduler grid, `{error: str}` when `hunter.sources`/`hunter.schedules.grid` cannot be imported, `null` when no base time parses. **Not in the contract** — see below. |

### `apply`

| Key | Type | Definition |
|---|---|---|
| `queue_mode_observed` | bool | Inferred from the DATA, never from config (#293) — three signals, ANY one is enough: (1) `hunt_runs` exists, has a `queued` column, and `SELECT 1 FROM hunt_runs WHERE queued > 0 LIMIT 1` hits (the one DURABLE trace: `_clear_own_placeholder` deletes the PENDING/IN_PROGRESS row, and its `claimed_at`/`queued_at` with it, before the terminal row is written, so a quiet queue read "never used" from signal 3 alone on prod); (2) `SELECT 1 FROM applications WHERE ats_status IN ('PENDING','IN_PROGRESS') LIMIT 1` (NOT user-scoped); (3) the `claimed_at` column exists and `SELECT 1 FROM applications WHERE claimed_at IS NOT NULL LIMIT 1`. `false` on a bare DB. |
| `queue_enabled_local_config` | bool | This machine's `APPLY_QUEUE_ENABLED`. **Not in the contract.** |
| `pending` | object | `SELECT company, title, date, rowid, source, queued_at FROM applications WHERE user_id = ? AND ats_status = 'PENDING' ORDER BY rowid` — `rowid` IS the queue order `tracker.claim_pending` drains (`id` is a random UUID). `queued_at` → `NULL` when the column is absent. |
| `pending.count` | int | Row count (user-scoped, unlike `tracker.count_pending`). |
| `pending.oldest_date` | str \| null | `date` of the HEAD row; `null` on an empty queue. |
| `pending.oldest_wait_min` | int \| null | `_minutes_ago(head.queued_at)` — the HEAD row's wait, same rule as `tracker.oldest_pending_wait_min`: a NULL `queued_at` at the head (row written before the column existed) reports `null`, the tool never skips ahead to a younger stamped row. `null` on an empty queue. |
| `pending.head` | list | First 5 rows in queue order: `{company: str, title: str, source: str, wait_min: int \| null}`. |
| `in_progress` | object | `SELECT company, title, url_norm, claimed_at, claimed_by, source FROM applications WHERE user_id = ? AND ats_status = 'IN_PROGRESS' ORDER BY claimed_at`. With one worker (`apply_worker_loop(worker_id=0)`) `count` is 0 or 1 in practice; the shape is a list regardless. |
| `in_progress.count` | int | Row count. |
| `in_progress.cards[]` | list | One card per row, below. |
| `cards[].company` / `title` / `source` | str | Raw columns. |
| `cards[].claimed_by` | str | `hostname:pid` tag stamped by `claim_pending` (`''` before that column existed or when absent). |
| `cards[].claimed_min_ago` | int \| null | `_minutes_ago(claimed_at)`. |
| `cards[].stale` | bool | `claimed_min_ago > APPLY_CLAIM_TIMEOUT_MIN` (60 — the bot's default; the same threshold `scheduled_reset_stale_claims` uses to bounce the row back to PENDING). `false` when `claimed_min_ago` is `null`. |
| `cards[].run` | object \| null | The open `generation_runs` row for the card: `SELECT run_id, pipeline, profile, gen_model, started_at, verdict_first, verdict_final, refine_rounds, refine_accepted FROM generation_runs WHERE url_norm = ? AND finished_at IS NULL ORDER BY started_at DESC LIMIT 1` (no user predicate). `null` when the metrics tables are missing, the row's `url_norm` is `''` (paste mode), or no open run matches (a pre-metrics DB, or a metrics gap). |
| `run.run_id` | str | `generation_runs.run_id` (uuid4 hex). |
| `run.pipeline` | str | `api` \| `cli` (`backfill` rows are never open, so never here). |
| `run.profile` | str | `profile` if non-empty else `gen_model`; `''` when both are (the fixture's case). |
| `run.elapsed_min` | int \| null | `_minutes_ago(started_at)`. |
| `run.events` | int | Count of `pipeline_events` rows for the run (`SELECT ts, stage, event, duration_ms, payload FROM pipeline_events WHERE run_id = ? ORDER BY id`). |
| `run.last_event` | object \| null | `{stage, event, at}` of the last event by `id`; `null` with no events. |
| `run.current_stage` | object | `{stage: str, basis: str}` from `_infer_stage`, below. |
| `run.stage_started_min_ago` | int \| null | Minutes since the `start` event of `current_stage.stage`. Walk the events backwards to the LAST `start` row: if its `stage` equals the current stage → `_minutes_ago(ts)`, otherwise `null` (the current stage was inferred, or its `start` predates M1). `null` when no `start` row exists at all (pre-M1 run). |
| `run.refine_progress` | object \| null | The latest refine ROUND: the last event (by `id`) with `stage = 'refine'` and `event IN ('accepted','rejected','discarded')` (`REFINE_ROUND_EVENTS` — the loop's `start` row carries no round). `{round, kind, score, best, outcome: event, at}` where `round`/`kind`/`score`/`best` come from the JSON `payload` (`{round, kind, score, best, reason}` written by `verdict_refine.refine_loop::_record`; a missing/garbage payload yields `null`s, `discarded` carries `score: null`). `null` before the first round or on a pre-M1 run. |
| `run.verdict_first` / `verdict_final` | float \| null | Raw `generation_runs` columns (REAL). |
| `run.refine_rounds` / `refine_accepted` | int \| null | Raw columns; both are stamped by `update_run` AFTER the loop, so on a live card they are usually `null` until the loop ends (the fixture pre-stamps `refine_rounds = 1`). |
| `runs` | object \| null | `SELECT outcome FROM generation_runs WHERE started_at >= ? AND pipeline != 'backfill' AND (user_id = ? OR user_id = '')`. `null` when `generation_runs` is missing. |
| `runs.started` | int | Row count — runs STARTED in the window (`started_at`, string compare). |
| `runs.outcomes` | pairs | `outcome → count`, `most_common()` (uncapped); NULL outcome (still open) → `"(open)"`. Values the pipeline writes: `ok`, `no_docs`, `expired`, `too_short`, `fetch_error`, `fetch_blocked`, `skip_react_pre_llm`, `skip_backend_only`, `skip_doomed_gate`, `reused_repost`, `skip_prescreen`, `skip_react_post_llm`, `skip_dedup_company_title`, `blocked_judge`, `blocked_lang_gate`, `bogus_company`, `llm_outage`, `error` / an `error_type` string (API), `cli_timeout`, `cli_error`, `cli_no_folder` (CLI); plus the parent/sweeper stamps `orphan:<outcome>` (`hunter.services.apply_service._settle_orphan_run`) and `orphan:stale` (`metrics.reset_stale_open_runs`). Treat the set as open-ended — a new gate adds a value without a contract bump. |
| `runs.cut_zero_cost` | map | `outcome → count` restricted to `ZERO_COST_OUTCOMES = ("expired", "too_short", "skip_react_pre_llm", "skip_backend_only", "skip_doomed_gate", "reused_repost", "skip_prescreen")` — decided BEFORE the first generation call, $0. Only non-zero entries are present. |
| `runs.cut_zero_cost_total` | int | Sum of the above. |
| `skipped_rows` | object \| null | `SELECT ats_status, skip_reason FROM applications WHERE user_id = ? AND date IN (dates…) AND ats_status IN ('SKIP','EXPIRED')`. `null` when the `skip_reason` column is absent. |
| `skipped_rows.count` | int | Row count. |
| `skipped_rows.by_reason` | pairs | An `EXPIRED` row → `"EXPIRED"`; a SKIP row → its `skip_reason` prefix before the first `:` (`tracker.SKIP_REASON_PREFIXES`: `button`, `doomed`, `prescreen`, `react`, `dedup_ct`, `abort`, `other`), blank → `"(untagged)"` (pre-M2 rows). `most_common()`. |
| `failures` | object | From `SELECT date, fail_count FROM applications WHERE user_id = ? AND ats_status = 'FAIL'` (ALL time, then split). |
| `failures.in_window` | int | Rows with `date IN dates`. |
| `failures.retryable_total` | int | ALL-TIME rows with `fail_count < MAX_FAIL_RETRIES` (3, `hunter.tracker`) — or every FAIL row when the `fail_count` column is absent. |
| `failures.gave_up_total` | int | ALL-TIME rows with `fail_count >= 3` (0 when the column is absent). These are the rows `/retry_reset` revives. |
| `failures.next_retry` | object \| null | `{at: "HH:MM", in_min: int}` — the next of the local `RETRY_FAILED_TIMES` (default `02:45,07:45`); `null` when none parses. **Not in the contract** (local config). |
| `failures.log_records` | object \| null | `logs/apply_failures.jsonl` (`--failures-log`): every JSON line whose `ts` (`%Y-%m-%dT%H:%M:%SZ`) parses and is `>= start` → `{in_window: int, by_outcome: pairs}` (`outcome → count`, missing → `"?"`). `null` when the file does not exist or cannot be read. A reader without access to `logs/` returns `null`, which is a valid value. |
| `llm_outage` | object | `{paused: bool, remaining_min: int}` from `SELECT value FROM config WHERE key = 'llm_outage_until'` — an epoch-seconds string; `paused = until − now > 0`, `remaining_min = floor(left / 60)`. `{paused: false, remaining_min: 0}` when the `config` table or the key is missing, or the value is not numeric. |

### `result`

All from ONE query, `SELECT date, ats_status, sent, cost_usd, ats_verdict,
outcome_label, outcome_at FROM applications WHERE user_id = ?` (ALL time;
absent optional columns → `NULL AS <col>`), then split in Python. "applied"
below means `_bucket_status(ats_status) == 'APPLIED'` (see
`entered_tracker.by_status`).

| Key | Type | Definition |
|---|---|---|
| `ready.count` | int | Applied rows with `(sent or '').strip() == ''` — ALL time, not windowed: the ready stack is a backlog. A dash in `sent` (`—`/`–`/`-`) is NOT ready: on an applied row it means the owner declined it by hand (web-UI "Filter miss"/"Skipped", or an old manual dash) — see rule 4. |
| `ready.produced_in_window` | int | Applied rows with `date IN dates` (any `sent`). |
| `ready.mean_verdict` | float \| null | Mean of `ats_verdict` over READY rows where it is not NULL, 1 decimal; `null` when none. |
| `sent_in_window` | int | Applied rows where `sent_parse.classify(sent) == 'applied'` (a real date parsed out of the free-text Sent cell) AND `parse_sent_date(sent)` formatted `YYYY-MM-DD` is in `dates`. |
| `outcomes_in_window` | pairs | Over ALL rows (any status): `outcome_label → count` where the label is non-empty AND `_parse_ts(outcome_at) >= start`. Labels: `tracker.OUTCOME_LABELS` = `interview` / `rejected` / `offer` / `silence`. `most_common()`. |
| `cost.total_usd` | float | `SUM(cost_usd)` over PRICED rows, 2 decimals. Priced = applied AND `date IN dates` AND `cost_usd IS NOT NULL AND cost_usd > 0`. `0.0` is UNPRICED, not free: every CLI-served run since the M4b fallback stamps `0.0` (42 of 51 prod rows in the 30-day window on 2026-09-22). |
| `cost.priced_rows` | int | Count of priced rows. |
| `cost.unpriced_rows` | int | `produced_in_window − priced_rows` (NULL or 0.0). |
| `cost.per_priced_row_usd` | float \| null | `total_usd / priced_rows`, 2 decimals; `null` when 0 priced rows. |

### `events`

`null` unless both `pipeline_events` and `generation_runs` exist. Otherwise
the newest `--events` rows (default 15; the fixture test uses 10) of:

```sql
SELECT e.ts, e.stage, e.event, e.duration_ms, e.payload, r.pipeline, r.url_norm,
       (SELECT company FROM applications a WHERE a.url_norm = r.url_norm LIMIT 1) AS company
FROM pipeline_events e
JOIN generation_runs r ON r.run_id = e.run_id
ORDER BY e.ts DESC, e.id DESC LIMIT ?
```

Not windowed, not user-scoped (the `company` subquery has no `user_id`
predicate and no `ORDER BY` — with several users' rows sharing a `url_norm`
it returns an arbitrary one; today prod has one user).

| Key | Type | Definition |
|---|---|---|
| `at` | str | Display time of `ts`. |
| `ts` | str | Raw `pipeline_events.ts` (`…+00:00`). |
| `stage` | str | One of the stage names the pipelines write: `fetch`, `generate`, `ats_loop`, `judge`, `lang_gate`, `render`, `verdict` (`apply_api`; `apply_cli` writes `fetch`, `generate`, `judge`, `lang_gate`, `verdict` — its `claude -p` subprocess IS the generate stage, ats_loop/render live inside the skill), and `refine` (`verdict_refine.refine_loop`). Not `gates`/`delivery` — those exist only in `STAGE_ORDER` for inference. |
| `event` | str | `start` \| `ok` \| `error` \| `blocked` for a stage; `start` \| `accepted` \| `rejected` \| `discarded` for `refine`. |
| `duration_ms` | int \| null | Raw column (the M1 `stage()` call sites pass none — `null` in prod; the fixture writes 1000). |
| `company` | str | Subquery result or `''` (no `applications` row for that `url_norm` — e.g. a paste-mode run or the fixture's `r_orph`). |
| `pipeline` | str | `generation_runs.pipeline`. |
| `payload` | str | `pipeline_events.payload` TRUNCATED to 80 characters — a display string, not JSON to parse. **Not in the contract** as data; see below. |

### `coverage`

The plan's decision rules, computed by the tool for the bot's own diagnostics.
Verbatim thresholds and verdict strings, so the API port can reproduce them
on an "about this data" tab if it wants to; the page proper does not need
them. Every rule object carries `verdict`: `"PASS"` \| `"FAIL"` \|
`"UNMEASURED"`; UNMEASURED objects carry `why` and (rules 1–3, 5) no
numbers; measured objects carry `consequence_if_fail` (a constant string) and,
where there is a numeric threshold, `threshold` (a constant string).

**`1_run_coverage`** — needs `generation_runs` + `applications.source`; else
`{verdict: "UNMEASURED", why: "generation_runs or applications.source missing"}`.
Produced rows: `SELECT url_norm, source FROM applications WHERE user_id = ?
AND date IN (dates…) AND ats_status NOT IN ('PENDING','IN_PROGRESS') AND
url_norm != ''`. Rows with `source = ''` predate MARKET_MEMORY M3
(2026-09-13) and metrics itself (2026-09-10) — reported as
`excluded_blank_source` (int), not counted. `rows_produced` (int) = distinct
`url_norm` of the rest; `with_generation_run` (int) = `SELECT
COUNT(DISTINCT url_norm) FROM generation_runs WHERE pipeline != 'backfill'
AND url_norm IN (…)`; `share_pct` (float | null) = `_pct(covered, produced)`;
`threshold: ">= 90"`; verdict `UNMEASURED` when `share_pct` is null, `PASS`
when ≥ 90, else `FAIL`; `consequence_if_fail: "M1 must fix metrics wiring
before any page"`.

**`2_stage_resolution`** — needs both metrics tables; else `{verdict:
"UNMEASURED", why: "metrics tables missing"}`. Over `SELECT run_id,
started_at, finished_at FROM generation_runs WHERE started_at >= ? AND
finished_at IS NOT NULL AND pipeline != 'backfill'` (no user predicate): for
each run with parseable `started_at < finished_at`, take the points
`[started_at, every event ts in id order, finished_at]`, the longest gap
between consecutive points as a share of wall time; runs with wall time
≤ 60 s are excluded (a $0 skip has no stage question). `finished_runs_over_1min`
(int), `median_longest_gap_share_pct` (float | null — the lower median,
`sorted(shares)[len // 2]`), `runs_with_gap_over_50pct` (int),
`start_events_seen` / `refine_events_seen` (int — counted over EVERY
qualifying run's events, including sub-minute ones), `threshold: "median <=
50"`; verdict `UNMEASURED` when no shares, `PASS` when median ≤ 50, else
`FAIL`; `consequence_if_fail: "M1 adds start events + one event per refine
round"`.

**`3_hunt_funnel`** — needs `hunt.source_runs` and `hunt.postings_seen` both
non-null with `found_raw > 0`; else `{verdict: "UNMEASURED", why: "no
source_runs/postings_seen rows in window"}`. `found_raw` (int),
`unique_seen` (int) = `passed + rejected`, `gap_pct` (float | null) =
`_pct(found_raw − unique_seen, found_raw)`, `threshold: "<= 30"`; verdict
`PASS` when `gap_pct ≤ 30`, else `FAIL` (also FAIL when null — cannot happen
with `found_raw > 0`); `consequence_if_fail: "M1 adds a hunt_runs table
written from hunter/main.py's own counters"`. Kept as the pre-M1 comparison;
on prod it fails by construction (94% at 7 days) and 3b is the rule the page
builds on.

**`3b_hunt_runs_present`** — `hunt.hunt_runs` null ⇒ `{verdict:
"UNMEASURED", why: <hunt_runs_unmeasured or "hunt_runs table missing">}`.
Otherwise `hunts_in_window`, `found`, `new`, `queued` (ints copied from
`hunt.hunt_runs`), verdict `PASS` when `hunts_in_window > 0`, else
`UNMEASURED` with `why: "table present, no hunt rows in window"`. No
threshold, no consequence key.

**`4_ready_stack`** — always measured (only `applications` needed).
`ready_by_snapshot` (int) = `SELECT COUNT(*) FROM applications WHERE user_id
= ? AND sent = '' AND ats_status NOT IN (NON_APPLIED_STATUSES…) AND
ats_status != ''` with `NON_APPLIED_STATUSES = ("SKIP", "FAIL", "MANUAL",
"EXPIRED", "PENDING", "IN_PROGRESS", "—", "–", "-")`. Then
`tracker.iter_unsent_rows()`'s WHERE verbatim — `ats_status != 'SKIP' AND
ats_status NOT IN ('PENDING','IN_PROGRESS') AND id != '' AND (sent = '' OR
sent IN ('—', '–', '-')) AND user_id = ?` — split into applied rows with a
dash (`declined_by_owner_dash`, int) and the rest
(`unsent_applied_by_tracker_sql`, int). Verdict `PASS` iff
`ready_by_snapshot == unsent_applied_by_tracker_sql`;
`consequence_if_fail: "fix the 'ready' definition before anything else"`.
This is the rule that pinned `result.ready.count` to `sent = ''` only
(M0 on prod: 19 vs 24, the 5 were exactly the dashes).

**`5_leaked_open_runs`** — needs `generation_runs`; else `{verdict:
"UNMEASURED", why: "generation_runs missing"}`. `open_runs` (int) = `COUNT(*)
… WHERE finished_at IS NULL AND pipeline != 'backfill'`;
`older_than_timeout` (int) = the same with `AND started_at < ?`, `?` = `now −
APPLY_AGENT_CLI_TIMEOUT_SEC` as UTC isoformat (string compare);
`orphan_stamped` (int, informational, NOT in the verdict) = `COUNT(*) …
WHERE started_at >= start_utc AND pipeline != 'backfill' AND outcome LIKE
'orphan:%'` — a stamped run is a CLOSED run, which is what the rule wants;
`timeout_sec` (int) = `APPLY_AGENT_CLI_TIMEOUT_SEC` (10800 default — local
config); verdict `PASS` iff `older_than_timeout == 0`;
`consequence_if_fail: "M1 stamps orphan runs from
apply_worker._resolve_outcome"`. No user predicate.

**`growth`** — PRESENT ONLY when `generation_runs` exists (the key is absent,
not null, otherwise): `{pipeline_events_total: int | null,
pipeline_events_last_7d: int | null, generation_runs_total: int}` — the two
event counts are `null` when `pipeline_events` is missing;
`pipeline_events_last_7d` uses `ts >= now − 7 days` regardless of `--days`.
Row-growth check only (`pipeline_events` has no prune).

## The rules a port must reproduce exactly

### `_bucket_status(ats_status)`

Upper-cased, stripped. `PENDING` / `IN_PROGRESS` / `SKIP` / `FAIL` / `MANUAL`
/ `EXPIRED` → themselves; `''` / `—` / `–` / `-` → `(blank)`; anything else →
`APPLIED` (the column holds the ATS score as a string on an applied row —
`"94"`). Used by `entered_tracker.by_status`, every "applied" filter in
`result`, and rule 4's dash split.

### `queue_mode_observed` (three signals, #293)

Listed under `apply` above. The reason it is three signals and not one: on a
quiet prod queue no live row carries `claimed_at` (placeholders are deleted
before the terminal row is written), so the original single check reported
"never used" against a DB that had queued hundreds of jobs; `hunt_runs.queued
> 0` survives forever.

### `_infer_stage(events)` — the current stage of an open run

Evaluated over the run's events in `id` order, `last` = the newest:

1. No events → `{stage: "fetch", basis: "no events yet"}` (between
   `start_run` and the first `stage()` call the run IS on fetch).
2. `last.event == "start"` → `{stage: last.stage, basis: "start event"}`
   (observed, M1).
3. `last.stage == "refine"` and `last.event in ("accepted", "rejected",
   "discarded")` → `{stage: "refine", basis: "refine round <event>"}` — a
   round decision means the loop is STILL running; the plain next-stage
   guess below would say "delivery" for the whole loop.
4. `last.event in ("error", "blocked")` → `{stage: last.stage, basis: "last
   event was <event>"}`.
5. Otherwise (an `ok`, pre-M1 shape) → the stage AFTER `last.stage` in
   `STAGE_ORDER = ["fetch", "gates", "generate", "ats_loop", "judge",
   "lang_gate", "render", "verdict", "refine", "delivery"]`, clamped to
   `"delivery"` at the end, `"?"` for an unknown stage name; `basis:
   "inferred: after '<last.stage>' ok"`. The page shows "probably" on this
   basis.

`stage_started_min_ago` and `refine_progress` are the two M1 companions of
this rule (see the `run` keys).

### Window arithmetic in one place

`Window(days, now)`: `local_now = now.astimezone(Warsaw)`; `start_local =
(local_now − (days−1) d).replace(hour=0, minute=0, second=0, microsecond=0)`;
`start = start_local.astimezone(UTC)`; `dates = [start_local + i d for i in
0..days−1]` as `YYYY-MM-DD`. DST is handled by the zone conversion, so a
window crossing a DST change is 23/25 hours long on that day, as a calendar
day should be.

## Contract test (specified here, NOT created in this PR)

The shared fixture pair the plan calls for:

- `tests/fixtures/pipeline_snapshot/fixture.sql` — INSERT statements only,
  applied on top of an empty DB prepared by `hunter.db.init_db()` plus the
  four lazy DDLs (`postings_seen._ensure_table`, `source_health._ensure_table`,
  `metrics._ensure_tables`, `hunt_runs._ensure_table`), with every timestamp
  fixed relative to the frozen instant **`NOW = 2026-09-22T12:00:00+00:00`**
  (Warsaw 14:00 CEST, calendar day 2026-09-22). It is the test fixture
  `tests/test_pipeline_snapshot_tool.py::fixture_db` with `now` substituted —
  same rows, same ids, same counts, so the existing assertions in that file
  double as a sanity check on the SQL. The bot-side test should also emit a
  `schema.sql` (`sqlite3 .schema` of the prepared DB, checked in) so the API
  repo can build the identical DB with no Python.
- `tests/fixtures/pipeline_snapshot/expected.json` — the tool's `--json`
  output over that DB at `NOW`, `--days 1 --user u1 --events 10`, with the
  failures log pointed at a non-existent file.

The bot-side test that pins them (`build_snapshot(fixture) ==
expected.json` after normalisation) is the **next PR**; it needs a way to
freeze the clock — a `now=` parameter threaded into `build_snapshot`
(`Window` already takes one; `_local_hhmm` and `_minutes_ago` read `now` from
the window or the module) — which is a tool change and therefore not part of
this docs-only PR. The API repo runs the same fixture through the same
assertion; schema drift then fails in both repos, the way the scout payload
fixtures do.

**Normalised before compare** (volatile under a real clock or a local
config; the API port compares everything else byte-for-byte):

- `generated_at`, `window.start_utc` — from the clock.
- every `*_min_ago` (`claimed_min_ago`, `elapsed_min`,
  `stage_started_min_ago`), every `wait_min` / `oldest_wait_min`,
  `llm_outage.remaining_min`, `failures.next_retry.in_min` — from the clock.
- every `at` display string (`hunt_runs.last.at`, `last_event.at`,
  `refine_progress.at`, `events[].at`) — "today" vs "MM-DD" depends on the
  clock.
- `hunt.next_slot` (whole object) — depends on the local source roster and
  schedule config.
- `apply.queue_enabled_local_config`, `failures.next_retry`,
  `coverage.5_leaked_open_runs.timeout_sec` — local config.
- `coverage.5_leaked_open_runs.older_than_timeout` and its `verdict`, and
  `coverage.growth.pipeline_events_last_7d` — both use the clock as a cutoff
  (under the frozen `NOW` they are deterministic: 1 / FAIL / 24).

With the clock frozen at `NOW` (the bot-side test), every entry above except
`next_slot` and the two local-config keys is deterministic and the JSON
below is exact.

### `fixture.sql`

```sql
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);

-- hunt_runs: two hunts in the 1-day window + one three days old (cut by --days 1,
-- kept by --days 7). Totals in-window: found 230, filtered 190, dup 33/2/1,
-- new 4, capped 1, queued 3.
INSERT INTO hunt_runs (ts, "trigger", sources, found, filtered_out, filter_reasons,
    dup_url, dup_ct, dup_cooldown, "new", capped, queued, applied_inline, duration_ms) VALUES
  ('2026-09-22T11:10:00+00:00', 'scheduled', '["justjoin"]', 120, 100,
   '{"location": 60, "level": 40}', 15, 2, 1, 2, 0, 2, 0, 5000),
  ('2026-09-22T11:50:00+00:00', 'manual', '["pracuj", "justjoin"]', 110, 90,
   '{"location": 50, "keyword": 40}', 18, 0, 0, 2, 1, 1, 0, 5000),
  ('2026-09-19T12:00:00+00:00', 'scheduled', '["justjoin"]', 999, 999,
   '{"location": 999}', 0, 0, 0, 0, 0, 0, 0, 5000);

-- source_runs: NOW - 2h
INSERT INTO source_runs (source, ts, yield, ok, error) VALUES
  ('justjoin', '2026-09-22T10:00:00+00:00', 120, 1, ''),
  ('pracuj',   '2026-09-22T10:00:00+00:00', 0,   0, '429'),
  ('justjoin', '2026-09-22T10:00:00+00:00', 110, 1, '');

-- postings_seen: 3 passed + 7 rejected, all first seen NOW
INSERT INTO postings_seen (url_norm, url, source, first_seen, last_seen, seen_count,
    filter_verdict, filter_verdict_last) VALUES
  ('ex.com/0', 'https://ex.com/0', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'passed', 'passed'),
  ('ex.com/1', 'https://ex.com/1', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'passed', 'passed'),
  ('ex.com/2', 'https://ex.com/2', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'passed', 'passed'),
  ('ex.com/3', 'https://ex.com/3', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin'),
  ('ex.com/4', 'https://ex.com/4', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin'),
  ('ex.com/5', 'https://ex.com/5', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin'),
  ('ex.com/6', 'https://ex.com/6', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin'),
  ('ex.com/7', 'https://ex.com/7', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin'),
  ('ex.com/8', 'https://ex.com/8', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin'),
  ('ex.com/9', 'https://ex.com/9', 'justjoin', '2026-09-22T12:00:00+00:00', '2026-09-22T12:00:00+00:00', 1, 'location: Berlin', 'location: Berlin');

-- applications (user u1 unless noted). queued_at / claimed_at use the queue's
-- own '%Y-%m-%dT%H:%M:%SZ' format; date is the local calendar day.
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent, source, queued_at) VALUES
  ('p1', '2026-09-22', 'u1', 'Acme', 'Angular Dev', 'PENDING', 'https://ex.com/p1', 'ex.com/p1', '', 'justjoin', '2026-09-22T11:15:00Z'),
  ('p2', '2026-09-22', 'u1', 'Beta', 'Angular Dev', 'PENDING', 'https://ex.com/p2', 'ex.com/p2', '', 'justjoin', '2026-09-22T11:40:00Z');
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent, source, claimed_at) VALUES
  ('ip', '2026-09-22', 'u1', 'Example Corp', 'Angular Dev', 'IN_PROGRESS', 'https://ex.com/ip', 'ex.com/ip', '', 'justjoin', '2026-09-22T11:46:00Z');
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent, source, ats_verdict, cost_usd) VALUES
  ('a1', '2026-09-22', 'u1', 'Gamma', 'Angular Dev', '94', 'https://ex.com/a1', 'ex.com/a1', '', 'justjoin', 96, 0.31),
  ('a2', '2026-09-22', 'u1', 'Delta', 'Angular Dev', '88', 'https://ex.com/a2', 'ex.com/a2', '', 'justjoin', 90, NULL),
  ('a4', '2026-09-22', 'u1', 'Zeta',  'Angular Dev', '95', 'https://ex.com/a4', 'ex.com/a4', '2026-09-22', 'justjoin', 97, 0.5),
  -- CLI-served run: cost_usd 0.0, not NULL — counts as unpriced
  ('a5', '2026-09-22', 'u1', 'Omega', 'Angular Dev', '92', 'https://ex.com/a5', 'ex.com/a5', '', 'justjoin', 91, 0.0),
  -- owner declined by hand (web-UI "Filter miss" writes a dash): not ready
  ('a6', '2026-09-22', 'u1', 'Psi',   'Angular Dev', '89', 'https://ex.com/a6', 'ex.com/a6', '—', 'justjoin', 80, NULL),
  -- another user's ready row must never leak into u1's stacks
  ('x1', '2026-09-22', 'u2', 'Other', 'Angular Dev', '90', 'https://ex.com/x1', 'ex.com/x1', '', 'justjoin', 50, NULL);
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent, source, skip_reason) VALUES
  ('s1', '2026-09-22', 'u1', 'Theta', 'Angular Dev', 'SKIP', 'https://ex.com/s1', 'ex.com/s1', '—', 'justjoin', 'doomed:pl_onsite');
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent, source) VALUES
  ('e1', '2026-09-22', 'u1', 'Kappa', 'Angular Dev', 'EXPIRED', 'https://ex.com/e1', 'ex.com/e1', 'EXPIRED', 'justjoin');
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent, source, fail_count) VALUES
  ('f1', '2026-09-22', 'u1', 'Lambda', 'Angular Dev', 'FAIL', 'https://ex.com/f1', 'ex.com/f1', '—', 'justjoin', 1),
  ('f2', '2026-09-22', 'u1', 'Mu',     'Angular Dev', 'FAIL', 'https://ex.com/f2', 'ex.com/f2', '—', 'justjoin', 3);

-- generation_runs + pipeline_events. Finished runs are pre-M1-shaped (end-of-stage
-- events only); the in-progress run is M1-shaped (refine start + two rounds).
INSERT INTO generation_runs (run_id, user_id, url_norm, started_at, finished_at, pipeline, outcome, verdict_first, verdict_final, refine_rounds) VALUES
  ('r_ip',   'u1', 'ex.com/ip',   '2026-09-22T11:46:00+00:00', NULL,                        'cli',      NULL,                85, 88, 1),
  ('r_a1',   'u1', 'ex.com/a1',   '2026-09-22T08:40:00+00:00', '2026-09-22T09:10:00+00:00', 'cli',      'ok',                85, 88, 1),
  ('r_a2',   'u1', 'ex.com/a2',   '2026-09-22T08:40:00+00:00', '2026-09-22T09:10:00+00:00', 'cli',      'ok',                85, 88, 1),
  ('r_a4',   'u1', 'ex.com/a4',   '2026-09-22T08:40:00+00:00', '2026-09-22T09:10:00+00:00', 'cli',      'ok',                85, 88, 1),
  ('r_s1',   'u1', 'ex.com/s1',   '2026-09-22T10:20:00+00:00', '2026-09-22T10:20:20+00:00', 'cli',      'skip_doomed_gate',  85, 88, 1),
  ('r_e1',   'u1', 'ex.com/e1',   '2026-09-22T10:20:00+00:00', '2026-09-22T10:20:20+00:00', 'cli',      'expired',           85, 88, 1),
  ('r_f1',   'u1', 'ex.com/f1',   '2026-09-22T10:20:00+00:00', '2026-09-22T10:20:20+00:00', 'cli',      'cli_error',         85, 88, 1),
  -- a backfilled row must NOT count as run coverage
  ('bf_f2',  'u1', 'ex.com/f2',   '2026-09-22T10:20:00+00:00', '2026-09-22T10:20:00+00:00', 'backfill', 'ok',                85, 88, 1),
  -- a leaked open run, older than the CLI timeout (3 h)
  ('r_leak', 'u1', 'ex.com/leak', '2026-09-22T07:00:00+00:00', NULL,                        'cli',      NULL,                85, 88, 1),
  -- parent-stamped orphan: closed (not a leak), under a minute (out of rule 2)
  ('r_orph', 'u1', 'ex.com/orph', '2026-09-22T10:20:00+00:00', '2026-09-22T10:20:30+00:00', 'cli',      'orphan:cli_timeout', 85, 88, 1);

INSERT INTO pipeline_events (run_id, ts, stage, event, duration_ms, payload) VALUES
  ('r_ip', '2026-09-22T11:46:00+00:00', 'fetch',    'ok',       1000, ''),
  ('r_ip', '2026-09-22T11:51:00+00:00', 'generate', 'ok',       1000, ''),
  ('r_ip', '2026-09-22T11:53:00+00:00', 'judge',    'ok',       1000, ''),
  ('r_ip', '2026-09-22T11:56:00+00:00', 'verdict',  'ok',       1000, ''),
  ('r_ip', '2026-09-22T11:57:00+00:00', 'refine',   'start',    1000, '{"target": 95, "max_rounds": 5, "verdict_first": 85}'),
  ('r_ip', '2026-09-22T11:58:00+00:00', 'refine',   'rejected', 1000, '{"round": 1, "kind": "honest", "score": 84, "best": 85}'),
  ('r_ip', '2026-09-22T11:59:00+00:00', 'refine',   'accepted', 1000, '{"round": 2, "kind": "honest", "score": 90, "best": 90}'),
  ('r_a1', '2026-09-22T08:40:00+00:00', 'fetch',    'ok', 1000, ''),
  ('r_a1', '2026-09-22T08:45:00+00:00', 'generate', 'ok', 1000, ''),
  ('r_a1', '2026-09-22T08:47:00+00:00', 'judge',    'ok', 1000, ''),
  ('r_a1', '2026-09-22T08:50:00+00:00', 'verdict',  'ok', 1000, ''),
  ('r_a2', '2026-09-22T08:40:00+00:00', 'fetch',    'ok', 1000, ''),
  ('r_a2', '2026-09-22T08:45:00+00:00', 'generate', 'ok', 1000, ''),
  ('r_a2', '2026-09-22T08:47:00+00:00', 'judge',    'ok', 1000, ''),
  ('r_a2', '2026-09-22T08:50:00+00:00', 'verdict',  'ok', 1000, ''),
  ('r_a4', '2026-09-22T08:40:00+00:00', 'fetch',    'ok', 1000, ''),
  ('r_a4', '2026-09-22T08:45:00+00:00', 'generate', 'ok', 1000, ''),
  ('r_a4', '2026-09-22T08:47:00+00:00', 'judge',    'ok', 1000, ''),
  ('r_a4', '2026-09-22T08:50:00+00:00', 'verdict',  'ok', 1000, ''),
  ('r_s1',   '2026-09-22T10:20:00+00:00', 'fetch', 'ok', 1000, ''),
  ('r_e1',   '2026-09-22T10:20:00+00:00', 'fetch', 'ok', 1000, ''),
  ('r_f1',   '2026-09-22T10:20:00+00:00', 'fetch', 'ok', 1000, ''),
  ('r_leak', '2026-09-22T07:00:00+00:00', 'fetch', 'ok', 1000, ''),
  ('r_orph', '2026-09-22T10:20:00+00:00', 'fetch', 'ok', 1000, '');

-- LLM outage pause armed until NOW + 30 min (epoch seconds of 2026-09-22T12:30:00Z)
INSERT INTO config (key, value) VALUES ('llm_outage_until', '1790080200');
```

### `expected.json`

Produced 2026-09-22 by running the tool over exactly that DB with the clock
frozen at `NOW` (`--days 1 --user u1 --events 10`, no failures log). It
reproduces every count `tests/test_pipeline_snapshot_tool.py` asserts.
`next_slot` and `queue_enabled_local_config` show the values of the machine
it was generated on (25 registered sources, `APPLY_QUEUE_ENABLED=false`) and
are in the normalised list above.

```json
{
  "generated_at": "2026-09-22T12:00:00+00:00",
  "window": {"label": "today", "days": 1, "start_utc": "2026-09-21T22:00:00+00:00", "tz": "Europe/Warsaw"},
  "user_id": "u1",
  "hunt": {
    "window": "today",
    "hunt_runs": {
      "hunts": 2, "found": 230, "filtered_out": 190, "dup_url": 33, "dup_ct": 2, "dup_cooldown": 1,
      "new": 4, "capped": 1, "queued": 3, "applied_inline": 0, "duration_ms": 10000,
      "last": {"ts": "2026-09-22T11:50:00+00:00", "at": "13:50", "trigger": "manual",
               "sources": ["pracuj", "justjoin"], "found": 110, "new": 2},
      "by_trigger": {"scheduled": 1, "manual": 1},
      "top_filter_reasons": [["location", 110], ["level", 40], ["keyword", 40]]
    },
    "hunt_runs_unmeasured": null,
    "source_runs": {
      "runs": 3, "found_raw": 230, "sources_ran": 2, "sources_last_run_ok": 1, "errors": 1,
      "per_source": {
        "justjoin": {"runs": 2, "found": 230, "errors": 0, "last_ok": true},
        "pracuj": {"runs": 1, "found": 0, "errors": 1, "last_ok": false}
      }
    },
    "postings_seen": {"unique_seen": 10, "new_this_window": 10, "passed": 3, "rejected": 7,
                      "top_reasons": [["location", 7]]},
    "entered_tracker": {
      "rows": 12,
      "by_status": {"APPLIED": 5, "EXPIRED": 1, "FAIL": 2, "IN_PROGRESS": 1, "PENDING": 2, "SKIP": 1},
      "by_source": [["justjoin", 12]]
    },
    "next_slot": {"at": "14:20", "in_min": 20, "source": "justremote", "sources_total": 25}
  },
  "apply": {
    "queue_mode_observed": true,
    "queue_enabled_local_config": false,
    "pending": {
      "count": 2, "oldest_date": "2026-09-22", "oldest_wait_min": 45,
      "head": [
        {"company": "Acme", "title": "Angular Dev", "source": "justjoin", "wait_min": 45},
        {"company": "Beta", "title": "Angular Dev", "source": "justjoin", "wait_min": 20}
      ]
    },
    "in_progress": {
      "count": 1,
      "cards": [{
        "company": "Example Corp", "title": "Angular Dev", "source": "justjoin", "claimed_by": "",
        "claimed_min_ago": 14, "stale": false,
        "run": {
          "run_id": "r_ip", "pipeline": "cli", "profile": "", "elapsed_min": 14, "events": 7,
          "last_event": {"stage": "refine", "event": "accepted", "at": "13:59"},
          "current_stage": {"stage": "refine", "basis": "refine round accepted"},
          "stage_started_min_ago": 3,
          "refine_progress": {"round": 2, "kind": "honest", "score": 90, "best": 90,
                              "outcome": "accepted", "at": "13:59"},
          "verdict_first": 85.0, "verdict_final": 88.0, "refine_rounds": 1, "refine_accepted": null
        }
      }]
    },
    "runs": {
      "started": 9,
      "outcomes": [["ok", 3], ["(open)", 2], ["skip_doomed_gate", 1], ["expired", 1],
                   ["cli_error", 1], ["orphan:cli_timeout", 1]],
      "cut_zero_cost": {"expired": 1, "skip_doomed_gate": 1},
      "cut_zero_cost_total": 2
    },
    "skipped_rows": {"count": 2, "by_reason": [["EXPIRED", 1], ["doomed", 1]]},
    "failures": {"in_window": 2, "retryable_total": 1, "gave_up_total": 1,
                 "next_retry": {"at": "02:45", "in_min": 765}, "log_records": null},
    "llm_outage": {"paused": true, "remaining_min": 30}
  },
  "result": {
    "ready": {"count": 3, "produced_in_window": 5, "mean_verdict": 92.3},
    "sent_in_window": 1,
    "outcomes_in_window": [],
    "cost": {"total_usd": 0.81, "priced_rows": 2, "unpriced_rows": 3, "per_priced_row_usd": 0.41}
  },
  "events": [
    {"at": "13:59", "ts": "2026-09-22T11:59:00+00:00", "stage": "refine", "event": "accepted", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": "{\"round\": 2, \"kind\": \"honest\", \"score\": 90, \"best\": 90}"},
    {"at": "13:58", "ts": "2026-09-22T11:58:00+00:00", "stage": "refine", "event": "rejected", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": "{\"round\": 1, \"kind\": \"honest\", \"score\": 84, \"best\": 85}"},
    {"at": "13:57", "ts": "2026-09-22T11:57:00+00:00", "stage": "refine", "event": "start", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": "{\"target\": 95, \"max_rounds\": 5, \"verdict_first\": 85}"},
    {"at": "13:56", "ts": "2026-09-22T11:56:00+00:00", "stage": "verdict", "event": "ok", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": ""},
    {"at": "13:53", "ts": "2026-09-22T11:53:00+00:00", "stage": "judge", "event": "ok", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": ""},
    {"at": "13:51", "ts": "2026-09-22T11:51:00+00:00", "stage": "generate", "event": "ok", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": ""},
    {"at": "13:46", "ts": "2026-09-22T11:46:00+00:00", "stage": "fetch", "event": "ok", "duration_ms": 1000, "company": "Example Corp", "pipeline": "cli", "payload": ""},
    {"at": "12:20", "ts": "2026-09-22T10:20:00+00:00", "stage": "fetch", "event": "ok", "duration_ms": 1000, "company": "", "pipeline": "cli", "payload": ""},
    {"at": "12:20", "ts": "2026-09-22T10:20:00+00:00", "stage": "fetch", "event": "ok", "duration_ms": 1000, "company": "Lambda", "pipeline": "cli", "payload": ""},
    {"at": "12:20", "ts": "2026-09-22T10:20:00+00:00", "stage": "fetch", "event": "ok", "duration_ms": 1000, "company": "Kappa", "pipeline": "cli", "payload": ""}
  ],
  "coverage": {
    "1_run_coverage": {"rows_produced": 9, "excluded_blank_source": 0, "with_generation_run": 6, "share_pct": 66.7,
                       "threshold": ">= 90", "verdict": "FAIL",
                       "consequence_if_fail": "M1 must fix metrics wiring before any page"},
    "2_stage_resolution": {"finished_runs_over_1min": 3, "median_longest_gap_share_pct": 66.7, "runs_with_gap_over_50pct": 3,
                           "start_events_seen": 0, "refine_events_seen": 0, "threshold": "median <= 50", "verdict": "FAIL",
                           "consequence_if_fail": "M1 adds start events + one event per refine round"},
    "3_hunt_funnel": {"found_raw": 230, "unique_seen": 10, "gap_pct": 95.7, "threshold": "<= 30", "verdict": "FAIL",
                      "consequence_if_fail": "M1 adds a hunt_runs table written from hunter/main.py's own counters"},
    "3b_hunt_runs_present": {"hunts_in_window": 2, "found": 230, "new": 4, "queued": 3, "verdict": "PASS"},
    "4_ready_stack": {"ready_by_snapshot": 3, "unsent_applied_by_tracker_sql": 3, "declined_by_owner_dash": 1, "verdict": "PASS",
                      "consequence_if_fail": "fix the 'ready' definition before anything else"},
    "5_leaked_open_runs": {"open_runs": 2, "older_than_timeout": 1, "orphan_stamped": 1, "timeout_sec": 10800, "verdict": "FAIL",
                          "consequence_if_fail": "M1 stamps orphan runs from apply_worker._resolve_outcome"},
    "growth": {"pipeline_events_total": 24, "pipeline_events_last_7d": 24, "generation_runs_total": 10}
  }
}
```

(The tool prints `indent=2`, one scalar per line; the block above is the same
object reflowed for reading — compare parsed JSON, not text. Note the events
footer's tie order for equal `ts`: `id DESC`, so the last-inserted row —
`r_orph`, `company: ""` — sorts first among the four `10:20:00` rows.)

## Not in the contract

Keys the tool emits that the API does NOT port and the page does NOT depend
on. They stay in the tool's JSON (a diagnostic for the bot repo), and a
future contract bump may promote one; until then the API leaves them out or
returns `null`.

- **`hunt.next_slot`** — computed from the local scheduler roster
  (`hunter.sources.ALL_SOURCES` filtered by the `*_ENABLED` toggles) and the
  schedule env (`SCHEDULE_TIMES`, `SCHEDULE_SOURCE_OFFSET_MIN`,
  `SCHEDULE_BLACKOUT`) through `hunter.schedules.grid.fire_minute`. The API
  has none of that and computes nothing here; the bot could expose the next
  slot later (a `config` KV row written by the scheduler, or a `hunt_runs`
  "next" column) — a separate change.
- **`events[].payload`** — free-form JSON, truncated to 80 characters in the
  footer. Only these payload shapes are stable enough to rely on, and only
  when read from the FULL `pipeline_events.payload` column, not the
  truncated footer string: `{"score": …}` (`ats_loop`/`verdict` ok),
  `{"chars": …}` (`fetch` ok), `{"error": "<first 200 chars>"}` (`error`),
  the refine round `{round, kind, score, best, reason}` and the refine
  `start` `{target, max_rounds, verdict_first}`. Everything else is
  telemetry the writer may change.
- **`coverage`** (the whole object) — the plan's M0 decision rules and the
  `growth` counters, a bot-side diagnostic. The page shows it on an "about
  this data" tab at most; the API may port it verbatim (the rules are fully
  specified above so a port is mechanical) or omit it.
- **Anything read from the local `.env` / config rather than the DB**:
  `apply.queue_enabled_local_config` (`APPLY_QUEUE_ENABLED` — the DATA-based
  `queue_mode_observed` is the contract key), `apply.failures.next_retry`
  (`RETRY_FAILED_TIMES`), `coverage.5_leaked_open_runs.timeout_sec`
  (`APPLY_AGENT_CLI_TIMEOUT_SEC`). Thresholds the API needs for keys that ARE
  in the contract are pinned here as constants: `cards[].stale` uses 60
  minutes (`APPLY_CLAIM_TIMEOUT_MIN` default), `failures.retryable_total` /
  `gave_up_total` use 3 (`tracker.MAX_FAIL_RETRIES`, a code constant).
- **Display strings** — every `at` field and `hunt.window` (a duplicate of
  `window.label`). Format from the raw `ts`/`window` instead.
- **`apply.failures.log_records`** stays IN the contract but with the caveat
  that it reads a file, not the DB: an API process without `logs/`
  mounted returns `null`, which is a valid value of the key.

## What this document does NOT cover

- The API endpoint shape beyond "the snapshot object above under
  `GET /pipeline/snapshot?days=1|7`" — auth, caching, polling interval
  (10–15 s per the plan) are the API repo's own decisions.
- The page (M3), including which stacks expand into row lists — the plan
  says the API "returns ids"; no `ids` key exists in the snapshot today, so
  that is a contract ADDITION when M3 needs it, not something to infer from
  the counts.
- A `blocked` key for the `BLOCKED` queue (docs/APPLY_FAILURE_QUEUES_PLAN.md
  M3, closed by its own M0 result) and a shadow-run card — both listed under
  the plan's "Later".
- Moving the query layer into `hunter/pipeline_snapshot.py` — the plan's M1
  mentions it; it has not happened and this contract does not need it.
