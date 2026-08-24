"""An empty CLI run is not an account outage (docs/STACK_PRESCREEN_PLAN.md M6).

`claude -p` is non-interactive. When the apply skill asked a clarifying question
instead of generating -- "Do you want me to proceed anyway, or should I skip this
one?" -- nobody answered, the run ended with no output folder, and main_cli
raised. apply_agent folded that into the LLM-outage exit code, so a healthy API
account got an hour-long auto-apply pause armed by a chatty prompt. Measured on
the deploy host 2026-08-24: 5 of 60 retained runs, every one of which succeeded
on a later attempt.

The prompt now forbids the question (.claude/commands/apply.md). This module
guards the code half: an empty CLI run reports its own exit code and is handled
like an infrastructure timeout -- no FAIL row, no fail_count escalation, no
pause, back to the queue.
"""

import asyncio
from pathlib import Path

import pytest

from hunter.apply_shared import APPLY_CLI_NO_OUTPUT_EXIT_CODE, ApplyError, CliNoOutputError


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:  # pragma: no cover
        pass


def _fake_exec(returncode: int):
    async def _exec(*_args, **_kwargs):
        return _FakeProc(returncode)

    return _exec


class TestTheExceptionIsDistinct:
    def test_it_is_still_an_apply_error(self):
        # Callers that only know about ApplyError must keep working.
        assert issubclass(CliNoOutputError, ApplyError)

    def test_the_exit_code_is_its_own(self):
        from hunter.apply_shared import (
            APPLY_LLM_OUTAGE_EXIT_CODE,
            APPLY_MANUAL_EXIT_CODE,
            APPLY_RATE_LIMITED_EXIT_CODE,
        )

        assert APPLY_CLI_NO_OUTPUT_EXIT_CODE not in (
            0,
            1,
            APPLY_MANUAL_EXIT_CODE,
            APPLY_RATE_LIMITED_EXIT_CODE,
            APPLY_LLM_OUTAGE_EXIT_CODE,
        )


class TestDispatcherReportsIt:
    def test_cli_only_run_exits_47_not_46(self, monkeypatch):
        # APPLY_USE_CLI / --cli: there is no API account involved at all, so
        # reporting an outage here was pure fiction.
        import apply_agent

        monkeypatch.setattr(
            "apply_agent.main_cli",
            lambda *a, **kw: (_ for _ in ()).throw(CliNoOutputError("No output folder created")),
        )
        monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)

        with pytest.raises(SystemExit) as exc:
            apply_agent.main("https://example.com/job/1", force_cli=True)

        assert exc.value.code == APPLY_CLI_NO_OUTPUT_EXIT_CODE

    def test_no_api_key_run_exits_47_not_1(self, monkeypatch):
        # Without a key the CLI is the only path; an empty run there is
        # retryable infrastructure noise, not a vacancy failure worth a FAIL row.
        import apply_agent

        monkeypatch.setattr("apply_agent.LLM_API_KEY", "")
        monkeypatch.setattr("apply_agent._is_cli_available", lambda: True)
        monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)
        monkeypatch.setattr(
            "apply_agent.main_cli",
            lambda *a, **kw: (_ for _ in ()).throw(CliNoOutputError("No output folder created")),
        )

        with pytest.raises(SystemExit) as exc:
            apply_agent.main("https://example.com/job/1")

        assert exc.value.code == APPLY_CLI_NO_OUTPUT_EXIT_CODE

    def test_a_plain_apply_error_is_still_a_failure(self, monkeypatch):
        import apply_agent

        monkeypatch.setattr("apply_agent.LLM_API_KEY", "")
        monkeypatch.setattr("apply_agent._is_cli_available", lambda: True)
        monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)
        monkeypatch.setattr(
            "apply_agent.main_cli",
            lambda *a, **kw: (_ for _ in ()).throw(ApplyError("CLI exited with code 2")),
        )

        with pytest.raises(SystemExit) as exc:
            apply_agent.main("https://example.com/job/1")

        assert exc.value.code == 1

    def test_the_outage_fallback_still_reports_an_outage(self, monkeypatch):
        # Here the API account really IS down and the CLI was only the fallback,
        # so exit 46 remains correct even when the CLI produced nothing.
        import apply_agent
        from hunter.apply_shared import APPLY_LLM_OUTAGE_EXIT_CODE

        def _outage(*_a, **_kw):
            raise SystemExit(APPLY_LLM_OUTAGE_EXIT_CODE)

        monkeypatch.setattr("apply_agent.LLM_API_KEY", "key")
        monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)
        monkeypatch.setattr("apply_agent.main_api", _outage)
        monkeypatch.setattr("apply_agent._is_cli_available", lambda: True)
        monkeypatch.setattr("apply_agent.notify", lambda _m: None)
        monkeypatch.setattr(
            "apply_agent.main_cli",
            lambda *a, **kw: (_ for _ in ()).throw(CliNoOutputError("No output folder created")),
        )

        with pytest.raises(SystemExit) as exc:
            apply_agent.main("https://example.com/job/1")

        assert exc.value.code == APPLY_LLM_OUTAGE_EXIT_CODE


