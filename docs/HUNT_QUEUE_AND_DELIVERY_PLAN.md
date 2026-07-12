# Hunt Queue & Instant Delivery Plan

Owner request 2026-07-12: "Hunt skipped я видел много раз и мне это не нравится" +
"обработанная вакансия не сразу появляется на Google Диске и в таблице — я хочу сразу".

Three problems, three milestones. All in branch `fix/hunt-queue-and-instant-delivery`.

---

## Problem statement

### P1 — scheduled hunts are silently LOST when the lock is busy

`hunter/main.py::run_hunt` serializes everything through a global `_hunt_lock`,
and when the lock is held it **skips the run entirely** ("⏭ Hunt skipped — auto-apply
still processing") instead of waiting. Two structural reasons this fires often:

1. **15 exact-minute collisions per day.** Base times 13:00 and 19:00 differ by
   360 min — a multiple of the 40-min per-source offset — so slots 9–23 of the
   13:00 cycle land on the same minute as slots 0–14 of the 19:00 cycle
   (19:00 arbeitnow vs justjoin, 19:40 remotive vs nofluffjobs, … 04:20).
   One of each pair always loses the lock race and is skipped. Every added
   source adds one more collision pair.
2. **Long auto-apply batches swallow neighbor slots.** One hunt that finds a few
   new jobs runs N × (up to 15 min pipeline + 30 s delay) sequentially — easily
   past the 40-min slot spacing — and every slot that fires meanwhile is lost.

A skipped slot is latency, not data loss (the source's next slot is 5–11 h later
and dedup is stable), but the freshness hit is real (LinkedIn has a 24 h search
window) and the Telegram noise is constant.

### P2 — `_retry_failed` piggybacks on every hunt

`_run_hunt_impl` runs `_retry_failed()` after **every** per-source AUTO_APPLY hunt
(72 slots/day). It retries the global FAIL list (up to `MAX_JOBS_PER_RUN` × 15 min
each) — it is the main reason hunts hold the lock longer than 40 min, and it
hammers the same FAIL list dozens of times a day for no benefit.

### P3 — Sheets/Drive delivery is not immediate on every path

The "mirror to Sheets + upload to Drive right after apply" hooks exist, but
coverage is uneven:

| Apply path | Sheets now | Drive now |
|---|---|---|
| AUTO_APPLY hunt loop (`main._auto_apply_all`) | ✅ `_sync_to_sheets` | ✅ `_upload_to_drive` |
| Retry loop (`main._retry_failed`) | ✅ | ✅ |
| Manual card / paste **with** URL (`bot/apply_runner._run_apply_agent`) | ✅ (duplicated inline) | ✅ (duplicated inline) |
| Paste **without** URL | ❌ (`if url:` gate) | ❌ (`get_folder_by_url("")` → None) |
| LinkedIn batch (`bot/apply_runner._run_linkedin_batch`) | ❌ no hooks at all | ❌ no hooks at all |
| Targeted lookup misses (URL normalization edge) | ❌ silent no-op | ❌ silent no-op |

Whatever falls through waits for the safety nets: Sheets — `gsheets_resync`
every 5 min (dirty rows) — tolerable; Drive — `gdrive_upload_missing` every
**3 hours** — this is the "не сразу на диске" the owner sees. Failures of the
immediate hooks are also only logged (best-effort), so a token/quota hiccup
degrades to the same slow backfill invisibly.

---

## Design decisions

- **Serialization stays.** One apply pipeline at a time is the right design
  (smooth LLM spend, readable Telegram, no LibreOffice contention). We change
  the *skip* policy, not the concurrency model. No parallel applies.
- **Wait, don't skip.** `asyncio.Lock` waiters are FIFO; a queued scheduled hunt
  is a small coroutine that runs a quick fetch once the lock frees. Pile-ups
  drain naturally because per-source fetches are seconds when nothing is new.
- **Queue silently for scheduled hunts, notify for manual ones.** The owner
  explicitly dislikes the "Hunt skipped" spam. A scheduled hunt that waits needs
  no message — its normal hunt report arrives when it runs. A human typing
  `/hunt` gets one "⏳ queued" reply so the command doesn't look ignored.
- **Retry gets its own slots at :45.** The hunt grid only ever fires at :00/:20/:40
  (base minutes 00 + multiples of 40), so `07:45` / `18:45` never exact-collide
  with any hunt slot regardless of source count.
- **Instant delivery becomes one shared function** instead of three divergent
  copies, with a **fallback**: when there is no URL (paste) or the targeted
  lookup finds nothing, run the existing idempotent backfills
  (`push_missing_rows` + `upload_missing_folders`) immediately — they deliver
  exactly the rows/folders that are missing, right now, instead of hours later.
- **Safety nets stay but tighten.** `gdrive_upload_missing` 3 h → 30 min
  (config-overridable). It is idempotent (skips rows that already have a Drive
  URL) and cheap when there is nothing to do.

---

## M1 — scheduled hunts queue instead of skipping

**`hunter/main.py`**
- `run_hunt(context, source_names=None, *, notify_queued=False)`:
  - drop the `if _hunt_lock.locked(): … return` skip branch;
  - when the lock is busy: `logger.info("[Hunt] Busy — queued …")`, and send
    "⏳ Hunt queued — will start when the current hunt finishes." **only** when
    `notify_queued=True`;
  - then `async with _hunt_lock: await _run_hunt_impl(…)` — i.e. wait.

**`hunter/commands/hunt.py`** — pass `notify_queued=True` (human is waiting).
`schedules/hunt.py` and `__main__.py --now` keep the silent default.

**Tests** (`tests/test_hunt_queue.py`):
- lock held → `run_hunt` does NOT return early; when released, `_run_hunt_impl`
  runs (patch impl; assert called once).
- lock held + `notify_queued=False` → no Telegram message; `notify_queued=True`
  → exactly one "queued" message, and the impl still runs.
- two concurrent `run_hunt` calls both complete, serially.

## M2 — `_retry_failed` moves to its own schedule

**`hunter/main.py`**
- remove the `await _retry_failed(context)` call from `_run_hunt_impl`;
- new public `run_retry_failed(context)`: no-op unless `AUTO_APPLY`; waits on
  `_hunt_lock` (same queue semantics as M1, log-only); runs `_check_apply_ready`
  then `_retry_failed`. `_retry_failed` itself already returns silently when the
  FAIL list is empty — no new noise.

**`hunter/config.py`** — `RETRY_FAILED_TIMES` (default `"07:45,18:45"`,
comma-separated HH:MM, Warsaw tz like SCHEDULE_TIMES).

**`hunter/schedules/retry_failed.py`** — `scheduled_retry_failed` callback
(error handling mirrors `schedules/hunt.py`); registered in
`schedules/__init__.py::register()` once per configured time
(`retry_failed_0745` job names). Invalid time strings: log warning, skip that
entry, fall back to defaults if none parse.

**Tests** (`tests/test_retry_schedule.py`):
- `_run_hunt_impl` source no longer references `_retry_failed` (wiring guard);
- `run_retry_failed` gates on AUTO_APPLY, runs `_retry_failed` under the lock,
  and reports `_check_apply_ready` errors instead of running;
- `register()` on a fake app creates one daily job per RETRY_FAILED_TIMES entry;
- malformed `RETRY_FAILED_TIMES` entry is skipped with a warning.

## M3 — instant Sheets + Drive delivery on every path

**New `hunter/delivery.py`** — single best-effort entry point, never raises:

```python
async def deliver_apply_now(url: str | None) -> str | None:
    """Mirror the just-applied row to Sheets and upload its folder to Drive NOW.

    Targeted fast path when a URL is known; falls back to the idempotent
    backfills (push_missing_rows / upload_missing_folders) when it isn't, or
    when the targeted lookup misses. Returns the Drive folder URL when the
    targeted upload produced one (callers may show an "Open folder" link).
    """
```

- targeted Sheets: `cache.load_from_db()` → `cache.get_row_by_url(url)` →
  `gsheets_sync.mirror_new_row(row)`;
- targeted Drive: `tracker.get_folder_by_url(url)` →
  `gdrive_sync.upload_application_folder(...)` (gated by `GDRIVE_ENABLED`);
- fallback (no url / row not found): `gsheets_sync.push_missing_rows()`;
  (no url / folder not found): `gdrive_sync.upload_missing_folders(PROJECT_DIR)`;
- every stage in its own try/except + `logger.warning` — a Sheets failure must
  not block the Drive upload and vice versa.

**Callers rewired** (all duplication deleted):
- `hunter/main.py`: `_sync_to_sheets` + `_upload_to_drive` replaced by
  `delivery.deliver_apply_now(job.url)` in `_auto_apply_all` and `_retry_failed`;
- `hunter/bot/apply_runner.py::_run_apply_agent`: inline mirror/upload blocks
  replaced by `drive_url = await deliver_apply_now(url or None)` — the
  paste-without-URL case is now covered by the fallback; keep the
  "📁 Open folder on Drive" notify when a drive_url comes back;
- `hunter/bot/apply_runner.py::_run_linkedin_batch`: call
  `deliver_apply_now(url)` after each successful job (had NO hooks before).

**Safety net tightened**: `GDRIVE_UPLOAD_MISSING_INTERVAL_MIN` config
(default 30, was hardcoded 3 h) used by `schedules/__init__.py::register()`.

**Tests** (`tests/test_delivery.py`):
- url + row found → mirror_new_row called, push_missing_rows NOT called;
- url + row missing → push_missing_rows fallback called;
- url=None → both backfills called, no targeted lookups;
- Sheets stage raising does not prevent the Drive stage;
- GDRIVE_ENABLED=false → no Drive calls, Sheets still delivered;
- `_run_linkedin_batch` calls deliver_apply_now on success (wiring);
- `_run_apply_agent` paste-no-URL path calls deliver_apply_now(None) (wiring);
- register() uses the new interval config.

---

## Out of scope (deliberately)

- Parallel apply pipelines — rejected, see Design decisions.
- Changing the schedule grid / offsets — unnecessary once skips are gone.
- A `/retry` manual command — the scheduled slots + normal hunts cover it;
  add later only if the owner asks.
- Sheets/Drive failure alerts to Telegram — oauth_alert already covers the
  dead-token case; transient failures now self-heal within ≤30 min.

## Rollout / verification

- Full pytest suite green; ruff check + format clean.
- After deploy, "⏭ Hunt skipped" should disappear from the chat entirely;
  the per-apply Drive link message keeps arriving right after each apply, and
  paste-mode applies now get Sheets/Drive delivery within seconds too.
- CLAUDE.md: schedule section, config table (RETRY_FAILED_TIMES,
  GDRIVE_UPLOAD_MISSING_INTERVAL_MIN), pipeline step 8-10 notes, work log.
