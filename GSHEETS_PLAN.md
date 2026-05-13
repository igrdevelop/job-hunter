# Google Sheets Tracker — Implementation Plan (v2)

**Branch:** `feature/google-sheets-tracker`
**Goal:** Mirror `tracker.xlsx` to a single Google Spreadsheet so the user can view and edit it from a browser anywhere. The local Excel file remains as a safety net (primary write target). Sheets is best-effort secondary.

**Key design decisions:**
- No separate `to_send` file. Google Sheets has built-in filter views — user creates a saved filter "Unsent only" (rows where `Sent` is empty) once in the browser.
- Excel is primary, Sheets is mirror. If Sheets fails, bot keeps working.
- In-memory cache speeds up dedup (free perf win).
- Both bot and user can write `Sent` column — bot only writes lifecycle statuses (`EXPIRED`), user writes actual send dates. They never target the same row simultaneously.

**This v2 plan incorporates fixes for blockers found in the v1 self-review.**

---

## 1. Architecture

### Data flow

```
       ┌─────────────────────────────────────────────────┐
       │                  Bot (server)                    │
       │                                                  │
       │  ┌──────────────┐    ┌────────────────────────┐  │
       │  │  Excel I/O   │───▶│   In-memory cache     │  │
       │  │  (primary)   │    │   (full tracker view) │  │
       │  └──────────────┘    └───────────┬────────────┘  │
       │         ▲                        │               │
       │         │                        │ writes        │
       │  ┌──────┴────┐    ╔══════════╗   ▼               │
       │  │  ALL      │◀───╣ tracker  ╠───────────────┐   │
       │  │  writes   │    ║ asyncio  ║               │   │
       │  │  protected│    ║   Lock   ║               │   │
       │  │  by lock  │    ╚══════════╝               │   │
       │  └───────────┘                               │   │
       │         ▲                                    │   │
       │         │ writes                             │   │
       │  ┌──────┴───────────────────────────────────┐│   │
       │  │ apply pipeline / hunt loop / commands   ││   │
       │  └──────────────────────────────────────────┘│   │
       │                                              │   │
       │  ┌──────────────────────────┐                │   │
       │  │  Sheets client (writer)  │◀───────────────┘   │
       │  │  best-effort, async      │                    │
       │  └─────────────┬────────────┘                    │
       │                │                                 │
       │  ┌─────────────▼────────────┐    every 30 min    │
       │  │  Sheets pull (read)      │────────────────────┤
       │  │  full snapshot           │    pull user edits │
       │  └──────────────────────────┘                    │
       └────────────────┬─────────────────────────────────┘
                        │
                        ▼ Google Sheets API
                  ┌──────────────────┐
                  │  Tracker (sheet) │  ◀── user edits Sent
                  │                  │      column via browser
                  └──────────────────┘      filter view
                                            "Unsent only"
```

### Write order (every mutation)

All writes go through a single `asyncio.Lock` in `TrackerCache`:

1. **Local Excel** — synchronous. If fails → bubble up the error, stop.
2. **In-memory cache** — synchronous.
3. **Google Sheets** — async, best-effort. On failure: log + mark cache row as `gsheets_dirty=True`.

Lock prevents the hunt loop and the apply pipeline (which run concurrently in different tasks) from corrupting the cache.

### Sync direction (corrected)

- **Excel → Sheets:** every write (status changes, new applies, EXPIRED stamps).
- **Sheets → Excel:** periodic full snapshot pull (every 30 min) + on `/sync_sent` command. **Pulls ALL user-editable columns** (Sent, To Learn, Re-application), not just Sent.
- **Conflict resolution rule:** rows are matched by `ID` (column K). For each matched row:
  - If `Sent` differs and the bot didn't recently write `EXPIRED`: trust Sheets value.
  - If `Sent` is `EXPIRED` in Excel and empty/different in Sheets: bot's `EXPIRED` wins (it's a lifecycle stamp).
  - For non-conflicting user-editable columns (`To Learn`, `Re-application`, `Folder` if changed): trust Sheets.
  - `URL` and `ID` columns: never change in either direction (immutable).

