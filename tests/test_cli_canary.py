"""Tests for hunter/cli_canary.py (docs/APPLY_FAILURE_QUEUES_PLAN.md M1)."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from hunter import apply_cli, cli_canary
from hunter.cli_canary import CANARY_PROMPT, CanaryResult, format_alert, run_canary

# Real stderr of the 2026-09-10..21 incident (trimmed).
DENY_RULE_STDERR = (
    'Permission deny rule "/apply" matches no known tool — check for typos.\n'
    'Permission deny rule "URL:" matches no known tool — check for typos.\n'
)


class _FakeRun:
    """Stands in for subprocess.run; records argv, returns or raises per call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


@pytest.fixture(autouse=True)
def _isolated_best_effort_db(tmp_path, monkeypatch):
    monkeypatch.setattr("hunter.best_effort.DB_PATH", tmp_path / "health.db")


@pytest.fixture
def restricted_perms(monkeypatch):
    monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", False)


class TestRunCanary:
    def test_pass(self, monkeypatch, restricted_perms) -> None:
        fake = _FakeRun((0, "OK\n", ""))
        monkeypatch.setattr(cli_canary.subprocess, "run", fake)
        assert run_canary() == CanaryResult(True, "ok")

    def test_uses_the_apply_pipelines_own_argv_builder(self, monkeypatch, restricted_perms) -> None:
        fake = _FakeRun((0, "OK", ""))
        monkeypatch.setattr(cli_canary.subprocess, "run", fake)
        run_canary()
        (cmd,) = fake.calls
        assert cmd == apply_cli._cli_argv(CANARY_PROMPT)
        # The property the 2026-09-10 incident broke: the prompt precedes the
        # variadic tool flags, and only their values follow them.
        assert cmd[2] == CANARY_PROMPT
        assert cmd[3:] == [
            "--allowedTools",
            apply_cli._CLI_ALLOWED_TOOLS,
            "--disallowedTools",
            apply_cli._CLI_DISALLOWED_TOOLS,
        ]

    def test_apply_command_and_canary_share_the_builder(self, restricted_perms) -> None:
        apply_cmd = apply_cli._build_cli_command("URL: https://x.example/1")
        canary_cmd = apply_cli._cli_argv(CANARY_PROMPT)
        # Same flags in the same positions; only the prompt differs.
        assert apply_cmd[:2] == canary_cmd[:2]
        assert apply_cmd[3:] == canary_cmd[3:]

    def test_legacy_perms_canary_follows_the_escape_hatch(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", True)
        fake = _FakeRun((0, "OK", ""))
        monkeypatch.setattr(cli_canary.subprocess, "run", fake)
        run_canary()
        assert fake.calls[0] == ["claude", "-p", "--dangerously-skip-permissions", CANARY_PROMPT]

    def test_runs_in_the_project_dir_with_no_stdin(self, monkeypatch, restricted_perms) -> None:
        from hunter.config import PROJECT_DIR

        fake = _FakeRun((0, "OK", ""))
        monkeypatch.setattr(cli_canary.subprocess, "run", fake)
        run_canary()
        assert fake.kwargs[0]["cwd"] == str(PROJECT_DIR)
        assert fake.kwargs[0]["stdin"] is subprocess.DEVNULL

    def test_the_incident_is_caught_and_not_retryable(self, monkeypatch, restricted_perms) -> None:
        monkeypatch.setattr(cli_canary.subprocess, "run", _FakeRun((1, "", DENY_RULE_STDERR)))
        result = run_canary()
        assert not result.ok
        assert not result.retryable
        assert "rejected" in result.reason
        assert "matches no known tool" in result.detail

    def test_argv_warning_fails_even_when_exit_code_is_zero(
        self, monkeypatch, restricted_perms
    ) -> None:
        monkeypatch.setattr(cli_canary.subprocess, "run", _FakeRun((0, "OK", DENY_RULE_STDERR)))
        assert not run_canary().ok

    def test_nonzero_exit_is_retryable(self, monkeypatch, restricted_perms) -> None:
        monkeypatch.setattr(
            cli_canary.subprocess, "run", _FakeRun((1, "", "API Error: 529 Overloaded"))
        )
        result = run_canary()
        assert not result.ok and result.retryable
        assert result.reason == "exit code 1"
        assert "529" in result.detail

    @pytest.mark.parametrize("reply", ["OK", "ok\n", "OK.", "`OK`", "  Ok!  "])
    def test_bare_ok_in_any_case_or_punctuation_passes(
        self, monkeypatch, restricted_perms, reply
    ) -> None:
        monkeypatch.setattr(cli_canary.subprocess, "run", _FakeRun((0, reply, "")))
        assert run_canary().ok

    @pytest.mark.parametrize("reply", ["OK, but I cannot continue", "BOOK", "Not OK", "OK OK", ""])
    def test_anything_beyond_a_bare_ok_fails(self, monkeypatch, restricted_perms, reply) -> None:
        monkeypatch.setattr(cli_canary.subprocess, "run", _FakeRun((0, reply, "")))
        result = run_canary()
        assert not result.ok
        assert result.reason == "unexpected reply"

    def test_unexpected_reply(self, monkeypatch, restricted_perms) -> None:
        monkeypatch.setattr(cli_canary.subprocess, "run", _FakeRun((0, "I can't help", "")))
        result = run_canary()
        assert not result.ok
        assert result.reason == "unexpected reply"

    def test_missing_binary_is_not_retryable(self, monkeypatch, restricted_perms) -> None:
        monkeypatch.setattr(cli_canary.subprocess, "run", _FakeRun(FileNotFoundError("claude")))
        result = run_canary()
        assert not result.ok and not result.retryable

    def test_timeout(self, monkeypatch, restricted_perms) -> None:
        monkeypatch.setattr(
            cli_canary.subprocess, "run", _FakeRun(subprocess.TimeoutExpired("claude", 180))
        )
        result = run_canary()
        assert not result.ok and result.retryable
        assert "timed out" in result.reason


class TestStartupCanary:
    @pytest.fixture
    def sent(self, monkeypatch):
        messages: list[str] = []

        async def fake_send_text(_context, text):
            messages.append(text)

        monkeypatch.setattr("hunter.bot.notifications.send_text", fake_send_text)
        monkeypatch.setattr("hunter.config.CLI_CANARY_ENABLED", True)
        monkeypatch.setattr("llm_client.cli_credentials_present", lambda: True)
        return messages

    def _run(self, **kwargs):
        return asyncio.run(
            cli_canary.run_startup_canary(SimpleNamespace(), retry_delay=0, **kwargs)
        )

    def _script(self, monkeypatch, *results: CanaryResult) -> list[int]:
        calls: list[int] = []
        queue = list(results)

        def fake_run_canary(*_a, **_kw):
            calls.append(1)
            return queue.pop(0)

        monkeypatch.setattr(cli_canary, "run_canary", fake_run_canary)
        return calls

    def test_pass_sends_nothing(self, monkeypatch, sent) -> None:
        calls = self._script(monkeypatch, CanaryResult(True, "ok"))
        assert self._run().ok
        assert len(calls) == 1
        assert sent == []

    def test_transient_failure_is_retried_once_then_passes(self, monkeypatch, sent) -> None:
        calls = self._script(
            monkeypatch, CanaryResult(False, "exit code 1"), CanaryResult(True, "ok")
        )
        assert self._run().ok
        assert len(calls) == 2
        assert sent == []

    def test_persistent_failure_alerts_once(self, monkeypatch, sent) -> None:
        calls = self._script(
            monkeypatch, CanaryResult(False, "exit code 1"), CanaryResult(False, "exit code 1")
        )
        assert not self._run().ok
        assert len(calls) == 2
        assert len(sent) == 1
        assert "canary failed" in sent[0]

    def test_deterministic_failure_is_not_retried(self, monkeypatch, sent) -> None:
        calls = self._script(
            monkeypatch,
            CanaryResult(False, "rejected", DENY_RULE_STDERR, retryable=False),
        )
        assert not self._run().ok
        assert len(calls) == 1
        assert len(sent) == 1

    def test_disabled_does_nothing(self, monkeypatch, sent) -> None:
        monkeypatch.setattr("hunter.config.CLI_CANARY_ENABLED", False)
        calls = self._script(monkeypatch, CanaryResult(True, "ok"))
        assert self._run() is None
        assert calls == []

    def test_no_cli_login_does_nothing(self, monkeypatch, sent) -> None:
        monkeypatch.setattr("llm_client.cli_credentials_present", lambda: False)
        calls = self._script(monkeypatch, CanaryResult(True, "ok"))
        assert self._run() is None
        assert calls == []
        assert sent == []

    def test_an_unexpected_exception_never_escapes(self, monkeypatch, sent) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(cli_canary, "run_canary", boom)
        assert self._run() is None


class TestFormatAlert:
    def test_detail_is_html_escaped(self) -> None:
        text = format_alert(CanaryResult(False, "exit <1>", 'rule "<x>" & more'))
        assert "&lt;1&gt;" in text
        assert "&lt;x&gt;" in text and "&amp;" in text
        assert "<pre>" in text
