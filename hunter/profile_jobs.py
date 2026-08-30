"""hunter/profile_jobs.py — atomic claim/finish/fail primitives over the
`profile_jobs` table (docs/RESUME_PROFILE_STORE_PLAN.md step 4b).

Contract source: job-hunter-api/docs/RESUME_PROFILE_STORE.md's "Shared
contract" section — the API writes a row into the shared tracker.db (PUT
/api/profile -> kind='render', POST /api/profile/uploads -> kind='parse')
and this module is the bot's sole consumer. Same precedent as
hunter/users.py's telegram_link_codes: the API writes, the bot claims and
resolves; DDL lives in hunter/db.py and must not change unilaterally here.

Statuses: pending -> running -> done | error. `error` is terminal — a retry
is a new job the client creates by re-submitting (PUT/upload again), not
something this module resurrects on its own. A `running` row whose
`updated_at` is older than a timeout is assumed to belong to a drain tick
that crashed mid-job and is reset back to `pending` by
reset_stale_profile_jobs() (crash recovery, same idea as
hunter.tracker.reset_stale_claims for the apply queue).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.tracker.DB_PATH / hunter.best_effort.DB_PATH).
DB_PATH: Path = TRACKER_DB_PATH

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_next_profile_job() -> dict | None:
    """Atomically claim the oldest pending job: status -> running,
    updated_at stamped to now.

    A single UPDATE...RETURNING statement (SQLite >= 3.35) — the row
    selection and the status flip happen in one atomic write, mirroring
    hunter.tracker.claim_pending's own primitive for the apply queue.
    Ordered by `created_at` (the API stamps it, ISO-8601 UTC) with `rowid`
    as a tiebreaker for same-timestamp inserts. Returns the full row as a
    dict, or None when the queue is empty.
    """
    now = _now_iso()
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            """
            UPDATE profile_jobs
            SET status='running', updated_at=?
            WHERE id = (
                SELECT id FROM profile_jobs
                WHERE status='pending'
                ORDER BY created_at, rowid
                LIMIT 1
            )
            RETURNING *
            """,
            (now,),
        ).fetchone()
    return dict(row) if row else None


def finish_profile_job(job_id: str, result: str) -> None:
    """running -> done, storing the job's output in `result` (the written-
    file list for 'render', the draft profile JSON for 'parse')."""
    with get_db(DB_PATH) as conn:
        conn.execute(
            "UPDATE profile_jobs SET status=?, result=?, updated_at=? WHERE id=?",
            (STATUS_DONE, result, _now_iso(), job_id),
        )


def fail_profile_job(job_id: str, error: str) -> None:
    """running -> error (terminal). The client sees `error` on its next poll
    of GET /api/profile/jobs/:id and retries by re-submitting — this module
    never auto-retries a failed job."""
    with get_db(DB_PATH) as conn:
        conn.execute(
            "UPDATE profile_jobs SET status=?, error=?, updated_at=? WHERE id=?",
            (STATUS_ERROR, str(error)[:2000], _now_iso(), job_id),
        )


def reset_stale_profile_jobs(timeout_min: int) -> int:
    """running -> pending for rows whose updated_at is older than
    timeout_min minutes ago — a drain tick that died mid-job leaves its row
    stuck `running` forever otherwise. Returns the number of rows reset. A
    row with a NULL updated_at (should not happen via claim_next_profile_job,
    but defensive) is left alone — there's no age to compare."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_min)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_db(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE profile_jobs SET status=?, updated_at=NULL "
            "WHERE status=? AND updated_at IS NOT NULL AND updated_at < ?",
            (STATUS_PENDING, STATUS_RUNNING, cutoff),
        )
        return cur.rowcount
