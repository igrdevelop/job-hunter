# Hunt / Apply Split Plan

Owner request 2026-07-30: hunt (vacancy finding) and apply (vacancy processing)
should be independent loops. Currently `_auto_apply_all` runs inside the hunt lock
— a 10-job CLI batch can hold `_hunt_lock` for hours, blocking every other hunt.
The owner also wants support for N parallel apply workers.

Related: docs/HUNT_QUEUE_AND_DELIVERY_PLAN.md (P1 — the FIFO wait that replaced
the old skip-on-busy policy). This plan supersedes the FIFO approach with a clean
architectural separation.

---

## Problem statement

### P1 — hunt and apply are coupled through `_hunt_lock`

```
async with _hunt_lock:          # held for the ENTIRE duration
    _run_hunt_impl()            #   fetch + filter + dedup   (~2-5 min)
    _auto_apply_all(jobs)       #   N × (3-45 min + 30s)    (can be HOURS)
```

A hunt slot that fires while a batch is applying **waits** (FIFO), but the next
hunt is 40 min away — if the current batch takes 2+ hours, several hunts queue
up and run back-to-back long after their source's freshness window.

With the CLI fallback (M4/M4b), one vacancy can take 30-45 min. A 10-job batch
is 5-7 hours under the lock. Meanwhile LinkedIn's 24h search window closes and
new vacancies are missed.

### P2 — vacancies without a tracker row are invisible

Currently a new vacancy exists only as a `Job` object in memory during
`_auto_apply_all`. If the batch is interrupted (outage, 3 consecutive fails,
bot restart), unapplied jobs vanish — they return only if the source still
lists them on the next hunt cycle. Short-lived listings (< 24h) can be lost.

### P3 — no parallelism in apply

Jobs are processed strictly sequentially. Two independent vacancies (different
companies, different LLM calls, different folders) could run concurrently, but
the architecture forbids it.

### P4 — CLI timeout creates a false FAIL

`apply_service.py:145-149`: a CLI timeout (`asyncio.TimeoutError`) returns
`"fail"` → `add_failed(job)` → FAIL row in tracker with escalating
`fail_count`. But a CLI timeout is infrastructure, not the vacancy's fault.

---

## Design

### Core idea: DB-backed queue with PENDING status

```
Hunter loop                          Apply worker(s)
─────────────                        ───────────────
fetch → filter → dedup               poll PENDING from DB
  │                                     │
  └─► INSERT … ats_status='PENDING'     ├─► UPDATE … ats_status='IN_PROGRESS'
      + Telegram notification           │     run apply_agent subprocess
      + release _hunt_lock              │     UPDATE … ats_status (OK/FAIL/SKIP)
      (seconds, not hours)              │     deliver (Sheets/Drive)
                                        └─► next PENDING
```

### New `ats_status` values

| Status | Meaning | Written by |
|--------|---------|------------|
| `PENDING` | Found, awaiting apply | hunt loop |
| `IN_PROGRESS` | Claimed by a worker | apply worker |
| (existing) | `SKIP`, `FAIL`, `MANUAL`, `EXPIRED`, score | apply/tracker |

`PENDING` and `IN_PROGRESS` rows are excluded from dedup (a PENDING job must
not be re-added) but excluded from unsent counts (no CV yet).

### Atomic claim (N-worker safe)

```sql
UPDATE applications
SET ats_status = 'IN_PROGRESS',
    claimed_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')
WHERE id = (
    SELECT id FROM applications
    WHERE ats_status = 'PENDING'
    ORDER BY id
    LIMIT 1
)
RETURNING *
```

SQLite serializes writes — two workers cannot claim the same row. The
`ORDER BY id` gives FIFO. `claimed_at` enables stale-claim detection (a
worker that crashes leaves an IN_PROGRESS row; a periodic sweep resets
claims older than `APPLY_CLAIM_TIMEOUT_MIN`).

### Worker count

New config: `APPLY_WORKERS` (default `1`). Each worker is an `asyncio.Task`
in the same bot process — no separate microservice, no IPC, same event loop,
same `tracker.db`. Workers share nothing except the DB (each claim is atomic)
and the Telegram bot context (for notifications).

Practical ceiling: 2-3 workers. Beyond that, LLM rate limits (Anthropic
429), LibreOffice contention, and Telegram rate limits become the bottleneck.

### LibreOffice isolation

Multiple `soffice --headless` invocations with the same user profile corrupt
each other. Each worker uses a unique profile:

```python
env = {**os.environ, "UserInstallation": f"file:///tmp/lo_worker_{worker_id}"}
```

Passed through `apply_service.run_apply_agent_subprocess` → the subprocess's
`generate_docs.py` inherits it.

---

## Milestones

### M0 — Measurement (free, read-only)

**What:** measure actual hunt-lock hold times and apply durations from the
production log to confirm the problem is worth the complexity.

