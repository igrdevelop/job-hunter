# Hunt drill-down on /pipeline — plan

Status: M0 done, M1 + M2 implemented (2026-09-27, branch `feat/hunt-drilldown`); M3 (API) and M4 (site) next.

## Why

The site's `/pipeline` page shows the hunt only as window totals, plus one
"Last hunt…" line. The owner asked (2026-09-27) for **every hunt as a row in a
table**. Clicking a row opens that hunt's own pipeline in place:

- raw finds per source → filtered out (by reason) → duplicates → new →
  queued / capped;
- then **each vacancy that passed the filter**, with where it is now: still in
  the queue (#N), generating (current stage), ready, SKIP/FAIL/EXPIRED, or a
  duplicate (and of what).

Nothing links a hunt to its vacancies today:

- `hunt_runs` holds counts only, keyed by an integer id, and never receives the
  hunt's `hunt_live.hunt_id`.
- The dedup reasons are integer counters in `hunter/main.py::_run_hunt_impl`.
- The PENDING placeholder in `applications` is deleted and re-inserted on every
  terminal write (`tracker._clear_own_placeholder`), so a `hunt_id` stamped on
  it would not survive.

The key that is stable across the whole lifecycle is `url_norm`, present in
`applications`, `generation_runs` and the new `hunt_jobs`.

Checked before writing: `docs/AGENT_LOG.md` has no rejected version of a per-hunt
list. `PIPELINE_VIZ_PLAN.md` and `PIPELINE_SNAPSHOT_CONTRACT.md` do not mention
one; it is a contract **addition**, not a reversal.

## Owner decisions (2026-09-27)

- **Per-vacancy rows only past the filter.** Filtered-out vacancies stay a
  count by reason; that is already `hunt_runs.filter_reasons`.
- **Inline expand.** A row opens under itself and refreshes every 3 s while the
  hunt, or any of its vacancies, is still moving.

## Non-goals

- No change to how a hunt or an apply executes, and no new message. The
  existing Telegram report is untouched.
- No `hunt_id` threaded through the apply subprocess or the tracker. A
  vacancy's fate is joined at **read time** by `url_norm`.
  - Trade-off: if a later hunt or a manual paste re-applies the same URL, the
    row shows the vacancy's CURRENT fate. That is the question the owner asks
    ("what happened to it"), so this is accepted.
- No per-vacancy rows for filtered-out listings. That is roughly 10× the rows
  for information `postings_seen` already keeps per `url_norm`.

## M0 — measure the volume (done)

Free and read-only, on prod (`mode=ro`), over the last 7 days of `hunt_runs`:

| hunts | avg passed filter | max passed filter | avg dup_url | avg dup_ct | avg new | avg found |
|---|---|---|---|---|---|---|
| 513 | 6.9 | 43 | 4.0 | 2.8 | 0.1 | 68.9 |

**Decision rule** (stated before the run): if average passed-filter ≤ 150 per
hunt, store every post-filter row; otherwise store `dup_url` as a count only.

**Result: 6.9, so store every row.** That is ≈ 500 rows/day at ~73 hunts/day.
Retention: `HUNT_JOBS_TTL_DAYS`, default 30 days (owner decision 2026-09-27),
so ≈ 15k rows. The `hunt_runs` counts keep their own ring; an older hunt still
lists with its funnel, just without per-vacancy rows.

## M1 — bot records the per-hunt data

- **`hunt_runs`** gains two columns via a lazy `ALTER` in its own
  `_ensure_table`:
  - `hunt_id TEXT` (the `hunt_live` uuid, indexed);
  - `per_source TEXT` (JSON `{source: raw count or "ERR"}`, the loop's own
    `fetch_stats`).
- **New `hunter/hunt_jobs.py`** (lazy DDL, no `user_id`):
  - One row per filter-passed vacancy of a hunt: `hunt_id, url_norm, url,
    source, title, company, fate, fate_detail, ts`.
  - The `fate` values:

    | fate | meaning |
    |---|---|
    | `dup_url` | duplicate of a known URL |
    | `dup_ct` | duplicate by company + title |
    | `dup_cooldown` | company is in cooldown |
    | `new` | new, and nothing acted on it (outage pause / apply not ready) |
    | `card` | Telegram Apply/Skip card (manual-only source or AUTO_APPLY off) |
    | `capped` | cut by `MAX_JOBS_PER_RUN` |
    | `queued` | PENDING row written |
    | `applied_inline` | handed to the inline batch |

  - Invariant, pinned by tests: `count(fate IN dup_*)` equals the matching
    `hunt_runs` counter, and `count(fate NOT IN dup_*)` = `hunt_runs.new`.
  - `record_hunt_jobs` raises; the loop wraps it in `best_effort("hunt.jobs")`.
  - It prunes rows whose `hunt_id` is no longer in `hunt_runs`.
  - It is gated by `HUNT_JOBS_ENABLED`.
- **`hunter/main.py`** fills a fate map as the dedup loop and the ACT step
  decide, and flushes it with the `hunt_runs` row in the same idempotent
  `_flush_hunt_run`.

## M2 — contract: list + detail

`tools/pipeline_snapshot.py` stays the contract (read-only, `mode=ro`, a missing
table or column gives `null`, never 0). It gains two functions:

- `hunts_list(conn, user_id, limit)`: the newest `hunt_live` rows, including
  waiting, running and retry passes.
  - Left-joined to `hunt_runs` by `hunt_id` for the counts.
  - Carries a per-hunt vacancy summary from `hunt_jobs` ⋈ `applications`.
- `hunt_detail(conn, hunt_id, user_id)`: the header plus `jobs[]`.
  - The header is the live row + counts + `per_source` + `filter_reasons`.
  - Each job carries the vacancy's current tracker status (queue position for
    PENDING) and its newest `generation_runs` row with the current stage.

Both functions are documented in `PIPELINE_SNAPSHOT_CONTRACT.md` with a fixture
pair.

## M3 — API port, M4 — site table

- **API:** `GET /pipeline/hunts?limit=` and `GET /pipeline/hunts/:huntId` in
  job-hunter-api, both ported from the tool functions.
- **Site:** a hunts table under the control bar on `/pipeline`, with an inline
  detail row. See the site repo.
