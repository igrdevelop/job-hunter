# Google Drive SSL Failures — Concurrent Uploads over One Shared Service

**Status:** PLANNED
**Branch:** `claude/gdrive-ssl-error-77e085` (off `origin/master` @ 7c0492a)
**Author:** opus, 2026-07-29
**Trigger:** owner report — Telegram alert
`⚠️ gdrive.upload_shadow_folder: 3 подряд сбоев, последний: [SSL] record layer failure (_ssl.c:2590)`

Source of truth for the diagnosis: the prod log mirror
`G:/My Drive/Job Hunter/Logs/2026-07-1x…2026-07-29.log`.

---

## 1. Diagnosis (evidence-based)

### 1.1 It is not the network, the token, or Google

The error is a TLS *framing* error (`record layer failure`, `LENGTH_MISMATCH`),
not a handshake, auth or quota error. It appears in bursts, interleaved with
successful uploads on the same second, and always for a *subset* of folders
while their neighbours upload fine. A broken network or a dead token fails
uniformly; this does not.

Occurrences per day (`grep -c "record layer failure"`):

| Day | 07-11 | 07-12 | 07-13 | 07-14 | 07-16 | 07-17 | 07-19 | 07-22 | 07-27 | 07-28 | 07-29 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Count | 0 | 0 | 20 | 75 | 12 | 36 | 18 | 57 | 18 | 28 | 4 |

First occurrence: **2026-07-13 05:45:59**. Continuous since.

### 1.2 Two concurrent `upload_missing_folders()` runs

Every burst is immediately preceded by a `delivery … falling back` line, and
overlaps a scheduled `gdrive_upload_missing` slot. 2026-07-27:

```
05:45:01  [delivery] no tracker row found … — falling back
05:45:02  [delivery] no folder recorded … — falling back        ← run #1 (delivery fallback)
05:45:12  Running job "gdrive_upload_missing"                    ← run #2 (scheduler)
05:45:13  shadow upload failed …/YOITConsulting/deepseek-v3: [SSL] record layer failure
05:45:13  shadow upload failed …/YOITConsulting/deepseek-v3: [SSL] record layer failure
05:45:18  shadow upload failed …/Clurgo/deepseek-v3
05:45:20  shadow upload failed …/Clurgo/deepseek-v3
```

The **same folder failing twice within one second** is the proof: two passes are
walking the same folder list at the same time.

`hunter/delivery.py::deliver_apply_now` calls `_upload_missing_folders()`
whenever the targeted upload did not happen ([delivery.py:47-48](../hunter/delivery.py)),
and `upload_missing_folders` has no re-entrancy guard.

### 1.3 Root cause: one non-thread-safe service shared by worker threads

`gdrive_sync._get_service()` caches a single `googleapiclient` service for the
lifetime of the process ([gdrive_sync.py:44-59](../hunter/gdrive_sync.py)). Under
it sits one `httplib2.Http` object with one keep-alive TLS socket, and
**httplib2 is not thread-safe** (documented upstream).

Every upload runs that service inside a worker thread:

* `_do_upload` → `asyncio.to_thread(upload_folder, svc, …)` (gdrive_sync.py:157)
* `upload_shadow_folder._do` → `asyncio.to_thread(upload_folder, svc, …)` (gdrive_sync.py:230)
* `upload_missing_folders._upload_row` → `asyncio.to_thread(upload_folder, svc, …)` (gdrive_sync.py:490)
* `upload_log_file` → `asyncio.to_thread(upload_file, svc, …)` (gdrive_sync.py:348)

Two of these in flight at once write into the same TLS stream and read each
other's bytes → exactly `record layer failure` / `LENGTH_MISMATCH`.

`_FOLDER_LOCK` ([gdrive_sync.py:76-106](../hunter/gdrive_sync.py)) serializes only
`get_or_create_folder`. It was added for the duplicate-folder race and does not
cover the file-upload calls, which is where the collision now happens.

### 1.4 Amplifier A: an abandoned thread keeps the poisoned socket

`upload_missing_folders` guards each folder with
`asyncio.wait_for(asyncio.to_thread(...), timeout=_UPLOAD_TIMEOUT)`.
`asyncio.to_thread` is **not cancellable** — on timeout the awaiting coroutine
gives up but the worker thread keeps running, still holding the shared socket
mid-request. Everything issued afterwards reads its leftovers. The 07-28 cascade
starts exactly this way:

