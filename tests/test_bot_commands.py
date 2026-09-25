"""Pipeline control plan, PR 1 — the `bot_commands` queue.

Part 1: the claim/finish/fail/reject primitives (hunter/bot_commands.py).
Part 2: the async drain (hunter/schedules/bot_commands.py) — owner re-check,
kind whitelist, source validation, the busy rule, launching the work with
`context.application.create_task` and stamping done/error.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hunter import bot_commands
from hunter import main as hunter_main
from hunter.db import ensure_bot_commands_table, get_db
from hunter.schedules import bot_commands as drain

# ── helpers ───────────────────────────────────────────────────────────────────


def _insert(
    cid: str,
    *,
    kind: str = "hunt",
    payload: object = None,
    user_id: str = "owner",
    created_at: str = "2026-09-25T10:00:00+00:00",
    status: str = "pending",
) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload or {})
    with get_db(bot_commands.DB_PATH) as conn:
        ensure_bot_commands_table(conn)
        conn.execute(
            "INSERT INTO bot_commands (id, user_id, kind, payload, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, user_id, kind, raw, status, created_at),
        )


def _row(cid: str) -> dict:
    row = bot_commands.get(cid)
    assert row is not None
    return row


class _Source:
    def __init__(self, name: str) -> None:
        self.name = name


SOURCES = [_Source("justjoin"), _Source("linkedin"), _Source("pracuj")]


def _context() -> SimpleNamespace:
    """A stand-in for PTB's CallbackContext: only `.application.create_task`
    (plain asyncio tasks) and `.bot` are used."""
    return SimpleNamespace(
        application=SimpleNamespace(
            create_task=lambda coro, name=None: asyncio.get_running_loop().create_task(
                coro, name=name
            )
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Owner configured, known sources, no Telegram, empty in-flight sets,
    best_effort counters in a temp DB."""
    from hunter import best_effort as be

    monkeypatch.setattr(be, "DB_PATH", tmp_path / "subsystem_health.db")
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner")
    monkeypatch.setattr("hunter.sources.ALL_SOURCES", SOURCES)
    monkeypatch.setattr(drain, "_notify", AsyncMock())
    drain._lock_tasks.clear()
    drain._expired_tasks.clear()
    yield
    drain._lock_tasks.clear()
    drain._expired_tasks.clear()


class _Gate:
    """Holds launched work open until the test has drained the whole tick —
    real work (a hunt, an expired check) outlives one tick by minutes; an
    instant fake would finish while drain_once awaits the next claim."""

    def __init__(self, return_value=None) -> None:
        self.event: asyncio.Event | None = None
        self.return_value = return_value
        self.mock = AsyncMock(side_effect=self._run)

    async def _run(self, *_a, **_kw):
        if self.event is None:
            self.event = asyncio.Event()
        await self.event.wait()
        return self.return_value

    def open(self) -> None:
        if self.event is None:
            self.event = asyncio.Event()
        self.event.set()


async def _settle() -> None:
    """Let launched tasks run to completion."""
    pending = list(drain._lock_tasks | drain._expired_tasks)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)
    await asyncio.sleep(0)


# ── Part 1: primitives ────────────────────────────────────────────────────────


def test_claim_next_is_fifo_and_flips_to_running() -> None:
    _insert("b", created_at="2026-09-25T10:00:05+00:00")
    _insert("a", created_at="2026-09-25T10:00:01+00:00")
    first = bot_commands.claim_next()
    assert first is not None and first["id"] == "a"
    assert first["status"] == "running"
    assert first["started_at"] and first["started_at"].endswith("+00:00")
    second = bot_commands.claim_next()
    assert second is not None and second["id"] == "b"
    assert bot_commands.claim_next() is None


def test_claim_next_on_empty_or_fresh_db_is_none() -> None:
    assert bot_commands.claim_next() is None


