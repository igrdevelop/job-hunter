"""M2 (docs/LLM_OUTAGE_RESILIENCE_PLAN.md): time-boxed auto-apply pause.

One outage stops one batch (M1); the pause stops the NEXT staggered slots
from re-fetching into the same wall. Time-boxed: after expiry the next slot
probes naturally — a top-up heals the bot without owner action.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter import llm_outage, main
from hunter.models import Job


@pytest.fixture()
def outage_db(tmp_path, monkeypatch):
    """Route the config KV table (llm_profiles._db_get/_db_set) to a tmp DB."""
    db = tmp_path / "tracker.db"
    monkeypatch.setattr("hunter.llm_profiles._get_db_path", lambda: db)
    return db


def _job(n: int = 0) -> Job:
    return Job(
        title=f"Role {n}",
        company=f"Co{n}",
        location="Remote",
        salary=None,
        url=f"https://example.com/job/{n}",
        source="test",
    )


# ── llm_outage state machine ──────────────────────────────────────────────────


def test_no_pause_by_default(outage_db):
    assert llm_outage.pause_remaining() == 0


def test_arm_then_remaining_then_expiry(outage_db, monkeypatch):
    monkeypatch.setattr("hunter.config.LLM_OUTAGE_PAUSE_MIN", 60)
    until = llm_outage.arm_pause(now=1_000_000.0)
    assert until == 1_000_000 + 3600
    assert llm_outage.pause_remaining(now=1_000_000.0) == 3600
    assert llm_outage.pause_remaining(now=1_000_000.0 + 3599) == 1
    # Time-boxed: expired on its own, no manual clear needed.
    assert llm_outage.pause_remaining(now=1_000_000.0 + 3600) == 0


def test_rearm_extends_deadline(outage_db, monkeypatch):
    monkeypatch.setattr("hunter.config.LLM_OUTAGE_PAUSE_MIN", 60)
    llm_outage.arm_pause(now=1_000_000.0)
    llm_outage.arm_pause(now=1_002_000.0)  # probe hit the wall again
    assert llm_outage.pause_remaining(now=1_002_000.0) == 3600


def test_clear_pause(outage_db):
    llm_outage.arm_pause()
    assert llm_outage.pause_remaining() > 0
    assert llm_outage.clear_pause() is True
    assert llm_outage.pause_remaining() == 0
    assert llm_outage.clear_pause() is False  # already clear


def test_garbage_db_value_means_no_pause(outage_db):
    from hunter.llm_profiles import _db_set

    _db_set("llm_outage_until", "not-a-number")
    assert llm_outage.pause_remaining() == 0


# ── batch loops arm the pause on outage ───────────────────────────────────────


def test_auto_apply_outage_arms_pause(outage_db, monkeypatch):
    monkeypatch.setattr(main, "add_failed", lambda job: pytest.fail("no FAIL rows"))
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_deliver_now", AsyncMock())
    monkeypatch.setattr(main, "_run_apply_agent", AsyncMock(side_effect=["llm_outage"]))

    asyncio.run(main._auto_apply_all(context=None, jobs=[_job(0)]))

    assert llm_outage.pause_remaining() > 0
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "paused until" in msgs


def test_retry_outage_arms_pause(outage_db, monkeypatch):
    monkeypatch.setattr(main, "get_failed_jobs", lambda: [_job(0)])
    monkeypatch.setattr(main, "increment_fail_count", lambda url: pytest.fail("no escalation"))
    monkeypatch.setattr(main, "remove_failed", lambda url: None)
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_deliver_now", AsyncMock())
    monkeypatch.setattr(main, "_run_apply_agent", AsyncMock(side_effect=["llm_outage"]))

    asyncio.run(main._retry_failed(context=None))

    assert llm_outage.pause_remaining() > 0


# ── scheduled slots skip silently while paused ────────────────────────────────


def test_run_retry_failed_skips_while_paused(outage_db, monkeypatch):
    llm_outage.arm_pause()
    monkeypatch.setattr(main, "AUTO_APPLY", True)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    retried = AsyncMock()
    monkeypatch.setattr(main, "_retry_failed", retried)
    monkeypatch.setattr(main, "_check_apply_ready", lambda: pytest.fail("must skip first"))

    asyncio.run(main.run_retry_failed(context=None))

    retried.assert_not_awaited()
    # Silent skip: the one alert went out at arm time, not per slot.
    main.send_text.assert_not_awaited()


def test_run_retry_failed_runs_after_expiry(outage_db, monkeypatch):
    monkeypatch.setattr(main, "AUTO_APPLY", True)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    retried = AsyncMock()
    monkeypatch.setattr(main, "_retry_failed", retried)
    monkeypatch.setattr(main, "_check_apply_ready", lambda: None)

    asyncio.run(main.run_retry_failed(context=None))

    retried.assert_awaited_once()


# ── /llm outage subcommand ────────────────────────────────────────────────────


def _run_llm_cmd(args: list[str], monkeypatch) -> str:
    from hunter.commands import llm as llm_cmd

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    monkeypatch.setattr(llm_cmd, "TELEGRAM_CHAT_ID", update.effective_chat.id)
    context = MagicMock()
    context.args = args
    asyncio.run(llm_cmd.cmd_llm(update, context))
    return update.message.reply_text.await_args.args[0]


def test_cmd_llm_outage_status_and_clear(outage_db, monkeypatch):
    assert "No LLM-outage pause" in _run_llm_cmd(["outage"], monkeypatch)
    llm_outage.arm_pause()
    assert "paused" in _run_llm_cmd(["outage"], monkeypatch)
    assert "lifted" in _run_llm_cmd(["outage", "clear"], monkeypatch)
    assert llm_outage.pause_remaining() == 0
    assert "No active" in _run_llm_cmd(["outage", "clear"], monkeypatch)
