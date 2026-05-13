# Google Sheets Tracker — Implementation Plan

**Branch:** `feature/google-sheets-tracker`
**Goal:** Mirror `tracker.xlsx` to a single Google Spreadsheet so the user can view and edit it from a browser anywhere. The local Excel file remains as a safety net (primary write target). Sheets is best-effort secondary.

**Key design decision:** No separate `to_send` file. Google Sheets has built-in filter views — the user creates a saved filter "Unsent only" (rows where `Sent` is empty) once in the browser and uses it forever. This eliminates an entire sync flow that existed only because Excel doesn't have decent filter views.

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
       │         │ writes                 │ writes        │
       │         │                        ▼               │
       │  ┌──────┴───────────────────────────────────┐    │
       │  │  apply pipeline / hunt loop / commands  │    │
       │  └──────────────────────────────────────────┘    │
       │                                  │               │
       │  ┌──────────────────────────┐    │ best-effort   │
       │  │  Sheets client (writer)  │◀───┘ async         │
       │  └─────────────┬────────────┘                    │
       │                │                                 │
       │  ┌─────────────▼────────────┐    every N min     │
       │  │  Sheets refresh (read)   │────────────────────┤
       │  └──────────────────────────┘    pull user edits │
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

1. **Local Excel** — synchronous. If fails → bubble up the error, stop.
2. **In-memory cache** — synchronous.
3. **Google Sheets** — async, best-effort. On failure: log + mark cache row as `gsheets_dirty=True`.

### Sync direction

- **Excel → Sheets**: every write.
- **Sheets → Excel**: periodic (every 30 min) + on `/sync_sent` command. Pulls user-edited `Sent` values from Sheets back into Excel and cache.
- **Conflict resolution:** Sheets wins for the `Sent` column only. Bot never writes `Sent` itself (only user does).

### Resync background task

A separate JobQueue task `gsheets_resync_dirty` runs every 5 min and retries any rows where `gsheets_dirty=True` (covers network blips or quota errors).

---

## 2. Authentication

- **Type:** OAuth (same flow as Gmail).
- **Scopes:**
  - `https://www.googleapis.com/auth/spreadsheets` — read/write to Sheets.
  - `https://www.googleapis.com/auth/drive.file` — minimal Drive permission to **create** new Spreadsheet files (no full Drive access).
- **Credentials file:** `gsheets_credentials.json` (downloaded from Google Cloud Console).
- **Token file:** `gsheets_token.json` (created after first auth).
- **First-run setup:**
  1. Run `python tools/gsheets_auth.py` locally — opens browser, user grants permission, token saved.
  2. `scp` `gsheets_token.json` to server `/home/deploy/job-hunter/`.
  3. Restart container.

### Mount in docker-compose.yml

```yaml
volumes:
  - ./gsheets_credentials.json:/app/gsheets_credentials.json
  - ./gsheets_token.json:/app/gsheets_token.json
```

---

## 3. Schema

Single Google Spreadsheet, single tab `Tracker`. Same 11 columns as `tracker.xlsx`:

| Col | Name | Notes |
|-----|------|-------|
| A | Date | Application date |
| B | Company | |
| C | Job Title | |
| D | Stack | |
| E | ATS % | Or SKIP / FAIL / MANUAL / EXPIRED |
| F | URL | Dedup key |
| G | Folder | Path on server |
| H | Sent | **User-editable** in browser |
| I | Re-application | `+` flag |
| J | To Learn | |
| K | ID | Short UUID (8-char hex) — sync key |

Header row formatted bold + frozen.

### Filter views (user creates once in the browser)

- **"Unsent only"** — `Sent` column is empty. This replaces the old `to_send.xlsx`.
- **"Sent this month"**, **"FAIL"**, **"MANUAL"** — optional, user can add as needed.

Filter views are per-user shareable URLs — no bot involvement.

---

## 4. Files to create / modify

### New files

- `hunter/gsheets_client.py` — low-level wrapper over `googleapiclient.discovery.build('sheets', 'v4')`:
  - `read_all(sheet_id, tab) -> list[dict]`
  - `append_rows(sheet_id, tab, rows: list[dict])`
  - `update_cell(sheet_id, tab, row_idx, col, value)`
  - `update_row(sheet_id, tab, row_idx, row: dict)`
  - `create_spreadsheet(title) -> str` (returns ID)
- `hunter/gsheets_sync.py` — high-level domain logic:
  - `init_or_load_spreadsheet()` — at bot startup, ensure ID exists in `.env`, else create + migrate.
  - `mirror_apply(row: dict)` — called after a successful apply.
  - `mirror_skip(row: dict)`, `mirror_fail(row: dict)`, `mirror_manual(row: dict)`, `mirror_expired(row: dict)`.
  - `pull_sent_marks()` — read Sheet, find rows where `Sent` differs from local cache, write diffs back to Excel + cache.
  - `resync_dirty()` — retry failed writes.
- `tools/gsheets_auth.py` — one-time OAuth flow, mirrors `tools/gmail_auth.py`.
- `tests/test_gsheets_client.py` — unit tests with mocked API.
- `tests/test_gsheets_sync.py` — sync logic tests.

