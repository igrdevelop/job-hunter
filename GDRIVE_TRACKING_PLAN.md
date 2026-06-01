# Plan: Google Drive Upload Tracking

## Problem

After each apply, the bot attempts to upload the application folder to Drive.
If the upload fails (network blip, API error, timeout), the error is silently
swallowed and never retried. Because tracker.xlsx has no "Drive URL" column,
the system cannot distinguish uploaded rows from not-yet-uploaded ones.
The user must run `/gdrive_upload_missing` manually to catch missed uploads.

---

## Goal

- Store the Drive folder URL in tracker.xlsx after every successful upload.
- `upload_missing_folders` skips rows that already have a Drive URL.
- The scheduled job becomes a true retry for only the rows that failed earlier.
- Show the Drive link on the Telegram card when a row already has one.

---

## Tracker Schema Change

Add a new column at the end of the existing schema:

| Col | Name      | Description                             |
|-----|-----------|-----------------------------------------|
| 12  | Drive URL | Google Drive folder URL, or blank/dash  |

All existing constants in `hunter/tracker.py` are index-based (`COL_*`), so
adding column 12 does not shift any existing column.

---

## Implementation Steps

### Step 1 — `hunter/tracker.py`: add `COL_DRIVE_URL` constant + two functions

```
COL_DRIVE_URL = 12  # new column

def get_drive_url_by_url(job_url: str) -> str | None
    # Read tracker, find row by URL, return Drive URL cell value (or None)

def set_drive_url(job_url: str, drive_url: str) -> None
    # Open tracker, find row by URL, write drive_url to COL_DRIVE_URL
```

Header row: add `"Drive URL"` to the header tuple where it is written
(currently set in `add_applied` and bootstrap logic — search for where the
header row is written and append there too).

### Step 2 — `hunter/gdrive_sync.py`: write URL to tracker after upload

In `upload_application_folder`, after a successful `_do_upload`:

```python
from hunter.tracker import set_drive_url
await asyncio.to_thread(set_drive_url, job_url, url)
```

The function signature needs a `job_url: str | None = None` parameter added
so callers that don't have the URL (bulk scan) can omit it.

In `upload_missing_folders`, after each successful upload, also call
`set_drive_url(row["URL"], url)` so the bulk command marks rows as done.

### Step 3 — `hunter/gdrive_sync.py`: skip already-uploaded rows

In `upload_missing_folders`, before adding a row to `to_upload`:

```python
if row.get("Drive URL", "").strip() not in ("", "-", "—"):
    already_uploaded += 1
    continue
```

Return `already_uploaded` count in the result dict.

### Step 4 — Call sites: pass `job_url` to `upload_application_folder`

Update every call to `upload_application_folder` to pass the job URL:

- `hunter/telegram_bot.py:719` — `url` variable is already in scope
- `hunter/main.py:293` — `job.url` is available

### Step 5 — `hunter/telegram_bot.py`: update `/gdrive_upload_missing` reply

Show the new `already_uploaded` count in the command response:

```
✅ gdrive_upload_missing
Uploaded:          5
Already on Drive:  42
Skipped (no local folder): 3
Errors: 0
```

### Step 6 — tests

- `test_tracker_drive_url.py`: `get_drive_url_by_url`, `set_drive_url`
- `test_gdrive_sync_tracking.py`:
  - `upload_application_folder` writes Drive URL to tracker on success
  - `upload_missing_folders` skips rows with existing Drive URL
  - `upload_missing_folders` writes Drive URL for newly uploaded rows

---

## Files Changed

| File | Change |
|------|--------|
| `hunter/tracker.py` | `COL_DRIVE_URL`, `get_drive_url_by_url`, `set_drive_url`, header row |
| `hunter/gdrive_sync.py` | `job_url` param, write Drive URL after upload, skip already-uploaded in bulk |
| `hunter/telegram_bot.py` | pass `url` to `upload_application_folder`; show `already_uploaded` in reply |
| `hunter/main.py` | pass `job.url` to `upload_application_folder` |
| `tests/test_tracker_drive_url.py` | new test file |
| `tests/test_gdrive_sync_tracking.py` | new test file |
| `CLAUDE.md` | update tracker schema table (col 12) |

---

## Risks & Notes

- **Existing tracker.xlsx rows** have no Drive URL column — blank cells are
  treated as "not uploaded", so first `/gdrive_upload_missing` run will upload
  everything as before. After that, only genuinely missing rows are retried.
- **No migration script needed** — openpyxl writes to the new column; blank
  cells in existing rows are fine.
- **Schema documentation** — CLAUDE.md tracker table must be updated in the
  same commit (per project rules).
- **`set_drive_url` must be safe to call multiple times** (idempotent) — Drive
  deduplicates by file name, and tracker just overwrites the cell.
