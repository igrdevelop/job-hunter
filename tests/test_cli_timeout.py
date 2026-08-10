"""M3 (docs/HUNT_APPLY_SPLIT_PLAN.md): a widened (CLI-eligible) subprocess
timeout is infrastructure, not the vacancy's fault — it must never write a
FAIL row or escalate fail_count. The guards that must fail if M3 is reverted:
  - test_auto_apply_cli_timeout_writes_no_fail_row_and_continues
  - test_retry_cli_timeout_leaves_fail_count_untouched
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from hunter import main
from hunter.models import Job


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return b"", b""

    def kill(self):
        self.killed = True


def _job(n: int = 0) -> Job:
    return Job(
        title=f"Role {n}",
        company=f"Co{n}",
        location="Remote",
        salary=None,
        url=f"https://example.com/job/{n}",
        source="test",
    )


# ── apply_service: TimeoutError -> "cli_timeout" only when the timeout was
# actually widened (CLI-eligible run); a plain API-mode timeout stays "fail" ──


def test_subprocess_timeout_maps_to_cli_timeout_when_widened(monkeypatch):
    from hunter.services import apply_service

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _hang(*a, **k):
        raise asyncio.TimeoutError

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(apply_service, "_effective_timeout", lambda t: t * 3)
    monkeypatch.setattr(apply_service.asyncio, "wait_for", _hang)

    outcome = asyncio.run(
        apply_service.run_apply_agent_subprocess(
            job=_job(),
            timeout_sec=5,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert outcome == "cli_timeout"


def test_subprocess_timeout_stays_fail_when_not_widened(monkeypatch):
    from hunter.services import apply_service

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _hang(*a, **k):
        raise asyncio.TimeoutError

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(apply_service, "_effective_timeout", lambda t: t)
    monkeypatch.setattr(apply_service.asyncio, "wait_for", _hang)

    outcome = asyncio.run(
        apply_service.run_apply_agent_subprocess(
            job=_job(),
            timeout_sec=5,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert outcome == "fail"


def test_url_variant_timeout_maps_to_cli_timeout_when_widened(monkeypatch):
    from hunter.services import apply_service

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _hang(*a, **k):
        raise asyncio.TimeoutError

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(apply_service, "_effective_timeout", lambda t: t * 3)
    monkeypatch.setattr(apply_service.asyncio, "wait_for", _hang)

    outcome, detail = asyncio.run(
        apply_service.run_apply_agent_for_url(
            url="https://example.com/job/1",
            timeout_sec=5,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert outcome == "cli_timeout"
    assert "timed out" in detail.lower()


# ── Cancel must kill the child so release_claim can't double-apply ────────────


def test_subprocess_cancel_kills_child(monkeypatch):
    from hunter.services import apply_service

    proc = _FakeProc()

    async def _fake_exec(*a, **k):
        return proc

    async def _cancel(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(apply_service, "_effective_timeout", lambda t: t)
    monkeypatch.setattr(apply_service.asyncio, "wait_for", _cancel)

    async def _run():
        await apply_service.run_apply_agent_subprocess(
            job=_job(),
            timeout_sec=5,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )

    try:
        asyncio.run(_run())
        raise AssertionError("CancelledError was not raised")
    except asyncio.CancelledError:
        pass
    assert proc.killed is True


def test_url_variant_cancel_kills_child(monkeypatch):
    from hunter.services import apply_service

    proc = _FakeProc()

    async def _fake_exec(*a, **k):
        return proc

    async def _cancel(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(apply_service, "_effective_timeout", lambda t: t)
    monkeypatch.setattr(apply_service.asyncio, "wait_for", _cancel)

    async def _run():
        await apply_service.run_apply_agent_for_url(
            url="https://example.com/job/1",
            timeout_sec=5,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )

    try:
        asyncio.run(_run())
        raise AssertionError("CancelledError was not raised")
    except asyncio.CancelledError:
        pass
    assert proc.killed is True


# ── _auto_apply_all: no FAIL row, batch continues (unlike llm_outage) ────────


def test_auto_apply_cli_timeout_writes_no_fail_row_and_continues(monkeypatch):
    jobs = [_job(0), _job(1), _job(2)]
    failed_writes: list[str] = []

    monkeypatch.setattr(main, "add_failed", lambda job: failed_writes.append(job.url))
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_deliver_now", AsyncMock())
    runner = AsyncMock(side_effect=["ok", "cli_timeout", "ok"])
    monkeypatch.setattr(main, "_run_apply_agent", runner)

    asyncio.run(main._auto_apply_all(context=None, jobs=jobs))

    # Job 1 hit the CLI timeout: no FAIL row for it, but job 2 IS attempted
    # (cli_timeout is per-job infrastructure, not global state like llm_outage).
    assert failed_writes == []
    assert runner.await_count == 3
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "CLI timed out" in msgs


# ── _retry_failed: fail_count untouched, batch continues ─────────────────────


def test_retry_cli_timeout_leaves_fail_count_untouched(monkeypatch):
    jobs = [_job(0), _job(1)]
    increments: list[str] = []

    monkeypatch.setattr(main, "get_failed_jobs", lambda: list(jobs))
    monkeypatch.setattr(main, "increment_fail_count", lambda url: increments.append(url) or 1)
    monkeypatch.setattr(main, "remove_failed", lambda url: None)
    monkeypatch.setattr(main, "classify_retry_outcome", lambda url: "applied")
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_deliver_now", AsyncMock())
    runner = AsyncMock(side_effect=["cli_timeout", "ok"])
    monkeypatch.setattr(main, "_run_apply_agent", runner)

    asyncio.run(main._retry_failed(context=None))

    # No escalation, and the second row IS attempted (not a global stop).
    assert increments == []
    assert runner.await_count == 2
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "CLI timeout" in msgs


# ── bot/apply_runner._run_apply_agent: same treatment as llm_outage ──────────


def test_apply_runner_cli_timeout_notifies_without_fail(monkeypatch):
    from hunter.bot import apply_runner

    notified: list[str] = []

    async def _fake_run_for_url(*a, **k):
        return "cli_timeout", "CLI timed out after 2700s"

    async def _fake_notify(text):
        notified.append(text)

    async def _fake_deliver(url):
        raise AssertionError("deliver_apply_now must not run on cli_timeout")

    monkeypatch.setattr("hunter.services.apply_service.run_apply_agent_for_url", _fake_run_for_url)
    monkeypatch.setattr(apply_runner, "_tg_notify", _fake_notify)
    monkeypatch.setattr("hunter.delivery.deliver_apply_now", _fake_deliver)

    asyncio.run(apply_runner._run_apply_agent("https://example.com/job/9"))

    assert any("CLI timed out" in n for n in notified)
