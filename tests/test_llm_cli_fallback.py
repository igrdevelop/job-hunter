"""M4b (docs/LLM_OUTAGE_RESILIENCE_PLAN.md): call_llm-level CLI fallback.

When LLM_OUTAGE_FALLBACK_CLI is on, an LLMOutageError from any provider gets
ONE `claude -p` retry at the call_llm choke point — covering the cheap stages
(judge / verdict / translate / outreach) that the pipeline-level M4 fallback
never reached. Any CLI failure re-raises the ORIGINAL outage so exit-46
semantics (stop batch, no FAIL row, arm pause) are preserved.
"""

import subprocess

import pytest

import llm_client
from llm_client import LLMOutageError, LLMRateLimitError


class _CliResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def outage_provider(monkeypatch):
    """Anthropic provider always raises an account outage."""

    def _boom(*a, **k):
        raise LLMOutageError("Your credit balance is too low")

    monkeypatch.setattr(llm_client, "_call_anthropic", _boom)
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: pytest.fail("must not sleep"))


def _patch_cli(monkeypatch, result=None, exc=None):
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _call():
    return llm_client.call_llm("SYS-PROMPT", "USER-MSG", provider="anthropic", api_key="k")


def test_flag_off_no_cli_attempt(outage_provider, monkeypatch):
    monkeypatch.delenv("LLM_OUTAGE_FALLBACK_CLI", raising=False)
    calls = _patch_cli(monkeypatch, _CliResult(stdout='{"ok": true}'))
    with pytest.raises(LLMOutageError):
        _call()
    assert calls == []


def test_flag_on_cli_serves_the_call(outage_provider, monkeypatch):
    monkeypatch.setenv("LLM_OUTAGE_FALLBACK_CLI", "true")
    calls = _patch_cli(monkeypatch, _CliResult(stdout='```json\n{"score": 91}\n```'))
    assert _call() == {"score": 91}
    assert len(calls) == 1
    assert calls[0]["cmd"][:2] == ["claude", "-p"]
    # Prompt rides STDIN (argv would hit the Windows ~32K limit on real prompts)
    assert "SYS-PROMPT" in calls[0]["input"] and "USER-MSG" in calls[0]["input"]


def test_cli_nonzero_exit_reraises_original_outage(outage_provider, monkeypatch):
    monkeypatch.setenv("LLM_OUTAGE_FALLBACK_CLI", "true")
    _patch_cli(monkeypatch, _CliResult(returncode=1, stderr="not logged in"))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()


def test_cli_missing_reraises_original_outage(outage_provider, monkeypatch):
    monkeypatch.setenv("LLM_OUTAGE_FALLBACK_CLI", "true")
    _patch_cli(monkeypatch, exc=FileNotFoundError("claude not on PATH"))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()


def test_cli_garbage_output_reraises_outage_not_parse_error(outage_provider, monkeypatch):
    """Unparseable CLI output must NOT surface as a plain LLMError — that would
    downgrade the outage to a FAIL row in the batch loops."""
    monkeypatch.setenv("LLM_OUTAGE_FALLBACK_CLI", "true")
    _patch_cli(monkeypatch, _CliResult(stdout="I'm sorry, something went wrong"))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()


def test_dual_shadow_override_never_falls_back(outage_provider, monkeypatch):
    """The shadow A/B run forces a specific model — serving it from the
    subscription would poison the comparison."""
    from hunter import llm_profiles

    monkeypatch.setenv("LLM_OUTAGE_FALLBACK_CLI", "true")
    calls = _patch_cli(monkeypatch, _CliResult(stdout='{"ok": true}'))
    llm_profiles.set_override(llm_profiles.PROFILES["sonnet"])
    try:
        with pytest.raises(LLMOutageError):
            _call()
    finally:
        llm_profiles.set_override(None)
    assert calls == []


def test_rate_limit_does_not_trigger_cli(monkeypatch):
    """Genuine 429s keep the normal retry ladder — the CLI is for outages only."""

    def _limited(*a, **k):
        raise LLMRateLimitError("429 Too Many Requests")

    monkeypatch.setattr(llm_client, "_call_anthropic", _limited)
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_OUTAGE_FALLBACK_CLI", "true")
    calls = _patch_cli(monkeypatch, _CliResult(stdout='{"ok": true}'))
    with pytest.raises(llm_client.LLMError):
        llm_client.call_llm("s", "u", provider="anthropic", api_key="k", max_retries=2)
    assert calls == []