```
01:46:11  shadow upload failed …/SETI_IT_Digital/deepseek-v4-pro: The read operation timed out
01:46:12  shadow upload failed …/Attio/deepseek-v3: [SSL] record layer failure
01:46:13  shadow upload failed …/RiteNRG/deepseek-v4-pro: [SSL] record layer failure
01:46:13  best_effort(gdrive.upload_shadow_folder): alert sent (3 consecutive failures)
```

The service is built with no explicit socket timeout, so a hung read can block a
worker thread indefinitely.

### 1.5 Amplifier B: shadow folders are re-uploaded on every single pass

`_upload_shadow_subfolders` ([gdrive_sync.py:522-548](../hunter/gdrive_sync.py))
re-uploads **every** shadow subfolder on every run. Shadow sets have no tracker
row, hence no `Drive URL` column to skip on, so there is no "already uploaded"
check at all — by design, "idempotent (Drive upserts by name)".

Measured on 2026-07-28: **86** distinct shadow folders, **1192** successful
shadow uploads in one day (~14 full passes), ~7 files each → on the order of
**700 Drive API calls every 30 minutes**, forever, all re-writing bytes that are
already there.

That is why one backfill pass takes **20–25 minutes** against a 30-minute
interval (01:51:18 → 02:11:02; 02:21:18 → 02:46:17; 02:56:52 → 03:16:21). The
window in which any other Drive caller can collide is therefore almost always
open.

### 1.6 Impact

Moderate, not data-losing:

* every failed folder is retried on the next pass, so nothing is permanently lost;
* Telegram alerts from `best_effort` (the owner-visible symptom);
* wasted Drive API quota and 20+ minutes of pointless traffic per pass;
* a genuine post-apply CV folder can reach Drive 20–30 min late when its targeted
  upload is the one that gets corrupted (e.g. 07-28 07:23:42
  `upload_application_folder … YOITConsulting: [SSL] record layer failure`).

---

## 2. Fixes

Three milestones, ordered so each is independently shippable and testable.
M1 stops the collision, M2 makes the collision impossible by construction, M3
removes the load that keeps the window open.

### M1 — Re-entrancy guard on `upload_missing_folders`

**Problem:** the delivery fallback and the scheduled backfill run the same full
pass concurrently (§1.2). A second pass is not just unsafe, it is pointless: the
first one is already uploading exactly the folders the second would.

**Change** (`hunter/gdrive_sync.py`):

* module-level `_BACKFILL_LOCK: asyncio.Lock | None` created lazily (same
  pattern as `_folder_lock()` — no running loop at import time);
* `upload_missing_folders` acquires it non-blockingly (`locked()` check /
  `asyncio.Lock` + a `_backfill_running` flag). If a pass is already running:
  log once at INFO and return a result dict with a new
  `"skipped_busy": True` key and zero counters — **no exception**, so
  `best_effort` does not count it as a failure;
* `commands/gdrive.py` reports "⏳ a backfill is already running — skipped" when
  `skipped_busy` is set, instead of a misleading "✅ Uploaded: 0".

**Deliberately not done:** queueing the second pass. Its work is a strict subset
of the running one; waiting would only re-walk the same list a minute later.

**Acceptance:** two concurrent `upload_missing_folders()` calls → the second
returns immediately with `skipped_busy`, `upload_folder` is called exactly once
per folder.

**Tests** (`tests/test_gdrive_sync.py`): concurrent-call test asserting one pass
executes and the other short-circuits; the lock is released after an exception
inside the pass (guard must be in a `finally`).

---

### M2 — Serialize every Drive API call + kill the abandoned-thread hazard

**Problem:** §1.3/§1.4. M1 removes the *double backfill*, but a targeted
post-apply upload (`upload_application_folder`), a log upload
(`upload_log_file`) and the backfill can still overlap — that is a different
pair of callers, and 07-28 07:23:42 proves it happens.

**Change** (`hunter/gdrive_sync.py`, `hunter/gdrive_client.py`):

