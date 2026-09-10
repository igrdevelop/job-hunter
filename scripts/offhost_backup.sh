#!/usr/bin/env bash
# scripts/offhost_backup.sh — off-host restic backup for the VPS.
#
# hunter/tracker_backup.py only ever writes LOCAL snapshots under
# ./backups on the same disk as the live data — a dead disk or a wiped VPS
# takes both the original and the "backup" with it. This script is the
# off-host half (docs/improvement-2026-09/06-OPS_PLAN.md M1): a restic
# snapshot of the bot's data directories (+ optionally job-hunter-api's) to
# an S3-compatible bucket (Backblaze B2 / Hetzner Object Storage / anything
# restic supports).
#
# Intended to run from cron on the VPS (see docs/DEPLOY.md "Backups" for the
# crontab line + one-time `restic init`). This script does NOT run itself —
# it is committed for the owner to install, never executed automatically by
# an agent.
#
# Required env (fail fast if unset):
#   RESTIC_REPOSITORY     restic repo target, e.g. s3:https://s3.<region>.backblazeb2.com/<bucket>
#   RESTIC_PASSWORD_FILE  path to a file holding the repo encryption password
#
# Optional env:
#   BOT_DATA_DIR      default /home/deploy/job-hunter — backs up
#                      $BOT_DATA_DIR/{db,users,backups}
#   API_DATA_DIR       job-hunter-api's data dir (app.sqlite + any per-user
#                      files it owns separately from $BOT_DATA_DIR/users).
#                      Unset/missing = skipped, not a failure — plenty of
#                      hosts run only the bot.
#   KEEP_DAILY          default 30  (restic forget --keep-daily)
#   KEEP_WEEKLY          default 12  (restic forget --keep-weekly)
#   BACKUP_PING_URL      optional dead-man's-switch URL (e.g. healthchecks.io);
#                        pinged with curl on success/failure, best-effort —
#                        a ping failure never fails the backup itself.
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / RESTIC_REPOSITORY etc. — any
#                        other restic/backend env vars restic itself reads
#                        (see restic docs for the target backend), passed
#                        straight through.
#
# Exit code: non-zero on any restic failure (backup or forget/prune) —
# wire this into cron with a mailer, or rely on BACKUP_PING_URL.

set -euo pipefail

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set (see script header)}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE must be set (see script header)}"

BOT_DATA_DIR="${BOT_DATA_DIR:-/home/deploy/job-hunter}"
KEEP_DAILY="${KEEP_DAILY:-30}"
KEEP_WEEKLY="${KEEP_WEEKLY:-12}"

_ping() {
  # $1: URL suffix ("" for success, "/fail" for failure). Best-effort —
  # never let a dead-man's-switch outage fail the backup job itself.
  local suffix="${1:-}"
  if [ -n "${BACKUP_PING_URL:-}" ]; then
    curl -fsS -m 10 --retry 2 -o /dev/null "${BACKUP_PING_URL}${suffix}" || true
  fi
}

_fail() {
  echo "offhost_backup: FAILED: $*" >&2
  _ping "/fail"
  exit 1
}

command -v restic >/dev/null 2>&1 || _fail "restic binary not found on PATH"

paths=()
for sub in db users backups; do
  d="${BOT_DATA_DIR}/${sub}"
  if [ -d "$d" ]; then
    paths+=("$d")
  else
    echo "offhost_backup: WARNING: expected bot data dir missing, skipping: $d" >&2
  fi
done

if [ "${#paths[@]}" -eq 0 ]; then
  _fail "none of \$BOT_DATA_DIR/{db,users,backups} exist under $BOT_DATA_DIR — nothing to back up"
fi

if [ -n "${API_DATA_DIR:-}" ]; then
  if [ -d "${API_DATA_DIR}" ]; then
    paths+=("${API_DATA_DIR}")
  else
    echo "offhost_backup: API_DATA_DIR set but missing, skipping: ${API_DATA_DIR}" >&2
  fi
fi

echo "offhost_backup: backing up: ${paths[*]}"
if ! restic backup "${paths[@]}"; then
  _fail "restic backup failed"
fi

echo "offhost_backup: pruning (keep-daily=${KEEP_DAILY} keep-weekly=${KEEP_WEEKLY})"
if ! restic forget --keep-daily "${KEEP_DAILY}" --keep-weekly "${KEEP_WEEKLY}" --prune; then
  _fail "restic forget/prune failed"
fi

echo "offhost_backup: done"
_ping ""
exit 0