class TestServiceMapsTheExitCode:
    def test_auto_hunt_runner(self, monkeypatch):
        from hunter.models import Job
        from hunter.services.apply_service import run_apply_agent_subprocess

        monkeypatch.setattr(
            "hunter.services.apply_service.asyncio.create_subprocess_exec",
            _fake_exec(APPLY_CLI_NO_OUTPUT_EXIT_CODE),
        )

        outcome = asyncio.run(
            run_apply_agent_subprocess(
                Job(
                    title="Senior Frontend Developer",
                    company="Acme",
                    location="Remote",
                    salary=None,
                    url="https://example.com/jobs/1",
                    source="test",
                ),
                timeout_sec=5,
                apply_agent_path=Path("apply_agent.py"),
                python_executable="python",
            )
        )

        assert outcome == "cli_no_output"

    def test_manual_runner(self, monkeypatch):
        from hunter.services.apply_service import run_apply_agent_for_url

        monkeypatch.setattr(
            "hunter.services.apply_service.asyncio.create_subprocess_exec",
            _fake_exec(APPLY_CLI_NO_OUTPUT_EXIT_CODE),
        )

        outcome, detail = asyncio.run(
            run_apply_agent_for_url(
                url="https://example.com/jobs/1",
                timeout_sec=5,
                apply_agent_path=Path("apply_agent.py"),
                python_executable="python",
            )
        )

        assert outcome == "cli_no_output"
        assert detail


class TestWorkerTreatsItAsInfrastructure:
    def test_the_row_goes_back_to_pending_without_a_fail(self, tracker_db, monkeypatch):
        # No FAIL row, no fail_count escalation, no outage pause: the vacancy is
        # fine and every observed case succeeded on a later attempt.
        from unittest.mock import AsyncMock

        from hunter import apply_worker, tracker
        from hunter.models import Job

        job = Job(
            title="Senior Frontend Developer",
            company="Acme",
            location="Remote",
            salary=None,
            url="https://example.com/jobs/1",
            source="test",
        )
        tracker.add_pending(job)
        tracker.claim_pending()

        monkeypatch.setattr(apply_worker, "send_text", AsyncMock())
        armed = []
        monkeypatch.setattr("hunter.llm_outage.arm_pause", lambda: armed.append(True))

        is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "cli_no_output"))

        assert is_fail is False
        assert armed == [], "a chatty prompt must not pause auto-apply for an hour"
        rows = tracker.lookup_url(job.url)
        assert rows and rows[0]["ats"].strip().upper() == tracker.PENDING_ATS


class TestThePromptForbidsTheQuestion:
    def test_the_rule_is_present_and_unambiguous(self):
        # The code half above only limits the damage; this is the actual fix.
        prompt = Path(".claude/commands/apply.md").read_text(encoding="utf-8")
        assert "decide, never ask" in prompt.lower()
        assert "generate the package, always" in prompt.lower()

    def test_it_comes_before_the_generation_steps(self):
        prompt = Path(".claude/commands/apply.md").read_text(encoding="utf-8")
        assert prompt.index("never ask") < prompt.index("## Step 4"), (
            "a rule the model reads after it has already started deliberating is not a rule"
        )
