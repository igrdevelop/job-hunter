#!/usr/bin/env bash
# scripts/restore_drill.sh — prove the restic backup is actually restorable.
#
# A backup nobody has ever restored is a hope, not a backup. This script
# restores the LATEST restic snapshot into a scratch temp dir and runs
# `sqlite3 ... "pragma integrity_check"` against every .db file it finds —
# the same check hunter/tracker_backup.py already runs on the LOCAL copy at
# backup time (see hunter/tracker_backup.py::_verify_integrity), but here
# against what restic actually has in the bucket, end to end.
#
# Read-only against the restic repo (a `restic restore`, never a live-system
# mutation) and writes only inside its own scratch temp dir, which it
# deletes on exit — safe to run on a schedule (e.g. monthly, alongside the
# owner's own manual check) or by hand before trusting a restore in an
# actual incident.
#
# Required env (same as scripts/offhost_backup.sh):
#   RESTIC_REPOSITORY
#   RESTIC_PASSWORD_FILE
#
# Optional env:
#   SNAPSHOT   restic snapshot id to restore (default: latest)
#
# Exit code: non-zero if the restore itself fails, or if ANY restored .db
# file fails integrity_check.

set -euo pipefail

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set (see script header)}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE must be set (see script header)}"

SNAPSHOT="${SNAPSHOT:-latest}"

command -v restic >/dev/null 2>&1 || {
  echo "restore_drill: FAILED: restic binary not found on PATH" >&2
  exit 1
}
command -v sqlite3 >/dev/null 2>&1 || {
  echo "restore_drill: FAILED: sqlite3 binary not found on PATH" >&2
  exit 1
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

echo "restore_drill: restoring snapshot '${SNAPSHOT}' into ${scratch}"
if ! restic restore "${SNAPSHOT}" --target "${scratch}"; then
  echo "restore_drill: FAILED: restic restore failed" >&2
  exit 1
fi

# Find every restored sqlite file and integrity-check it. Bot backups are
# named tracker_db_*.db / app_sqlite_*.db (hunter/tracker_backup.py); this
# also covers any other *.db restic picked up under the backed-up dirs.
mapfile -d '' -t db_files < <(find "${scratch}" -type f -name '*.db' -print0)

if [ "${#db_files[@]}" -eq 0 ]; then
  echo "restore_drill: FAILED: no .db files found in the restored snapshot" >&2
  exit 1
fi

failures=0
for f in "${db_files[@]}"; do
  result="$(sqlite3 "$f" "PRAGMA integrity_check;" 2>&1 || true)"
  if [ "$result" = "ok" ]; then
    echo "restore_drill: OK   $f"
  else
    echo "restore_drill: FAIL $f -> $result" >&2
    failures=$((failures + 1))
  fi
done

if [ "$failures" -gt 0 ]; then
  echo "restore_drill: FAILED: ${failures} of ${#db_files[@]} restored db file(s) failed integrity_check" >&2
  exit 1
fi

echo "restore_drill: OK — ${#db_files[@]} restored db file(s) passed integrity_check"
exit 0