### Modified files

- `hunter/config.py` — add:
  - `GSHEETS_ENABLED` (default `False` until user is ready)
  - `GSHEETS_TRACKER_ID` (env, may be empty)
  - `GSHEETS_DRIVE_FOLDER_ID` (optional — to organize created file in a specific Drive folder)
  - `GSHEETS_REFRESH_INTERVAL_MIN` (default 30)
  - `GSHEETS_CREDENTIALS_FILE`, `GSHEETS_TOKEN_FILE` paths
- `hunter/tracker.py` — add hook calls to `gsheets_sync` after every successful Excel write. Wrap with `try/except` so Sheets failure never blocks Excel write.
- `hunter/services/tracker_service.py` — same hook pattern.
- `hunter/telegram_bot.py`:
  - Rewrite `/unsent` to count rows in cache where `Sent` is empty (no to_send dependency).
  - Rewrite `/sync_sent` to call `gsheets_sync.pull_sent_marks()` instead of reading to_send.xlsx.
  - Register schedule: `gsheets_resync_dirty` every 5 min.
  - Register schedule: `gsheets_refresh` every 30 min.
  - Optional new command `/gsheets_status` showing ID, last sync timestamps, dirty row count.
- `hunter/to_send.py` — **delete** (no longer needed).
- `hunter/main.py` — remove `to_send.sync_and_rebuild()` calls at start of hunt cycle.
- `hunter/app.py` (or startup hook) — call `gsheets_sync.init_or_load_spreadsheet()` on boot.
- `requirements.txt` — no new deps (Google libs already present).
- `docker-compose.yml` — mount `gsheets_*.json`, remove `to_send.xlsx` volume.
- `.gitignore` — add `gsheets_credentials.json`, `gsheets_token.json`.

### Deleted concept

- `to_send.xlsx` (file): no longer created or maintained. Replaced by the "Unsent only" filter view in the Sheet.
- `hunter/to_send.py` (module): deleted.
- `hunter/expired_to_send_check.py`: review — likely can be merged into `expired_check.py` since there's no separate to_send anymore.

### Backward compat

For users with existing `to_send.xlsx` on disk: leave the file alone. Just stop reading/writing to it. They can delete manually if they want.

---

## 5. In-memory cache

```python
# hunter/tracker_cache.py (new)
class TrackerCache:
    rows: dict[str, dict]      # ID -> row dict
    by_url: dict[str, str]     # normalized_url -> ID
    by_ctkey: dict[str, str]   # company+title key -> ID
    dirty_ids: set[str]        # rows that failed to push to Sheets

    def load_from_excel(self) -> None
    def add(self, row: dict) -> None
    def update_sent(self, row_id: str, sent_value: str) -> None
    def update_status(self, row_id: str, ats_value: str) -> None
    def is_known_url(self, url: str) -> bool
    def is_known_ct(self, company: str, title: str) -> bool
    def unsent_count(self) -> int
    def unsent_angular_count(self) -> int
```

- Loaded once at startup from `tracker.xlsx`.
- Every write to Excel also updates cache.
- `filters.dedup` reads from cache instead of re-opening Excel each hunt.
- `/unsent` reads counts from cache.
- This is a side benefit of the refactor — much faster hunts.

---

## 6. Migration (first run)

When bot starts and `GSHEETS_ENABLED=true` but `GSHEETS_TRACKER_ID` is empty:

1. Create new Spreadsheet `Job Hunter — Tracker (yyyy-mm-dd)` via Sheets API.
2. Read local `tracker.xlsx` → upload all rows in batch (one `values.update` call).
3. Format header row (bold, frozen).
4. Send Telegram message:
   ```
   ✅ Google Sheets tracker initialized
   📊 https://docs.google.com/spreadsheets/d/<id>

   Add this ID to .env on the server:
   GSHEETS_TRACKER_ID=<id>

   Then restart the bot.

   Tip: in the Sheet, go to Data → Create filter view →
   filter "Sent" column "Is empty" → save as "Unsent only".
   ```
5. Bot exits cleanly (so user can update `.env` and restart).

Once ID is in `.env`, all subsequent starts skip migration and just verify the ID is accessible.

---

## 7. Implementation phases

### Phase 1 — Auth + client (LOW risk)
- [ ] 1.1 Create `tools/gsheets_auth.py` (clone of gmail_auth.py with new scopes)
- [ ] 1.2 Create `hunter/gsheets_client.py` (low-level wrapper)
- [ ] 1.3 Unit tests for client with mocked API
- [ ] 1.4 Manual test: run auth tool locally, verify token works

### Phase 2 — In-memory cache (LOW risk, big perf win)
- [ ] 2.1 Create `hunter/tracker_cache.py`
- [ ] 2.2 Load cache at bot startup
- [ ] 2.3 Wire all `is_known()` / `is_known_ct()` calls through cache
- [ ] 2.4 Wire all tracker writes to update cache
- [ ] 2.5 Tests: cache consistency after add/update/remove