def test_claim_skips_non_pending_rows() -> None:
    _insert("done1", status="done")
    _insert("run1", status="running")
    assert bot_commands.claim_next() is None


def test_finish_fail_reject_are_terminal_with_timestamps() -> None:
    for cid in ("f", "e", "r"):
        _insert(cid)
        bot_commands.claim_next()
    bot_commands.finish("f", '{"x": 1}')
    bot_commands.fail("e", "boom" * 1000)
    bot_commands.reject("r", "not the owner")

    f, e, r = _row("f"), _row("e"), _row("r")
    assert (f["status"], f["result"], f["error"]) == ("done", '{"x": 1}', "")
    assert e["status"] == "error" and len(e["error"]) == 2000
    assert (r["status"], r["error"]) == ("rejected", "not the owner")
    for row in (f, e, r):
        assert row["finished_at"] and row["finished_at"].endswith("+00:00")


def test_fail_orphaned_running_only_touches_running_rows() -> None:
    _insert("run", status="running")
    _insert("pend")
    _insert("done", status="done")
    assert bot_commands.fail_orphaned_running("bot restarted") == 1
    assert (_row("run")["status"], _row("run")["error"]) == ("error", "bot restarted")
    assert _row("pend")["status"] == "pending"
    assert _row("done")["status"] == "done"


def test_init_db_creates_the_table(tmp_path) -> None:
    from hunter.db import init_db

    db = tmp_path / "fresh.db"
    init_db(db, xlsx_path=tmp_path / "none.xlsx")
    with get_db(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_commands)")}
    assert cols == {
        "id",
        "user_id",
        "kind",
        "payload",
        "status",
        "result",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    }


# ── Part 2: the drain ─────────────────────────────────────────────────────────


def test_hunt_command_runs_run_hunt_with_the_sources_and_stamps_done() -> None:
    _insert("c1", payload={"sources": ["LinkedIn"]})
    fake = AsyncMock()

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            assert await drain.drain_once(_context()) == 1
            await _settle()

    asyncio.run(scenario())
    fake.assert_awaited_once()
    args, kwargs = fake.await_args
    assert args[1] == ["linkedin"]
    assert kwargs == {"trigger": "web", "command_id": "c1"}
    row = _row("c1")
    assert row["status"] == "done"
    assert row["finished_at"]


def test_hunt_all_sources_passes_none() -> None:
    _insert("c1", payload={"sources": None})
    fake = AsyncMock()

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            await drain.drain_once(_context())
            await _settle()

    asyncio.run(scenario())
    assert fake.await_args.args[1] is None
    assert _row("c1")["status"] == "done"


def test_hunt_failure_stamps_error() -> None:
    _insert("c1", payload={"sources": None})

    async def scenario():
        with patch.object(hunter_main, "run_hunt", AsyncMock(side_effect=RuntimeError("db gone"))):
            await drain.drain_once(_context())
            await _settle()

    asyncio.run(scenario())
    row = _row("c1")
    assert row["status"] == "error"
    assert "db gone" in row["error"]


def test_non_owner_is_rejected_and_nothing_runs() -> None:
    _insert("c1", user_id="someone-else", payload={"sources": None})
    fake = AsyncMock()

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            await drain.drain_once(_context())
            await _settle()

    asyncio.run(scenario())
    fake.assert_not_awaited()
    row = _row("c1")
    assert (row["status"], row["error"]) == ("rejected", drain.REASON_NOT_OWNER)


def test_single_user_mode_accepts_any_user(monkeypatch) -> None:
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "")
    _insert("c1", user_id="whoever", payload={"sources": None})
    fake = AsyncMock()

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            await drain.drain_once(_context())
            await _settle()

    asyncio.run(scenario())
    fake.assert_awaited_once()
    assert _row("c1")["status"] == "done"


