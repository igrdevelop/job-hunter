"""hunter/schedules/bot_commands.py — drain the web control bar's command
queue (pipeline control plan, PR 1 (b)).

The site's /pipeline page (owner only) inserts a `bot_commands` row through
job-hunter-api; this tick claims it every 3 s ON THE BOT'S OWN EVENT LOOP —
not in a thread like the profile_jobs drain — because the work it launches
(`run_hunt`, `run_retry_failed`, the expired check) is the bot's own async
code, serialized through the in-process `_hunt_lock`. A thread with its own
loop could not share that lock.

Per claimed row:
  1. Owner re-check. `user_id` must equal DEFAULT_USER_ID (the same rule as
     hunter/bot/auth.py::is_owner; the API checks too, this is the second
     line). An empty DEFAULT_USER_ID is single-user dev mode: accept.
  2. Kind whitelist: `hunt {sources: [..] | null}`, `retry_failed {}`,
     `check_expired {}`; anything else -> rejected.
  3. `hunt`: source names validated with the /hunt command's own parser
     (hunter/commands/hunt.py::parse_hunt_source_args) against ALL_SOURCES.
  4. Busy -> rejected, never queued (owner decision 2026-09-24: the page
     disables its buttons while a hunt runs; the bot enforces the same rule).
     `hunt` and `retry_failed` share `_hunt_lock`, so either is busy while
     the lock is held OR while a web-launched hunt/retry task has not yet
     acquired it (two rows claimed in the same tick must not both pass).
     `check_expired` has its own in-process guard.
  5. Launch the work with `context.application.create_task(...)` and return
     at once — a hunt takes minutes, the tick must not. The task stamps
     done / error when the work returns or raises (or is cancelled).

Telegram still gets the normal hunt / retry / expired report — the phone
stays the owner's source of truth; the web command adds a one-line notice.

Gated by BOT_COMMANDS_ENABLED. The tick runs inside
`best_effort("bot.commands")` so a broken table alerts instead of failing
silently every 3 s forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hunter import bot_commands

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Web-launched tasks still in flight, per busy group. A task removes itself
# when it finishes (add_done_callback). Module-level: one bot process.
_lock_tasks: set[asyncio.Task] = set()  # hunt + retry_failed (share _hunt_lock)
_expired_tasks: set[asyncio.Task] = set()  # check_expired

REASON_NOT_OWNER = "not the owner"
REASON_HUNT_BUSY = "hunt already running"
REASON_EXPIRED_BUSY = "check_expired already running"
REASON_AUTO_APPLY_OFF = "AUTO_APPLY is off — retries only run in auto-apply mode"


def _owner_user_id() -> str:
    from hunter import config

    return config.DEFAULT_USER_ID


def _hunt_busy() -> bool:
    """True while a hunt or a retry pass holds (or is about to take) _hunt_lock."""
    from hunter import main

    return main._hunt_lock.locked() or any(not t.done() for t in _lock_tasks)


def _expired_busy() -> bool:
    """True while a web check is in flight or ANY expired check (incl. the
    nightly job) is running in this process."""
    from hunter.schedules.check_expired import is_running

    return is_running() or any(not t.done() for t in _expired_tasks)


def _parse_payload(raw: Any) -> dict | None:
    """The payload as a dict; '' / NULL count as {}. None when it is not a
    JSON object."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _hunt_sources(payload: dict) -> tuple[list[str] | None, str | None]:
    """(source_names or None for all, rejection reason or None)."""
    from hunter.commands.hunt import parse_hunt_source_args
    from hunter.sources import ALL_SOURCES

    raw = payload.get("sources")
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        return None, "invalid payload: sources must be a list of names or null"
    valid = {s.name for s in ALL_SOURCES}
    names, unknown = parse_hunt_source_args(raw, valid)
    if unknown:
        return None, f"unknown source(s): {', '.join(unknown)}"
    return names, None


async def _db(fn: Callable[..., Any], *args: Any) -> None:
    """One bot_commands write off the loop, best-effort (a failed stamp must
    never crash the launched work or the tick)."""
    from hunter.best_effort import best_effort

    with best_effort("bot.commands"):
        await asyncio.to_thread(fn, *args)