```bash
# On the deploy host:
docker compose exec job-hunter python -c "
import re, statistics
from pathlib import Path
# Parse [Hunt] and [auto-apply] log entries for timing
log = Path('logs/hunter_errors.log').read_text()
# ... extract durations
"
```

**Decision rule:** if the median hunt-lock hold (including apply) is under
10 min and the p95 is under 30 min, defer this plan — the FIFO approach is
good enough. If the p95 exceeds 40 min (the slot spacing), proceed.

### M1 — PENDING status + hunt decoupling

**Scope:** hunt writes PENDING rows instead of calling `_auto_apply_all`.
A single apply worker loop (N=1) consumes them. No parallelism yet.

1. **DB migration** (`hunter/db.py`):
   - New column `claimed_at TEXT` on `applications` table (ALTER TABLE)
   - Update `_migrate_columns` to add `claimed_at` if missing

2. **New `tracker.py` functions:**
   - `add_pending(job: Job) -> str` — INSERT with `ats_status='PENDING'`,
     returns row ID. Stores `job.raw` as JSON in a new `pending_meta TEXT`
     column (the worker needs source, location, salary, permalink, post_text
     to reconstruct the Job object)
   - `claim_pending() -> dict | None` — atomic UPDATE + RETURNING
   - `complete_pending(url, new_status, ...)` — UPDATE from IN_PROGRESS to
     final status (reuses existing `add_applied` / `add_failed` logic)
   - `reset_stale_claims(timeout_min)` — IN_PROGRESS → PENDING for rows
     where `claimed_at` is older than timeout

3. **Dedup update:**
   - `is_known()` must return True for PENDING and IN_PROGRESS rows (a job
     in the queue is already "known")
   - `_COOLDOWN_SKIP_STATUSES` adds PENDING, IN_PROGRESS

4. **`hunter/main.py` changes:**
   - `_run_hunt_impl`: after filter+dedup, call `add_pending(job)` for each
     new job (+ send Telegram card), then **return** — no `_auto_apply_all`
   - `_auto_apply_all` removed from the hunt path (kept as internal for
     the apply worker)
   - `_hunt_lock` now protects only fetch+filter+dedup (~seconds)

5. **New `hunter/apply_worker.py`:**
   - `async def apply_worker_loop(context, worker_id=0):`
     - Loop: `claim_pending()` → run apply → `complete_pending()` → deliver
     - On `llm_outage`: arm pause, put row back to PENDING, break
     - On CLI timeout: put row back to PENDING (not FAIL), notify Telegram
     - On regular fail: complete as FAIL (existing behavior)
     - Sleep `APPLY_DELAY_SEC` between jobs
     - Respect `llm_outage.pause_remaining()` — sleep until expiry
   - No `_hunt_lock` — completely independent

6. **`hunter/schedules/__init__.py`:**
   - Start the apply worker as a long-running `asyncio.Task` during
     `register()` (or `_post_init`)
   - Add a periodic stale-claim sweep (every 15 min)

7. **Telegram commands:**
   - `/status` shows PENDING count + IN_PROGRESS count + worker status
   - `/queue` (new) — list PENDING jobs with position

8. **Retry integration:**
   - `_retry_failed` stays on its own schedule but also uses the worker:
     changes FAIL → PENDING (reset `fail_count`), worker picks it up
   - OR: keep retry as-is (it runs rarely, 2×/day, the lock contention
     is negligible)

### M2 — Parallel workers (N > 1)

**Prereq:** M1 stable in production for at least 1 week.

1. **Config:** `APPLY_WORKERS` (default 1, max 4)
2. **Startup:** spawn N `apply_worker_loop(context, worker_id=i)` tasks
3. **LibreOffice isolation:** per-worker `UserInstallation` env var
4. **Telegram:** worker ID in notifications (`[W1]`, `[W2]`)
5. **Drive/Sheets:** already serialized (`_DRIVE_LOCK`, Sheets append is
   atomic per row) — no changes needed
6. **LLM rate limits:** add a shared `asyncio.Semaphore(APPLY_WORKERS)` in
   `llm_client` if 429s become frequent (measure first)
7. **Consecutive-fail logic:** per-worker, not global — one worker hitting
   3 fails doesn't stop the others

### M3 — CLI timeout is not a FAIL

Can ship independently of M1/M2 (small, self-contained).

1. **New outcome:** `"cli_timeout"` in `ApplyOutcome` literal
2. **`apply_service.py`:** on `asyncio.TimeoutError` when
   `_effective_timeout` returned the CLI timeout, return `"cli_timeout"`
   instead of `"fail"`
3. **`main.py` / `apply_worker.py`:** on `"cli_timeout"`:
   - Do NOT call `add_failed(job)`
   - In M0 (pre-split): leave no tracker row (job returns on next hunt)
   - In M1+ (post-split): set row back to PENDING
   - Telegram: "⏰ CLI timed out for {company} — will retry"
