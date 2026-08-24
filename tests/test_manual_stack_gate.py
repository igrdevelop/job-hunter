"""The `--manual` flag and the stack gates it degrades.

docs/STACK_PRESCREEN_PLAN.md M2. Owner decision 2026-08-24: the auto-hunt keeps
filtering React-only postings (measured: 2 of 38 such packages were ever sent,
against a 43% baseline), but a URL the owner pastes himself is generated without
argument. Before this flag the pipeline could not tell the two apart -- `/force`
was the only override, and it bypasses every gate including dedup.

The end-to-end behavior of the gates lives in tests/test_golden_apply_e2e.py
(test_golden_react_only_*); this module covers the plumbing that carries the
flag from the Telegram handler down to them.
"""

import asyncio
from pathlib import Path

from hunter.models import Job
from hunter.services.apply_service import run_apply_agent_for_url, run_apply_agent_subprocess


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:  # pragma: no cover - not reached in these tests
        pass


def _capture_cmd(monkeypatch) -> list:
    captured: list = []

    async def _fake_exec(*args, **_kwargs):
        captured.append(list(args))
        return _FakeProc()

    monkeypatch.setattr("hunter.services.apply_service.asyncio.create_subprocess_exec", _fake_exec)
    return captured


class TestFlagIsOnlyOnTheManualPath:
    def test_manual_runner_marks_the_run(self, monkeypatch):
        captured = _capture_cmd(monkeypatch)

        asyncio.run(
            run_apply_agent_for_url(
                url="https://example.com/jobs/1",
                timeout_sec=5,
                apply_agent_path=Path("apply_agent.py"),
                python_executable="python",
            )
        )

        assert "--manual" in captured[0]

    def test_auto_hunt_runner_does_not(self, monkeypatch):
        # run_apply_agent_subprocess is the auto-hunt / apply-queue path. If it
        # ever grew --manual, every React-only vacancy the bot finds on its own
        # would be generated again -- the exact behavior M1/M2 exist to stop.
        captured = _capture_cmd(monkeypatch)

        asyncio.run(
            run_apply_agent_subprocess(
                Job(
                    title="Senior Frontend Developer",
                    company="Acme",
                    location="Remote",
                    salary=None,
                    url="https://example.com/jobs/2",
                    source="test",
                ),
                timeout_sec=5,
                apply_agent_path=Path("apply_agent.py"),
                python_executable="python",
            )
        )

        assert "--manual" not in captured[0]


class TestArgvParsing:
    def test_flag_is_parsed(self):
        from apply_agent import parse_apply_cli_argv

        parsed = parse_apply_cli_argv(["apply_agent.py", "https://x", "--manual"])
        assert parsed[-1] is True

    def test_absent_by_default(self):
        from apply_agent import parse_apply_cli_argv

        parsed = parse_apply_cli_argv(["apply_agent.py", "https://x"])
        assert parsed[-1] is False

    def test_is_independent_of_force(self):
        # --manual degrades the stack gates only; --force bypasses everything.
        from apply_agent import parse_apply_cli_argv

        url, _cli, force, *_rest = parse_apply_cli_argv(["apply_agent.py", "https://x", "--manual"])
        assert url == "https://x"
        assert force is False


class TestDispatcherThreadsTheFlag:
    def test_reaches_the_api_pipeline(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("apply_agent.main_api", lambda *a, **kw: seen.update(kw) or None)
        monkeypatch.setattr("apply_agent.LLM_API_KEY", "key")

        import apply_agent

        apply_agent.main("https://x", is_manual=True)

        assert seen["is_manual"] is True

    def test_reaches_the_cli_pipeline(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("apply_agent.main_cli", lambda *a, **kw: seen.update(kw) or None)

        import apply_agent

        apply_agent.main("https://x", force_cli=True, is_manual=True)

        assert seen["is_manual"] is True


class TestStackGateHelper:
    def test_auto_run_is_not_degraded(self, monkeypatch):
        sent = []
        monkeypatch.setattr("hunter.apply_shared.notify", lambda m: sent.append(m))
        from hunter.apply_shared import stack_gate_allows_manual

        assert stack_gate_allows_manual(False, "https://x", "React-only stack") is False
        assert sent == [], "an auto-hunt skip must stay silent here (the gate notifies itself)"

    def test_manual_run_is_degraded_and_announced(self, monkeypatch):
        sent = []
        monkeypatch.setattr("hunter.apply_shared.notify", lambda m: sent.append(m))
        from hunter.apply_shared import stack_gate_allows_manual

        assert stack_gate_allows_manual(True, "https://x", "React-only stack") is True
        # Degraded, not silent: the owner still has to see what the bot thought.
        assert len(sent) == 1
        assert "React-only stack" in sent[0]
        assert "https://x" in sent[0]
