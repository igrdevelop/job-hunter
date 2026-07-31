# Prompt: Hunt / Apply Split Implementation

You are implementing the hunt/apply split for the Job Hunter Bot project.
The full plan is in `docs/HUNT_APPLY_SPLIT_PLAN.md` — read it first.

## Context

This is a Python 3.11 Telegram bot that scrapes job boards, filters vacancies,
and auto-generates tailored CVs via LLM. The problem: the hunt (finding jobs)
and apply (generating CVs) run under the same asyncio lock — a long apply batch
blocks all hunts for hours. We're splitting them into independent loops with a
DB-backed queue.

## What to implement

Three milestones, in order. Each milestone = one or more commits.
**Do NOT implement M2 (parallel workers) — it is deferred.**

### Step 1: M3 — CLI timeout is not a FAIL

Small, self-contained. One commit.

**Files to change:**
- `hunter/services/apply_service.py` — new outcome `"cli_timeout"`. On
  `asyncio.TimeoutError`, check if `_effective_timeout` widened the timeout
  (CLI mode). If yes, return `"cli_timeout"` instead of `"fail"`.
- `ApplyOutcome` type literal — add `"cli_timeout"`.
- `hunter/main.py` (`_auto_apply_all`) — on `"cli_timeout"`: do NOT call
  `add_failed(job)`. Leave no tracker row (job returns on next hunt).
  Send Telegram message about CLI timeout.
- `hunter/bot/apply_runner.py` — same treatment as `llm_outage`: no FAIL,
  notify Telegram, the URL can be re-sent.
- Tests for the new outcome.

### Step 2: M4 — Fail audit log

Small, self-contained. One commit.

**Files to change:**
- `hunter/services/apply_service.py` — after determining outcome, write a
  JSON line to `logs/apply_failures.jsonl` for every non-ok, non-manual
  outcome. Schema: `{ts, url, company, title, outcome, exit_code, error,
  duration_sec, cli_mode}`. Use `RotatingFileHandler` (5 MB x 5 backups).
- `hunter/commands/` — new `/fails [N]` command: read last N entries from
  the JSONL file, format for Telegram.
- Register the command in `hunter/telegram_bot.py` (or wherever handlers
  are registered).
- Tests.

### Step 3: M1 — PENDING status + hunt/apply split

The main change. Multiple commits recommended (DB first, then tracker
functions, then the worker, then wiring).

#### Commit 3a: DB migration + tracker functions

**`hunter/db.py`:**
- Add `claimed_at TEXT` column via `_migrate_columns`
- Add `pending_meta TEXT` column via `_migrate_columns`

**`hunter/tracker.py`:**
- `add_pending(job: Job) -> str` — INSERT with `ats_status='PENDING'`.
  Store the full Job data (source, location, salary, raw dict with
  permalink/post_text) as JSON in `pending_meta`. Returns row ID.
- `claim_pending() -> dict | None` — atomic SQL: UPDATE first PENDING row
  to IN_PROGRESS + set `claimed_at`, RETURNING all columns. Returns None
  when queue is empty.
- `release_claim(url: str)` — IN_PROGRESS back to PENDING (for cli_timeout,
  llm_outage).
- `reset_stale_claims(timeout_min: int) -> int` — IN_PROGRESS -> PENDING
  for rows where `claimed_at` older than timeout. Returns count.
- Update `is_known()` to return True for PENDING and IN_PROGRESS rows.
- Update `_COOLDOWN_SKIP_STATUSES` to include PENDING, IN_PROGRESS.

Tests for all new functions.

#### Commit 3b: Apply worker

**New file `hunter/apply_worker.py`:**
- `async def apply_worker_loop(context, worker_id=0):` — infinite loop:
  1. Check `llm_outage.pause_remaining()` — if paused, sleep until expiry
  2. `claim_pending()` — if None, sleep 15 seconds, continue
  3. Reconstruct `Job` object from the claimed row + `pending_meta`
  4. Send Telegram notification (processing started)
  5. Run `_run_apply_agent(job)` (reuse existing subprocess wrapper)
  6. Handle outcome:
     - `"ok"`: call existing `record_successful_apply` flow + `deliver_now`
     - `"fail"`: call `add_failed` (DELETE the PENDING/IN_PROGRESS row first,
       then insert FAIL — or just UPDATE ats_status to FAIL)
     - `"cli_timeout"`: `release_claim(url)` + Telegram notify
     - `"llm_outage"`: `release_claim(url)` + arm pause + Telegram notify + break
     - `"manual"`: handle as today
     - `"rate_limited"`: UPDATE to FAIL (existing behavior)
  7. Sleep `APPLY_DELAY_SEC`
- The loop does NOT acquire `_hunt_lock`.
- Wrap the whole loop body in try/except so one crash doesn't kill the worker.

Tests: mock `claim_pending` to return a fake row, verify outcomes.

#### Commit 3c: Wire it together

**`hunter/main.py`:**
- When `AUTO_APPLY` and `APPLY_QUEUE_ENABLED`:
  - Replace `_auto_apply_all(context, capped)` with a loop that calls
    `add_pending(job)` for each job + sends Telegram cards
  - Remove `MAX_JOBS_PER_RUN` cap (all found jobs go to PENDING)
- When `APPLY_QUEUE_ENABLED` is False: keep existing behavior unchanged
  (feature gate for safe rollout).

**`hunter/config.py`:**
- `APPLY_QUEUE_ENABLED: bool` (env var, default `false`)
- `APPLY_CLAIM_TIMEOUT_MIN: int` (default `60` — stale claim reset)

**`hunter/schedules/__init__.py`:**
- Start `apply_worker_loop` as a long-running `asyncio.Task` during
  `register()` (only when `APPLY_QUEUE_ENABLED`)
- Add periodic stale-claim sweep (every 15 min) via `run_repeating`

**`hunter/commands/status.py`:**
- Show PENDING count + IN_PROGRESS count when queue is enabled

**`hunter/commands/queue.py` (new):**
- `/queue` — list PENDING jobs with position number

Register `/queue` handler.

Tests: integration test that hunt writes PENDING, worker picks it up.

## Rules

1. Read `CLAUDE.md` fully — it has repo conventions, commit rules, test
   requirements, protected files.
2. One commit per logical unit. Run `ruff check . && ruff format .` and
   `pytest tests/` before each commit. Fix any failures before committing.
3. Update `CLAUDE.md` in the same commit when you change config, bot
   behavior, tracker schema, or add files.
4. Commit messages in English only. No Co-Authored-By lines.
5. Branch from current `origin/master`: `git fetch origin && git checkout
   -b feat/hunt-apply-split origin/master`.
6. After all commits, open a PR via `/pr`.
7. Do NOT implement M2 (parallel workers). Do NOT implement M0 (measurement).
   Only M3, M4, M1.
8. The manual paste flow (`bot/apply_runner._run_apply_agent`) stays
   immediate — it does NOT go through the PENDING queue.
9. `/force` stays immediate — it does NOT go through the queue.
10. The feature gate `APPLY_QUEUE_ENABLED=false` means the old behavior is
    the default. The new queue path activates only when explicitly enabled.