1. **One process-wide Drive lock.** Rename `_FOLDER_LOCK` → `_DRIVE_LOCK` and
   route *every* Drive API call through one helper:

   ```python
   async def _drive_call(fn, *args, timeout: float | None = None):
       async with _drive_lock():
           return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
   ```

   Call sites to convert: `_resolve_folder` (keeps its current semantics),
   `_do_upload`, `upload_shadow_folder._do`, `upload_missing_folders._upload_row`,
   `upload_log_file`. Throughput is irrelevant here — this is a background chore,
   and after M3 a full pass is seconds, not minutes.

   The lock is acquired **per folder**, never for a whole pass, so a post-apply
   targeted upload waits at most one folder (~seconds) behind the backfill.

   Cross-*process* concurrency (detached dual-apply shadows) is unaffected: each
   process has its own service and its own socket. The existing
   `_resolve_create_race` in `gdrive_client` remains the guard there.

2. **Explicit socket timeout on the service.** Build with an authorized
   `httplib2.Http(timeout=DRIVE_HTTP_TIMEOUT)` so a hung request dies inside the
   worker thread instead of blocking it forever:

   ```python
   authed = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=...))
   build("drive", "v3", http=authed)
   ```

   `google-auth-httplib2==0.2.0` and `httplib2==0.32.0` are already in
   `requirements.lock` (transitive deps of `google-api-python-client`) — **no new
   dependency, no lock regeneration**. Note `build()` accepts either
   `credentials=` or `http=`, never both. Same change applies to
   `gsheets_client.build_service` only if M5 is taken (see §4).

   New config knob `GDRIVE_HTTP_TIMEOUT_SEC` (default `60`), documented in
   `.env.example` + CLAUDE.md config table.

3. **Invalidate the service on a wall-clock timeout.** In
   `upload_missing_folders`, an `asyncio.TimeoutError` from `wait_for` means a
   thread was abandoned mid-request and the connection state is unknown — call
   `_invalidate_service()` in that handler so the next call builds a fresh
   service (and a fresh socket) instead of inheriting the poisoned one.

**Acceptance:** with a fake service that records `(thread_id, enter_ts, exit_ts)`
per call, N concurrent upload coroutines produce **zero overlapping intervals**.

**Tests:** overlap-detection test (mutation-verified: removing the lock must make
it fail); `build_service` passes an `Http` carrying the configured timeout; a
`wait_for` timeout drops the cached service.

---

### M3 — Shadow-upload ledger (stop re-uploading 86 folders every 30 min)

**Problem:** §1.5. Shadow sets have no tracker row, so the per-row `Drive URL`
skip cannot see them and they are re-uploaded forever.

**Change:**

**Implementation note (deviation from the sketch below):** the DDL was NOT
added to `hunter/db.py`'s `_DDL`/`init_db()`. "Lazily ensured exactly like
`source_runs`" is itself the more specific instruction — `source_runs`'
schema lives entirely inside `hunter/source_health.py`, self-contained, never
touching `hunter/db.py`. `drive_ledger.py` follows that exact precedent: its
own `_DDL` + `_ensure_table(conn)`, called defensively on every read/write,
with a module-level `DB_PATH = TRACKER_DB_PATH` tests can monkeypatch (same
shape as `hunter.source_health.DB_PATH`). Same SQLite file (`tracker.db`),
different Python module. The schema is unchanged from the sketch.

1. New table (in `hunter/drive_ledger.py`, not `hunter/db.py` — see note
   above), lazily ensured exactly like `source_runs` / `subsystem_health`:

   ```sql
   CREATE TABLE IF NOT EXISTS drive_uploads (
       path        TEXT PRIMARY KEY,   -- folder path as recorded locally
       signature   TEXT NOT NULL,      -- file count : max mtime_ns : total size
       drive_url   TEXT NOT NULL DEFAULT '',
       uploaded_at TEXT NOT NULL
   );
   ```

2. New module `hunter/drive_ledger.py` — `signature(folder) -> str`,
   `is_current(path, sig) -> bool`, `record(path, sig, url)`, `forget(path)`.
   Signature is content-derived, so a shadow set whose files were renamed by the
   verdict suffix (`…_EN_ats91.pdf`) or regenerated re-uploads automatically;
   an untouched one is skipped.

3. `_upload_shadow_subfolders` consults the ledger before calling
   `upload_shadow_folder`, and records after a successful upload. Counters in the
   result dict gain `shadow_skipped`, surfaced by `/gdrive_upload_missing`.