@pytest.mark.parametrize(
    ("kind", "payload", "reason_part"),
    [
        ("reboot", {}, "unknown kind: reboot"),
        ("", {}, "unknown kind"),
        ("hunt", {"sources": ["nosuchboard"]}, "unknown source(s): nosuchboard"),
        ("hunt", {"sources": "linkedin"}, "invalid payload"),
        ("hunt", {"sources": [1, 2]}, "invalid payload"),
        ("hunt", "[1, 2]", "invalid payload"),
        ("hunt", "{not json", "invalid payload"),
    ],
)
def test_bad_rows_are_rejected(kind, payload, reason_part) -> None:
    _insert("c1", kind=kind, payload=payload)
    fake = AsyncMock()

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            await drain.drain_once(_context())

    asyncio.run(scenario())
    fake.assert_not_awaited()
    row = _row("c1")
    assert row["status"] == "rejected"
    assert reason_part in row["error"]


def test_hunt_rejected_while_hunt_lock_is_held() -> None:
    _insert("c1", payload={"sources": None})
    fake = AsyncMock()

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            await hunter_main._hunt_lock.acquire()
            try:
                await drain.drain_once(_context())
            finally:
                hunter_main._hunt_lock.release()

    asyncio.run(scenario())
    fake.assert_not_awaited()
    row = _row("c1")
    assert (row["status"], row["error"]) == ("rejected", drain.REASON_HUNT_BUSY)


def test_second_hunt_in_the_same_tick_is_rejected() -> None:
    """Two rows claimed in one tick: the first task has not taken the lock
    yet when the second is validated — it must still count as busy."""
    _insert("c1", payload={"sources": None}, created_at="2026-09-25T10:00:01+00:00")
    _insert("c2", payload={"sources": ["linkedin"]}, created_at="2026-09-25T10:00:02+00:00")
    gate = _Gate()
    fake = gate.mock

    async def scenario():
        with patch.object(hunter_main, "run_hunt", fake):
            assert await drain.drain_once(_context()) == 2
            gate.open()
            await _settle()

    asyncio.run(scenario())
    assert fake.await_count == 1
    assert _row("c1")["status"] == "done"
    assert (_row("c2")["status"], _row("c2")["error"]) == ("rejected", drain.REASON_HUNT_BUSY)


def test_retry_failed_runs_and_shares_the_busy_rule(monkeypatch) -> None:
    monkeypatch.setattr(hunter_main, "AUTO_APPLY", True)
    _insert("r1", kind="retry_failed", created_at="2026-09-25T10:00:01+00:00")
    _insert("h1", payload={"sources": None}, created_at="2026-09-25T10:00:02+00:00")
    gate = _Gate()
    fake_retry = gate.mock
    fake_hunt = AsyncMock()

    async def scenario():
        with (
            patch.object(hunter_main, "run_retry_failed", fake_retry),
            patch.object(hunter_main, "run_hunt", fake_hunt),
        ):
            await drain.drain_once(_context())
            gate.open()
            await _settle()

    asyncio.run(scenario())
    fake_retry.assert_awaited_once()
    assert fake_retry.await_args.kwargs == {"command_id": "r1"}
    fake_hunt.assert_not_awaited()
    assert _row("r1")["status"] == "done"
    assert _row("h1")["error"] == drain.REASON_HUNT_BUSY


def test_retry_failed_rejected_when_auto_apply_is_off(monkeypatch) -> None:
    monkeypatch.setattr(hunter_main, "AUTO_APPLY", False)
    _insert("r1", kind="retry_failed")
    asyncio.run(drain.drain_once(_context()))
    assert (_row("r1")["status"], _row("r1")["error"]) == ("rejected", drain.REASON_AUTO_APPLY_OFF)


def test_retry_failed_rejected_while_hunt_lock_is_held(monkeypatch) -> None:
    monkeypatch.setattr(hunter_main, "AUTO_APPLY", True)
    _insert("r1", kind="retry_failed")

    async def scenario():
        await hunter_main._hunt_lock.acquire()
        try:
            await drain.drain_once(_context())
        finally:
            hunter_main._hunt_lock.release()

    asyncio.run(scenario())
    assert _row("r1")["error"] == drain.REASON_HUNT_BUSY


