"""hunter/bot_commands.py — atomic claim/finish/fail/reject primitives over
the shared `bot_commands` table (pipeline control plan, PR 1).

The site's /pipeline page (owner only) asks the bot to do something — hunt
every source, hunt one source, retry FAILed rows, check expired postings —
by inserting a row here through job-hunter-api (`POST /pipeline/commands`).
The bot is the sole consumer: hunter/schedules/bot_commands.py claims a row
every 3 s on the bot's own event loop, validates it and launches the work.
Same API-writes / bot-resolves precedent as hunter/profile_jobs.py; the DDL
lives in hunter/db.py (`_BOT_COMMANDS_DDL`) and is mirrored by the API's
tracker-migrations.ts — neither side changes it unilaterally.

Statuses: pending -> running -> done | error, or pending -> running ->
rejected when validation refuses the row (the reason goes into `error`).
`claim_next()` IS the pending -> running transition — one atomic
UPDATE...RETURNING, so a row can never be claimed twice and there is no
separate "mark running" write that could interleave with another claim.
A row still `running` when the bot process starts again belongs to a
process that died mid-command; `fail_orphaned_running()` stamps it `error`
("bot restarted") from `_post_init` — the work itself is not resumed, the
owner presses the button again.

Every function RAISES on a broken DB: the drain wraps its tick in
`best_effort("bot.commands")`, which needs the exception to count.
All timestamps are UTC `%Y-%m-%dT%H:%M:%S+00:00` (the shared contract).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hunter.config import TRACKER_DB_PATH
from hunter.db import ensure_bot_commands_table, get_db

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.profile_jobs.DB_PATH).
DB_PATH: Path = TRACKER_DB_PATH

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_REJECTED = "rejected"

KIND_HUNT = "hunt"
KIND_RETRY_FAILED = "retry_failed"
KIND_CHECK_EXPIRED = "check_expired"
KINDS: tuple[str, ...] = (KIND_HUNT, KIND_RETRY_FAILED, KIND_CHECK_EXPIRED)

_ERROR_MAX_LEN = 2000


def now_iso() -> str:
    """UTC now in the contract's `%Y-%m-%dT%H:%M:%S+00:00` shape."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def claim_next() -> dict | None:
    """Atomically claim the oldest pending command: status -> running,
    started_at stamped. Returns the full row as a dict, or None when nothing
    is pending.

    One UPDATE...RETURNING (SQLite >= 3.35), same shape as
    hunter.profile_jobs.claim_next_profile_job. Ordered by `created_at` (the
    API stamps it) with `rowid` as the tiebreaker for same-second inserts.
    """
    with get_db(DB_PATH) as conn:
        ensure_bot_commands_table(conn)
        row = conn.execute(
            """
            UPDATE bot_commands
            SET status=?, started_at=?
            WHERE id = (
                SELECT id FROM bot_commands
                WHERE status=?
                ORDER BY created_at, rowid
                LIMIT 1
            )
            RETURNING *
            """,
            (STATUS_RUNNING, now_iso(), STATUS_PENDING),
        ).fetchone()
    return dict(row) if row else None


def _terminal(command_id: str, status: str, *, result: str = "", error: str = "") -> None:
    with get_db(DB_PATH) as conn:
        ensure_bot_commands_table(conn)
        conn.execute(
            "UPDATE bot_commands SET status=?, result=?, error=?, finished_at=? WHERE id=?",
            (status, result, str(error)[:_ERROR_MAX_LEN], now_iso(), command_id),
        )


def finish(command_id: str, result: str = "") -> None:
    """running -> done. `result` is free text (a short JSON summary for the
    check_expired kind, empty otherwise)."""
    _terminal(command_id, STATUS_DONE, result=result)


def fail(command_id: str, error: str) -> None:
    """running -> error (terminal). The work raised or was cancelled."""
    _terminal(command_id, STATUS_ERROR, error=error)


def reject(command_id: str, reason: str) -> None:
    """running -> rejected (terminal). Validation refused the row before any
    work started: not the owner, unknown kind, bad source name, busy."""
    _terminal(command_id, STATUS_REJECTED, error=reason)


def fail_orphaned_running(reason: str = "bot restarted") -> int:
    """Every `running` row -> error. Called once at bot startup: nothing can
    be running in a process that has just started, so such a row belongs to
    the previous process. Returns the number of rows stamped."""
    with get_db(DB_PATH) as conn:
        ensure_bot_commands_table(conn)
        cur = conn.execute(
            "UPDATE bot_commands SET status=?, error=?, finished_at=? WHERE status=?",
            (STATUS_ERROR, reason, now_iso(), STATUS_RUNNING),
        )
        return cur.rowcount


def get(command_id: str) -> dict | None:
    """One row by id (tests, diagnostics)."""
    with get_db(DB_PATH) as conn:
        ensure_bot_commands_table(conn)
        row = conn.execute("SELECT * FROM bot_commands WHERE id=?", (command_id,)).fetchone()
    return dict(row) if row else None