### Phase 3 — Drop to_send (MEDIUM risk — touches several call sites)
- [ ] 3.1 Rewrite `/unsent` to use cache
- [ ] 3.2 Rewrite `/sync_sent` (placeholder — will hook into Sheets in Phase 4)
- [ ] 3.3 Remove `to_send.sync_and_rebuild()` calls from `hunter/main.py`
- [ ] 3.4 Remove imports of `hunter.to_send` everywhere
- [ ] 3.5 Delete `hunter/to_send.py` + `hunter/expired_to_send_check.py` (merge logic into `expired_check.py` if needed)
- [ ] 3.6 Remove `to_send.xlsx` mount from docker-compose.yml
- [ ] 3.7 Update all tests that reference to_send
- [ ] 3.8 Manual test: bot still works locally, /unsent shows correct count

### Phase 4 — Sheets mirror (MEDIUM risk)
- [ ] 4.1 Create `hunter/gsheets_sync.py` with `mirror_*` functions
- [ ] 4.2 Hook `mirror_apply` after successful applies
- [ ] 4.3 Hook `mirror_skip/fail/manual/expired` for status changes
- [ ] 4.4 Wrap all calls with try/except → set `gsheets_dirty=True` on cache row
- [ ] 4.5 Background `resync_dirty` task every 5 min
- [ ] 4.6 Tests: sync hooks called with right data, failures don't break Excel writes

### Phase 5 — Sheets → Excel pull (MEDIUM risk)
- [ ] 5.1 Implement `pull_sent_marks()`
- [ ] 5.2 Rewire `/sync_sent` to call Sheets pull
- [ ] 5.3 Schedule periodic auto-pull every 30 min
- [ ] 5.4 Tests: user edits Sent in Sheets → bot picks up on next pull

### Phase 6 — Bootstrap migration (LOW-MEDIUM risk)
- [ ] 6.1 Implement `init_or_load_spreadsheet()`
- [ ] 6.2 First-run flow: detect empty `GSHEETS_TRACKER_ID`, create file, migrate data, message user, exit
- [ ] 6.3 Manual test on staging copy of `tracker.xlsx`

### Phase 7 — Production rollout (LOW risk if phases 1–6 green)
- [ ] 7.1 PR feature branch → develop, merge
- [ ] 7.2 PR develop → master, merge (use **Create a merge commit**, not squash)
- [ ] 7.3 SCP `gsheets_credentials.json` + `gsheets_token.json` to VPS
- [ ] 7.4 Set `GSHEETS_ENABLED=true` in `.env` (with empty ID)
- [ ] 7.5 Deploy → bot creates Sheet, messages ID
- [ ] 7.6 Add ID to `.env`, restart, verify mirror works on next apply
- [ ] 7.7 Create "Unsent only" filter view in the browser

---

## 8. Risks & open questions

| Risk | Mitigation |
|------|-----------|
| Google API quota (60 reads/min, 100 writes/min per project) | Batch writes (one call per apply, not per cell). Cache reads (only periodic refresh every 30 min). |
| OAuth token expires / refresh fails on server | Same as Gmail — token file gets refreshed automatically. Alert on Telegram if refresh fails. |
| User edits multiple cells while bot is writing | Bot only appends new rows or updates specific cells (Sent is user-only). No range overwrites. |
| Sheets write fails repeatedly → many dirty rows | `/gsheets_status` shows dirty count. Manual `/gsheets_resync` command for force retry. |
| Migration fails halfway (e.g. quota) | Idempotent — re-running migration is safe (uses ID column for upsert detection). |
| Spreadsheet gets accidentally deleted by user | Bot logs the error, sends Telegram alert. Excel remains intact, can re-migrate. |
| Removing to_send breaks existing tests | Phase 3 explicitly catches all references; run full test suite after each sub-step. |
| User used to two-file workflow | Document the filter view in the Telegram message at first run + in CLAUDE.md. |

---

## 9. Out of scope (for this branch)

- Google Drive for `Applications/` folder uploads — separate task after Sheets lands.
- Replacing Excel entirely — Excel remains as safety net long-term.
- Real-time sync via webhooks/push notifications — polling every 30 min is enough.
- Sharing the spreadsheet with other users — manual via Google UI.

---

## 10. Acceptance criteria

A pass means **all** of:

1. Bot starts, creates Sheet on first run with empty ID, messages user with link + filter-view instructions.
2. User adds ID to `.env`, restarts, bot continues normally.
3. Every apply writes to both `tracker.xlsx` and Sheets within 5 seconds.
4. If Sheets API is offline, bot keeps working with Excel-only and logs the failure; dirty rows retry every 5 min.
5. User edits `Sent` column in Google Sheets browser → within 30 min (or on `/sync_sent`), the value lands in local Excel + cache.
6. `/gsheets_status` shows: ID, last refresh time, dirty row count.
7. `/unsent` shows correct count from cache (no to_send file involved).
8. Existing tests (35 files) still pass + new tests for cache/client/sync.
9. `to_send.xlsx` and `hunter/to_send.py` no longer exist in the repo.
10. Documentation in `CLAUDE.md` updated with the new architecture section.