def test_check_expired_runs_reports_and_has_its_own_guard() -> None:
    _insert("e1", kind="check_expired", created_at="2026-09-25T10:00:01+00:00")
    _insert("e2", kind="check_expired", created_at="2026-09-25T10:00:02+00:00")
    result = {"total": 7, "alive": 5, "expired": [{"company": "A", "title": "B"}], "errors": []}
    gate = _Gate(return_value=result)
    fake = gate.mock

    async def scenario():
        with patch("hunter.schedules.check_expired.run_expired_check_and_report", fake):
            await drain.drain_once(_context())
            gate.open()
            await _settle()

    asyncio.run(scenario())
    fake.assert_awaited_once()
    assert fake.await_args.kwargs["report_when_nothing_expired"] is True
    row = _row("e1")
    assert row["status"] == "done"
    assert json.loads(row["result"]) == {"total": 7, "expired": 1, "errors": 0}
    assert (_row("e2")["status"], _row("e2")["error"]) == ("rejected", drain.REASON_EXPIRED_BUSY)


def test_check_expired_does_not_block_a_hunt() -> None:
    _insert("e1", kind="check_expired", created_at="2026-09-25T10:00:01+00:00")
    _insert("h1", payload={"sources": None}, created_at="2026-09-25T10:00:02+00:00")

    expired_gate = _Gate(return_value={"total": 0, "expired": [], "errors": []})

    async def scenario():
        with (
            patch(
                "hunter.schedules.check_expired.run_expired_check_and_report",
                expired_gate.mock,
            ),
            patch.object(hunter_main, "run_hunt", AsyncMock()),
        ):
            await drain.drain_once(_context())
            expired_gate.open()
            await _settle()

    asyncio.run(scenario())
    assert _row("e1")["status"] == "done"
    assert _row("h1")["status"] == "done"


def test_scheduled_drain_respects_the_flag(monkeypatch) -> None:
    monkeypatch.setattr("hunter.config.BOT_COMMANDS_ENABLED", False)
    _insert("c1", payload={"sources": None})
    asyncio.run(drain.scheduled_bot_commands_drain(_context()))
    assert _row("c1")["status"] == "pending"


def test_scheduled_drain_swallows_and_counts_a_broken_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bot_commands, "DB_PATH", tmp_path / "missing_dir" / "x.db")
    asyncio.run(drain.scheduled_bot_commands_drain(_context()))  # must not raise
    with get_db(tmp_path / "subsystem_health.db") as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM subsystem_health WHERE subsystem='bot.commands'"
        ).fetchone()
    assert row["consecutive_failures"] == 1


def test_register_wires_the_drain_and_the_state_tick() -> None:
    from unittest.mock import MagicMock

    import pytz

    from hunter import schedules

    app = MagicMock()
    schedules.register(app, pytz.timezone("Europe/Warsaw"))
    repeating = {c.kwargs.get("name"): c.kwargs for c in app.job_queue.run_repeating.call_args_list}
    assert repeating["bot_commands_drain"]["interval"] == 3
    assert repeating["bot_commands_drain"]["callback"] is drain.scheduled_bot_commands_drain
    assert repeating["bot_state"]["interval"] == 60


def test_post_init_cleans_up_the_previous_process() -> None:
    """Wiring guard: startup stamps leftover running commands and unfinished
    hunt_live rows as error, and publishes the scheduler facts once."""
    import inspect

    from hunter import telegram_bot

    src = inspect.getsource(telegram_bot._post_init)
    assert "bot_commands.fail_orphaned_running" in src
    assert "hunt_live.fail_unfinished" in src
    assert "bot_state.publish(app)" in src