### What if user deletes a row in Sheets

The bot logs a warning and **restores** the row from the Excel snapshot on next pull. Excel is the source of truth for row existence. (User can edit cells but not delete rows.)

### Resync background task

A separate JobQueue task `gsheets_resync_dirty` runs every 5 min and retries any rows where `gsheets_dirty=True` (covers network blips or quota errors).

---

## 2. Bootstrap & one-shot ID handling (FIXED: no infinite loop)

**Critical fix from v1 review:** v1 said "bot creates Sheet, exits, user adds ID to .env, restarts." But Docker has `restart: always`, so the bot would loop forever, creating a new spreadsheet each restart.

**v2 design:**

1. On startup, bot checks `gsheets_state.json` (local file on volume mount, not in git).
2. If `gsheets_state.json` has `spreadsheet_id` and it matches `GSHEETS_TRACKER_ID` from env → normal operation.
3. If `gsheets_state.json` has `spreadsheet_id` but env is empty → keep nagging user via Telegram every hour, but DO NOT create a new sheet. Use the one from state file for the current session.
4. If neither file nor env has ID → first-run flow: create sheet, save ID to `gsheets_state.json`, send Telegram message with ID + instructions, **continue running** with that ID.
5. User adds ID to `.env`, restarts at their convenience — by then state matches env.

`gsheets_state.json` mounted as volume → persists across container restarts.

```yaml
volumes:
  - ./gsheets_state.json:/app/gsheets_state.json
```

State file structure:
```json
{
  "spreadsheet_id": "1AbC...",
  "created_at": "2026-05-14T10:30:00Z",
  "last_pull_at": "2026-05-14T11:00:00Z"
}
```

---

## 3. Authentication