4. **Escape hatch:** `/gdrive_upload_missing force` bypasses the ledger (the one
   case the ledger gets wrong is a folder deleted on Drive by hand — the bot
   cannot see that). `parse` the optional arg in `commands/gdrive.py`, thread
   `force: bool = False` through `upload_missing_folders`.

**Deliberately not done:** a marker file inside the folder. It would itself be
uploaded to Drive, and it lives in `Applications/` which is gitignored,
container-mounted and periodically pruned — the DB is the right home, and
`tracker.db` is already the mounted, durable store the rest of this bot uses.

**Acceptance:** a second pass over an unchanged corpus performs **zero** Drive
calls for shadow folders; touching one file in one shadow folder makes exactly
that one re-upload; `force` re-uploads everything.

**Tests** (`tests/test_gdrive_sync.py` + new `tests/test_drive_ledger.py`):
signature changes on add/remove/rename/mtime bump; skip path; force path;
ledger write happens only after a *successful* upload (a failed upload must be
retried next pass — mutation-verified).

---

## 3. Verification in prod (after deploy)

1. `grep -c "record layer failure" <today>.log` → **0**.
2. Backfill pass duration (`Running job "gdrive_upload_missing"` → matching
   `executed successfully`) drops from ~20 min to **seconds**.
3. `/gdrive_upload_missing` reports `Shadow uploaded: 0`, `Shadow skipped: 86`
   on a steady-state corpus.
4. No `best_effort(gdrive.*)` alert for 48 h.

**Ops, independent of this branch** (still outstanding from PR #163/#166):
the duplicate Drive folders are still there — `8 duplicate folders named
'2026-07-09'`, `6 × '2026-07-12'`, `3 × '2026-07-10'` in today's log. Run:

```bash
docker compose exec job-hunter python tools/dedup_drive_folders.py
```

then re-run with `--apply` once the dry run looks right.

---

## 4. Out of scope (flagged, not fixed here)

**4.1 `delivery` keeps missing its own tracker row.** The fallback that triggers
the race fires because the targeted lookup misses:

```
2026-07-27 01:40:03  [delivery] no tracker row found for https://builtin.com/job/python-pytorch-developer-frontend-inference-compiler-dubai/7565379 — falling back
2026-07-29 01:40:06  [delivery] no tracker row found for  … the same URL …
```

The same URL, day after day, right after `[auto-apply] OK`. Suspicion: a
normalization mismatch between the URL the row is written under and the one
`cache.get_row_by_url` / `get_folder_by_url` looks up. M1 makes the consequence
harmless, but a full pointless backfill is still triggered per apply. Worth its
own investigation — needs a look at `normalize_url` vs. what `add_applied`
stores for this source.

**4.2 Google Sheets has the identical shape.** `gsheets_sync._get_service()`
caches one service and hands it to many `asyncio.to_thread` calls
(`gsheets_sync.py:158, 176, 183, 211, 259, 264, 303, 326, 468…`). Same
non-thread-safe `httplib2` underneath; it has not blown up only because Sheets
calls are short and rarer. If M2 lands cleanly, applying the same
`_drive_call`-style guard + socket timeout to `gsheets_sync` is ~20 lines and
closes the class of bug rather than one instance. **Recommended as a follow-up
M5, not bundled here** — it widens the blast radius of a fix the owner needs in
prod now.

---

## 5. Milestone checklist

- [x] **M1** re-entrancy guard + `skipped_busy` reporting + tests
- [x] **M2** `_DRIVE_LOCK` over all Drive calls, `GDRIVE_HTTP_TIMEOUT_SEC`,
      service invalidation on `wait_for` timeout + overlap tests
- [x] **M3** `drive_uploads` table, `hunter/drive_ledger.py`, shadow skip,
      `/gdrive_upload_missing force` + tests
- [ ] CLAUDE.md updated in the same commit (config table, `gdrive_sync`
      description, repository layout entry for `drive_ledger.py`, work-log entry)
- [ ] `ruff check .` + `ruff format .` + `python -m compileall .` + `pytest tests/`
- [ ] Ops: run `tools/dedup_drive_folders.py` (dry run → `--apply`)