async def _notify(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        from hunter.telegram_bot import send_text

        await send_text(context, text)
    except Exception as e:  # noqa: BLE001 — a notice must never break a command
        logger.warning("[bot_commands] Telegram notice failed: %s", e)


async def _execute(
    context: ContextTypes.DEFAULT_TYPE,
    command_id: str,
    kind: str,
    work: Callable[[], Awaitable[str | None]],
) -> None:
    """Run the work and stamp the terminal status whatever happens."""
    try:
        result = await work()
    except asyncio.CancelledError:
        await _db(bot_commands.fail, command_id, "cancelled")
        raise
    except Exception as e:  # noqa: BLE001 — every failure becomes an error row
        logger.exception("[bot_commands] %s %s failed", kind, command_id)
        await _db(bot_commands.fail, command_id, f"{type(e).__name__}: {e}")
        await _notify(context, f"⚠️ <b>Web command {kind} failed</b>\n<pre>{str(e)[:300]}</pre>")
        return
    await _db(bot_commands.finish, command_id, result or "")
    logger.info("[bot_commands] %s %s done", kind, command_id)


def _launch(
    context: ContextTypes.DEFAULT_TYPE,
    group: set[asyncio.Task],
    command_id: str,
    kind: str,
    work: Callable[[], Awaitable[str | None]],
) -> None:
    task = context.application.create_task(
        _execute(context, command_id, kind, work), name=f"bot_command_{kind}_{command_id}"
    )
    group.add(task)
    task.add_done_callback(group.discard)


async def dispatch(context: ContextTypes.DEFAULT_TYPE, row: dict) -> str:
    """Validate one claimed row and launch it, or reject it.

    Returns "launched" or "rejected: <reason>" (tests / logs).
    """
    command_id = str(row.get("id") or "")
    kind = str(row.get("kind") or "")

    async def _reject(reason: str) -> str:
        logger.warning("[bot_commands] rejected %s %s: %s", kind, command_id, reason)
        await asyncio.to_thread(bot_commands.reject, command_id, reason)
        return f"rejected: {reason}"

    owner = _owner_user_id()
    if owner and str(row.get("user_id") or "") != owner:
        return await _reject(REASON_NOT_OWNER)
    if kind not in bot_commands.KINDS:
        return await _reject(f"unknown kind: {kind or '(empty)'}")
    payload = _parse_payload(row.get("payload"))
    if payload is None:
        return await _reject("invalid payload: not a JSON object")

    if kind == bot_commands.KIND_HUNT:
        source_names, why = _hunt_sources(payload)
        if why:
            return await _reject(why)
        if _hunt_busy():
            return await _reject(REASON_HUNT_BUSY)

        async def _hunt() -> str | None:
            from hunter.main import run_hunt

            label = ", ".join(source_names) if source_names else "all sources"
            await _notify(context, f"🌐 Web: running hunt ({label})")
            await run_hunt(context, source_names, trigger="web", command_id=command_id)
            return None

        _launch(context, _lock_tasks, command_id, kind, _hunt)
        return "launched"

    if kind == bot_commands.KIND_RETRY_FAILED:
        from hunter import main

        if not main.AUTO_APPLY:
            return await _reject(REASON_AUTO_APPLY_OFF)
        if _hunt_busy():
            return await _reject(REASON_HUNT_BUSY)

        async def _retry() -> str | None:
            await _notify(context, "🌐 Web: retrying failed jobs")
            await main.run_retry_failed(context, command_id=command_id)
            return None

        _launch(context, _lock_tasks, command_id, kind, _retry)
        return "launched"

    # check_expired
    if _expired_busy():
        return await _reject(REASON_EXPIRED_BUSY)

    async def _expired() -> str | None:
        from hunter.schedules.check_expired import run_expired_check_and_report

        await _notify(context, "🌐 Web: checking tracker for expired vacancies…")
        result = await run_expired_check_and_report(
            context,
            header="🌐 <b>Expired check (web)</b>\n",
            report_when_nothing_expired=True,
        )
        return json.dumps(
            {
                "total": result.get("total", 0),
                "expired": len(result.get("expired") or []),
                "errors": len(result.get("errors") or []),
            }
        )

    _launch(context, _expired_tasks, command_id, kind, _expired)
    return "launched"


async def drain_once(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Claim and dispatch every pending row. Returns how many were claimed.
    Raises on a broken DB (the scheduled wrapper's best_effort counts it)."""
    n = 0
    while True:
        row = await asyncio.to_thread(bot_commands.claim_next)
        if row is None:
            return n
        n += 1
        await dispatch(context, row)


async def scheduled_bot_commands_drain(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback, every 3 s (hunter/schedules/__init__.py)."""
    from hunter import config
    from hunter.best_effort import best_effort

    if not config.BOT_COMMANDS_ENABLED:
        return
    with best_effort("bot.commands"):
        await drain_once(context)