- **Type:** OAuth (same flow as Gmail).
- **Scopes:**
  - `https://www.googleapis.com/auth/spreadsheets` — read/write to Sheets.
  - `https://www.googleapis.com/auth/drive.file` — minimal Drive permission to **create** new files.
  - ⚠️ **If `GSHEETS_DRIVE_FOLDER_ID` is set:** `drive.file` is **not enough** — you need full `https://www.googleapis.com/auth/drive` to move/place files in an existing folder. Decision: skip folder placement for now (file is in user's Drive root). Folder organization is a separate task.
- **Credentials file:** `gsheets_credentials.json` (downloaded from Google Cloud Console).
- **Token file:** `gsheets_token.json` (created after first auth).
- **First-run setup:**
  1. Run `python tools/gsheets_auth.py` locally — opens browser, user grants permission, token saved.
  2. `scp` `gsheets_token.json` to server `/home/deploy/job-hunter/`.
  3. Restart container.

### Startup validation (FIXED: fail-fast)

If `GSHEETS_ENABLED=true`:
- Validate `gsheets_credentials.json` exists → else log + Telegram alert + disable Sheets for session.
- Validate `gsheets_token.json` exists → same.
- Try one read of the spreadsheet (HEAD-style) → if 403/404, log + alert + disable Sheets for session.

Bot **keeps running** with Excel-only if Sheets validation fails. It does not crash.

---

## 4. Schema

Single Google Spreadsheet, single tab `Tracker`. Same 11 columns as `tracker.xlsx`:

| Col | Name | Editable by | Notes |
|-----|------|-------------|-------|
| A | Date | bot only | Application date |
| B | Company | bot only | |
| C | Job Title | bot only | |
| D | Stack | bot only | |
| E | ATS % | bot only | Or SKIP / FAIL / MANUAL / EXPIRED |
| F | URL | nobody (immutable) | Dedup key |
| G | Folder | bot writes, user can edit | Path on server |
| H | Sent | **both** | Bot writes `EXPIRED`; user writes actual send date |
| I | Re-application | both | `+` flag |
| J | To Learn | both | Skills gap notes |
| K | ID | nobody (immutable) | Short UUID (8-char hex) — sync key |

Header row formatted bold + frozen.

### Filter view (user creates once in the browser)

- **"Unsent only"** — `Sent` column is empty. This replaces the old `to_send.xlsx`.

Bot's first-run Telegram message includes step-by-step instructions to create this filter view.

---

## 5. Row addressing & lookup strategy (FIXED: avoid per-write reads)

**Problem from v1 review:** Google Sheets API works with row indices (e.g. `A5:K5`). If we only know `ID`, we'd have to search the sheet for that ID before every write — expensive.

**v2 solution:** the bot's cache stores `sheet_row_index` per row alongside `ID`.

- On full pull (every 30 min), update both data AND `sheet_row_index` map in cache.
- On append (new row): API returns the inserted range, parse the row index, store in cache.
- On cell update: use cached `sheet_row_index` directly — one write call.
- **Invariant:** rows are append-only. Bot never inserts rows in the middle. User is documented to not delete rows (only edit cells). If user does delete → next pull detects mismatch, logs warning, restores row to bottom.

This gives us O(1) write cost with no extra reads.

---

## 6. Files to create / modify

### New files

- `hunter/gsheets_client.py` — low-level wrapper:
  - `read_all(sheet_id, tab) -> list[tuple[int, dict]]` (returns row_index + data)
  - `append_rows(sheet_id, tab, rows: list[dict]) -> list[int]` (returns row indices)
  - `update_cell(sheet_id, tab, row_idx, col, value)`
  - `update_row(sheet_id, tab, row_idx, row: dict)`
  - `create_spreadsheet(title) -> str` (returns ID)
- `hunter/gsheets_sync.py` — high-level domain logic:
  - `init_or_load_spreadsheet()` — bootstrap with state file handling.
  - `mirror_apply(row, sheet_row_idx_callback)`, `mirror_skip(...)`, etc.
  - `mirror_expired_batch(rows)` — for the EXPIRED batch write
  - `pull_full_snapshot()` — read sheet, diff with cache, update Excel + cache for changes.
  - `resync_dirty()` — retry failed writes.
- `hunter/tracker_cache.py` — in-memory cache (see §7).
- `tools/gsheets_auth.py` — one-time OAuth flow.
- `tests/test_gsheets_client.py` — mocked-API unit tests.
- `tests/test_gsheets_sync.py` — sync logic tests.
- `tests/test_tracker_cache.py` — cache correctness tests including concurrency.

### Modified files

- `hunter/config.py` — add:
  - `GSHEETS_ENABLED` (default `False`)
  - `GSHEETS_TRACKER_ID` (env, may be empty)
  - `GSHEETS_REFRESH_INTERVAL_MIN` (default 30)
  - `GSHEETS_CREDENTIALS_FILE`, `GSHEETS_TOKEN_FILE`, `GSHEETS_STATE_FILE` paths
- `hunter/tracker.py` — wire cache + Sheets hooks after every Excel write.
- `hunter/services/tracker_service.py` — same.
- `hunter/expired_to_send_check.py` → **rename** to `hunter/expired_marker.py` (or merge into `expired_check.py`). Drop to_send-specific logic.
- `hunter/telegram_bot.py`:
  - Rewrite `/unsent` to count from cache.
  - Rewrite `/sync_sent` → `gsheets_sync.pull_full_snapshot()`.
  - Add JobQueue: `gsheets_resync_dirty` every 5 min, `gsheets_pull` every 30 min.
  - New command `/gsheets_status` (ID, last pull time, dirty count).
  - New command `/gsheets_resync` (force retry dirty rows).
- `hunter/main.py` — preserve `/force` re-apply semantics in cache (see §8).
- `hunter/app.py` (or startup hook) — call cache load + `init_or_load_spreadsheet()`.
- `docker-compose.yml` — mount `gsheets_*.json`, `gsheets_state.json`. Remove `to_send.xlsx` volume.
- `.gitignore` — `gsheets_credentials.json`, `gsheets_token.json`, `gsheets_state.json`.

### Deleted

- `hunter/to_send.py`
- `tests/test_to_send_sync.py`
- `tools/check_expired_to_send.py`
- `to_send.xlsx` from server (after migration is verified)

### Phase 3 surface area (FIXED: full inventory)

`grep -r "to_send"` found **15 files**. Each must be visited:

| File | Change |
|------|--------|
| `hunter/to_send.py` | DELETE |
| `hunter/tracker.py` | Remove `sync_sent_marks_from_to_send()` and its callers |
| `hunter/telegram_bot.py` | Rewrite `/unsent`, `/sync_sent` |
| `hunter/expired_to_send_check.py` | Rename → `expired_marker.py`, drop to_send logic |
| `hunter/main.py` | Remove `to_send.sync_and_rebuild()` call |
| `hunter/services/tracker_service.py` | Remove to_send rebuild after apply |
| `hunter/tracker_backup.py` | Remove to_send backup |
| `hunter/config.py` | Remove TO_SEND_PATH constants |
| `tools/repair_tracker.py` | Drop to_send fix logic |
| `tools/fix_pracuj_urls.py` | Drop to_send rewrite |
| `tools/backup_tracker.py` | Drop to_send backup |
| `tools/check_expired_to_send.py` | DELETE |
| `tests/test_to_send_sync.py` | DELETE |
| `tests/test_tracker_backup.py` | Remove to_send assertions |
| `tests/test_tracker_service.py` | Remove to_send setup |

A single grep at the end of Phase 3 must return zero matches.

---

## 7. In-memory cache (FIXED: concurrent-safe)

```python
# hunter/tracker_cache.py
class TrackerCache:
    rows: dict[str, dict]            # ID -> row dict
    by_url: dict[str, str]           # normalized_url -> ID
    by_ctkey: dict[str, str]         # company+title key -> ID
    sheet_row_index: dict[str, int]  # ID -> Sheets row index
    dirty_ids: set[str]              # rows that failed to push to Sheets
    _lock: asyncio.Lock              # serializes all mutations

    async def load_from_excel(self) -> None
    async def add(self, row: dict) -> None
    async def update_status(self, row_id: str, ats_value: str) -> None  # SKIP/FAIL/EXPIRED/...
    async def update_sent(self, row_id: str, sent_value: str) -> None
    async def update_field(self, row_id: str, field: str, value: str) -> None
    async def is_known_url(self, url: str, allow_reapply: bool = False) -> bool
    async def is_known_ct(self, company: str, title: str) -> bool
    async def unsent_count(self) -> int
    async def unsent_angular_count(self) -> int
    async def all_unsent(self) -> list[dict]
```

- All mutations acquire `_lock`. Reads of `is_known_*` also acquire (to avoid reading partial state).
- `is_known_url(url, allow_reapply=True)` is the hook for `/force` (see §8).
- Cache is the **single source of truth in process** — Excel is the persisted state, Sheets is the visible mirror.

---

## 8. `/force` re-apply preservation (FIXED)

Current `/force` flow lets user apply to a URL even if it's in tracker. With cache-based dedup, we add `allow_reapply` flag:

- Normal hunt: `cache.is_known_url(url)` → blocks duplicates.
- `/force <url>` flow: bypasses dedup entirely (it's an explicit override) — doesn't touch cache check.
- Re-application row gets `Re-application = +` flag and a fresh `ID`. URL appears twice in tracker.
- Cache's `by_url` map stores the **most recent** ID for that URL; the older row is still in `rows` (queryable by ID).

---

## 9. `Sent` write semantics (FIXED: who writes what)

Audit of current code reveals bot DOES write `Sent` in two places:

1. **`expired_to_send_check.py:194`** — sets `Sent="EXPIRED"` when a job offer is expired.
2. **`tracker.py:940`** — `sync_sent_marks_from_to_send()` copies user's `Sent` values from to_send.xlsx into tracker.xlsx.

After the refactor:

- `EXPIRED` writes — bot still does this, in both Excel and Sheets (lifecycle automation).
- User send-date writes — user types in Sheets directly, bot pulls every 30 min.
- The old `sync_sent_marks_from_to_send` path disappears (no to_send).
- They never race: a job is marked `EXPIRED` only if user hasn't already sent it; once user types a date in `Sent`, the EXPIRED check skips that row.

Conflict matrix:

| Excel `Sent` | Sheets `Sent` (on pull) | Action |
|--------------|--------------------------|--------|
| empty | empty | no-op |
| empty | `<date>` from user | Excel <- Sheets value (cache + xlsx update) |
| `EXPIRED` (bot) | empty | Sheets <- EXPIRED (push, if not already there) |
| `EXPIRED` (bot) | `<date>` from user | EDGE CASE — user marked sent on an expired job. Trust Sheets value. Log info. |
| `<date>` (synced earlier) | empty (user erased) | Excel <- empty. Log warning. |
| `<date>` | different `<date>` | Excel <- Sheets (user's latest wins) |

---

## 10. Migration on first run

1. Bot starts, sees `gsheets_state.json` empty and `GSHEETS_TRACKER_ID` empty.
2. Calls Sheets API `spreadsheets.create` → gets ID.
3. Reads local `tracker.xlsx` → uploads all rows in batch (one `values.update` call).
4. Formats header row (bold, frozen).
5. Writes `gsheets_state.json` with the ID.
6. Sends Telegram message:
   ```
   ✅ Google Sheets tracker initialized
   📊 https://docs.google.com/spreadsheets/d/<id>

   To make this permanent, add to .env on the server:
     GSHEETS_TRACKER_ID=<id>

   Then optionally restart. The bot will use this Sheet either way.

   📝 To replace the old to_send.xlsx workflow:
   In the Sheet, go to Data → Create filter view →
   filter "Sent" column "Is empty" → save as "Unsent only".
   ```
7. Continues running normally (no exit).

---

## 11. Implementation phases

### Phase 1 — Auth + client (LOW risk)
- [ ] 1.1 `tools/gsheets_auth.py` (clone gmail_auth)
- [ ] 1.2 `hunter/gsheets_client.py` with mocked-API tests
- [ ] 1.3 Verify `drive.file` scope creates files in root (manual)

### Phase 2 — Cache + concurrency (LOW risk, big perf)
- [ ] 2.1 `hunter/tracker_cache.py` with `asyncio.Lock`
- [ ] 2.2 Wire load at startup
- [ ] 2.3 Wire `is_known_*` through cache
- [ ] 2.4 Wire all mutations through cache
- [ ] 2.5 Tests including concurrent add/update
- [ ] 2.6 Bench hunt cycle before/after (expect 5-10x speedup on dedup)

### Phase 3 — Drop to_send (HIGH risk — touches 15 files)
- [ ] 3.1 Full pre-grep inventory committed as checklist (`PHASE3_TO_SEND_GREP.md`)
- [ ] 3.2 Rewrite `/unsent` (cache-only)
- [ ] 3.3 Rewrite `/sync_sent` as no-op placeholder (real impl in Phase 5)
- [ ] 3.4 Visit each of the 15 files, delete to_send refs
- [ ] 3.5 Rename `expired_to_send_check.py` → `expired_marker.py`
- [ ] 3.6 Remove `to_send.xlsx` mount from docker-compose
- [ ] 3.7 Run full test suite (expect 5-10 failures, fix each)
- [ ] 3.8 Final grep: `grep -r "to_send" --include="*.py" hunter/ tests/ tools/` → must be empty
- [ ] 3.9 Manual test on local copy of tracker.xlsx — hunt cycle works, /unsent counts correct

### Phase 4 — Sheets mirror (writes) (MEDIUM risk)
- [ ] 4.1 `hunter/gsheets_sync.py` mirror_* functions
- [ ] 4.2 Hook `mirror_apply` after successful applies
- [ ] 4.3 Hook `mirror_skip/fail/manual/expired`
- [ ] 4.4 try/except wrapper → `cache.mark_dirty(id)` on failure
- [ ] 4.5 Background `resync_dirty` job (5 min)
- [ ] 4.6 `/gsheets_status`, `/gsheets_resync` commands
- [ ] 4.7 Startup validation (credentials present, sheet reachable)
- [ ] 4.8 Tests including failure injection

### Phase 5 — Sheets pull (reads) (MEDIUM risk)
- [ ] 5.1 `pull_full_snapshot()` with conflict matrix from §9
- [ ] 5.2 Hook into `/sync_sent` command
- [ ] 5.3 Schedule periodic pull every 30 min
- [ ] 5.4 Tests for each row of the conflict matrix
- [ ] 5.5 Test for "user deleted row" → bot re-appends

### Phase 6 — Bootstrap (LOW risk)
- [ ] 6.1 State file `gsheets_state.json` read/write
- [ ] 6.2 `init_or_load_spreadsheet()` with all three branches (state, env, both empty)
- [ ] 6.3 Telegram first-run message with filter-view instructions
- [ ] 6.4 Manual test from clean state

### Phase 7 — E2E + rollout (LOW risk)
- [ ] 7.1 One e2e test against a real test Sheet (not in CI, manual)
- [ ] 7.2 Update CLAUDE.md architecture section
- [ ] 7.3 PR feature → develop, merge
- [ ] 7.4 PR develop → master (use **Create a merge commit**, not squash)
- [ ] 7.5 Deploy
- [ ] 7.6 SCP credentials + token + empty state file to VPS
- [ ] 7.7 Set `GSHEETS_ENABLED=true`, restart
- [ ] 7.8 Verify Telegram message with link
- [ ] 7.9 Add ID to .env, restart, verify mirror writes work
- [ ] 7.10 Create "Unsent only" filter view in browser

---

## 12. Risks & open questions (updated)

| Risk | Mitigation | Status |
|------|-----------|--------|
| Google API quota (60 read/min, 100 write/min) | Cached reads (only 30-min pulls), batch writes | designed |
| Infinite-loop creating spreadsheets | `gsheets_state.json` checkpoint | FIXED §2 |
| Row index volatility on writes | Cache `sheet_row_index`, append-only invariant | FIXED §5 |
| Concurrent writes race | `asyncio.Lock` in cache | FIXED §7 |
| Bot writes Sent too | Conflict matrix | FIXED §9 |
| `drive.file` scope insufficient for folder | Skip folder placement v1 | DEFERRED §3 |
| User deletes row in Sheets | Restore from Excel on pull | FIXED §1 |
| `/force` re-apply blocked by cache | `allow_reapply` flag | FIXED §8 |
| Phase 3 underestimated (15 files) | Pre-grep checklist + final grep | FIXED §6 |
| No fail-fast on missing credentials | Startup validation | FIXED §3 |
| Mocks miss real schema issues | Manual e2e against real Sheet (Phase 7.1) | FIXED §11 |
| Cache memory growth long-term | Measure after migration, lazy load if >100MB | accepted |
| Token refresh in Docker | Token file is volume-mounted, refresh persists | accepted |

---

## 13. Out of scope (for this branch)

- Google Drive for `Applications/` folder uploads.
- Replacing Excel entirely.
- Drive folder organization (requires wider scope).
- Real-time push notifications.
- Multi-user spreadsheet sharing.

---

## 14. Acceptance criteria

A pass means **all** of:

1. Bot starts fresh (no state, no env ID): creates Sheet, writes state file, messages user, **keeps running** (no exit, no restart loop).
2. User adds ID to `.env`, restarts, bot continues normally; state file unchanged.
3. Bot starts with missing credentials: logs error, sends Telegram alert, **keeps running** with Excel-only.
4. Every apply writes to both `tracker.xlsx` and Sheets within 5 seconds.
5. Sheets API offline: bot uses Excel-only; dirty rows retry every 5 min and eventually succeed.
6. User edits `Sent` column in browser → within 30 min (or via `/sync_sent`), value lands in Excel + cache.
7. User deletes a row in Sheets → bot detects on next pull, logs warning, restores row.
8. `/force <url>` re-applies even if URL is in cache.
9. `/gsheets_status` shows ID, last pull time, dirty count.
10. `/unsent` reads cache, shows correct count (no to_send file involved).
11. Concurrent hunt + apply do not corrupt cache (lock test).
12. `grep -r "to_send" --include="*.py"` returns zero results.
13. All existing tests pass + new tests for client/sync/cache.
14. CLAUDE.md updated with new architecture.
