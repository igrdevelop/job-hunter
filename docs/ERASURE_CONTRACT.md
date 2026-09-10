# Erasure contract (bot side)

docs/improvement-2026-09/07-COMPLIANCE_PLAN.md risk #1 ("right to erasure"),
milestone M1. This document is the bot-repo half of the contract — the
API-side call (admin.deleteUser enqueueing this, plus its own
`email_verification_tokens` cleanup) is a separate change in job-hunter-api,
tracked as a follow-up to this PR.

## What lives where

- `hunter/erasure.py::erase_user(user_id, *, dry_run=False, force_owner=False,
  exclude_job_id=None) -> ErasureReport` — the actual work: one sqlite
  transaction deleting every row scoped to `user_id` from every table with a
  `user_id` column (discovered via `PRAGMA table_info`, not a hardcoded
  list), then `shutil.rmtree(users/{uid}/)`, then a best-effort name-only
  sweep of `logs/` for files whose filename contains the uid.
- `hunter/schedules/profile_jobs.py` — a fourth `profile_jobs.kind`, `'erase'`,
  alongside `render`/`parse`/`preview`. The drain loop (every ~20s,
  `hunter/schedules/profile_jobs.py::drain_once`) claims and processes it the
  same way as every other kind.
- `tools/erase_user.py` — an owner-run CLI seam for the same function, for a
  support request handled without an API round trip.

## Job payload / result shape (same pattern as render/parse/preview)

Enqueue (mirrors PUT /api/profile -> kind='render' etc. in
job-hunter-api/docs/RESUME_PROFILE_STORE.md's "Shared contract" section — the
API is expected to write a `profile_jobs` row the same way, once it wires up
the admin-erasure call):

```json
{
  "id": "<uuid>",
  "user_id": "<uid to erase>",
  "kind": "erase",
  "payload": "{}",
  "status": "pending",
  "created_at": "<ISO-8601 UTC>"
}
```

`payload` is JSON `{}` (the common case) or `{"force_owner": true}` to allow
erasing `DEFAULT_USER_ID` itself — never expected from the API in normal
operation, kept for symmetry/testing since `hunter.erasure.erase_user` itself
supports it.

Result, written to the job's `result` column on success (`status='done'`):

```json
{
  "user_id": "<uid>",
  "dry_run": false,
  "tables": {"applications": 3, "profile_jobs": 0, "telegram_link_codes": 0,
             "telegram_links": 1, "user_settings": 2},
  "files": 14,
  "bytes": 483920,
  "users_dir_removed": true,
  "log_files_removed": []
}
```

`tables` lists EVERY table that had a `user_id` column at run time, including
ones with zero matching rows — a caller can diff the key set against its own
expectations without a second query. `log_files_removed` is almost always
`[]` today: log filenames don't carry a uid yet (M3 in the compliance plan is
the milestone that adds it) — see hunter/erasure.py's module docstring.

On failure, `status='error'` and `error` carries the exception message — same
terminal-failure contract as every other `profile_jobs` kind. The bot never
auto-retries; a retry is the API enqueueing a new job.

## Why the job's own `profile_jobs` row survives the bulk delete

The `erase` job's row lives in the very table (`profile_jobs`) the deletion
targets, for the very user being erased. `erase_user(..., exclude_job_id=job_id)`
excludes that one row from the `profile_jobs` bulk delete, so
`hunter/schedules/profile_jobs.py::_process_job`'s normal
`finish_profile_job(job_id, result)` call — unchanged, same as every other
kind — is what writes the terminal `status='done'` + `result` onto it
afterwards. The alternative (stamp `done` before deleting other rows, skip
`finish_profile_job` for this kind) was rejected: it would make `kind='erase'`
the one code path with its own status-writing special case instead of
sharing `_process_job`'s single dispatch-then-finish shape.

## What this milestone does NOT cover

- Deleting `job-hunter-api`'s own tables (`users`, `profiles`,
  `profile_revisions`, `email_verification_tokens`) — that repo's
  `admin.deleteUser` already clears `users`/`profiles`/`profile_revisions`
  and the `users/{uid}/` tree; wiring it to also enqueue `kind='erase'` here
  (for the tracker/telegram/settings data it can't see) and to clear
  `email_verification_tokens` itself is the API-side follow-up.
- A self-service `DELETE /api/auth/me` endpoint (API side, also a follow-up).
- Backup retention. `backups/` (tracker.xlsx snapshots) is untouched by
  design — see the "Notes" section of the PR this doc shipped with for the
  retention caveat; docs/improvement-2026-09/06-OPS_PLAN.md M1's backup-
  retention milestone is what actually bounds how long an erased user's data
  can still exist in a backup.
- Log **content** cleanup (only filenames are matched, and only once they
  carry a uid — M3).