4. **`bot/apply_runner.py`:** same treatment as `llm_outage` — no FAIL,
   notify, the URL can be re-sent

### M4 — Fail audit log

Can ship independently. Structured record of every apply failure.

1. **New file:** `logs/apply_failures.jsonl` (one JSON object per line)
2. **Schema:**
   ```json
   {
     "ts": "2026-07-30T14:23:01Z",
     "url": "https://...",
     "company": "Acme",
     "title": "Senior Frontend",
     "outcome": "fail",
     "exit_code": 1,
     "error": "stderr tail...",
     "worker_id": 0,
     "duration_sec": 142,
     "cli_mode": false
   }
   ```
3. **Written by:** `apply_service.py` on every non-ok outcome (fail,
   cli_timeout, rate_limited — not llm_outage which is global, not manual)
4. **Rotation:** same `RotatingFileHandler` pattern as `hunter_errors.log`
   (5 MB × 5 backups)
5. **Telegram command:** `/fails [N]` — last N failures from the log
   (default 5), with company + error + timestamp

---

## What does NOT change

- **tracker.db schema** — only additions (PENDING status, `claimed_at`,
  `pending_meta` columns). Existing rows untouched
- **apply_agent.py pipeline** — still a subprocess, same flags, same
  generate_docs, same judge/verdict/refine. The worker just orchestrates
  when and how it's called
- **Telegram bot** — same process, same event loop. Workers are tasks, not
  processes
- **Google Sheets/Drive** — delivery hooks (`deliver_apply_now`) called by
  the worker after a successful apply, same as today
- **Manual paste / URL message flow** — `bot/apply_runner._run_apply_agent`
  stays as-is (it's interactive, not queued). Could optionally write PENDING
  instead, but not required — the user expects immediate feedback
- **`/force` flow** — same, bypasses the queue
- **Dual-apply shadow** — still runs inside `apply_agent.main()`, the worker
  just waits for it

---

## Migration / rollback

- **M1 is backward-compatible**: existing rows have no `claimed_at` and
  `ats_status` is never PENDING/IN_PROGRESS — they're invisible to the new
  worker and the old hunt path. A rollback to the pre-split code ignores the
  new columns
- **Feature gate:** `APPLY_QUEUE_ENABLED` (default `false` initially).
  When off, the hunt loop calls `_auto_apply_all` exactly as today. When on,
  it writes PENDING rows and the worker loop runs. Flip to `true` after M0
  confirms the problem is real

---

## Execution order

```
M3 (cli_timeout, small)     ── commit 1
M4 (fail audit log, small)  ── commit 2
M1 (hunt/apply split, N=1)  ── commits 3-6 (DB, tracker, worker, wiring)
M0 (measurement, optional)  ── can run on deploy host anytime for diagnostics
M2 (parallel workers)       ── DEFERRED, not in this plan's scope
```

M3 and M4 are self-contained fixes (owner's points 2 and 3).
M1 is the main architectural change. M2 is deferred until M1 proves itself.

---

## Open questions

1. **Manual paste flow — queue or immediate?** Current: the user pastes a
   URL and gets docs in a few minutes. With the queue, it would go through
   PENDING → worker → docs. Pro: unified path. Con: feels slower for an
   interactive user. Recommendation: keep immediate for paste, queue for
   AUTO_APPLY hunts only.

2. **`MAX_JOBS_PER_RUN` — still needed?** With a queue, there's no batch
   to cap. The hunt writes all found jobs to PENDING, the worker processes
   them at its own pace. Could remove the cap entirely or repurpose it as
   a per-hunt write cap (unlikely to matter — most hunts find 0-5 jobs per
   source). Recommendation: remove after M1 stabilizes.

3. **Retry flow — reuse queue or keep separate?** The retry schedule
   (RETRY_FAILED_TIMES, 07:45/18:45) could flip FAIL → PENDING instead of
   running its own apply loop. Pro: one apply path. Con: retry priorities
   (FAIL rows) mix with fresh PENDING rows. Could add a `priority` column.
   Recommendation: keep separate initially, unify in M2 if the queue works
   well.

---

## Decision log

| Date | Decision |
|------|----------|
| 2026-07-30 | Owner: "может быть несколько потоков обработки вакансии?" — parallel workers designed (M2) but DEFERRED until M1 proves itself |
| 2026-07-30 | Owner: CLI timeout should not produce a FAIL row — tracked as M3 |
| 2026-07-30 | Owner: separate fail log needed — tracked as M4 |
| 2026-07-30 | Owner: M0 measurement is optional, not a gate — ship M3/M4/M1 without waiting for prod metrics |
| 2026-07-17 | (prior, docs/LLM_OUTAGE_RESILIENCE_PLAN.md) "No queue microservice" — the listing WAS the queue. This plan replaces that model with a DB-backed queue while keeping the single-process architecture |
