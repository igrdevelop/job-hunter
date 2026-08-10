"""M4b (docs/LLM_OUTAGE_RESILIENCE_PLAN.md): call_llm-level CLI fallback.

When a Claude CLI login exists on disk (the login IS the switch — no env flag,
owner decision 2026-07-18), an LLMOutageError from any provider gets ONE
`claude -p` retry at the call_llm choke point — covering the cheap stages
(judge / verdict / translate / outreach) that the pipeline-level M4 fallback
never reached. Any CLI failure re-raises the ORIGINAL outage so exit-46
semantics (stop batch, no FAIL row, arm pause) are preserved.

The fallback pins `--model` to the model the API call asked for (2026-08-10:
unpinned calls were served by the subscription's default model — the ATS
verdict judge stopped being Haiku and the scoring scale shifted). If the CLI
rejects the pinned model, ONE unpinned retry runs; a missing binary or a
timeout never retries.
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


def _patch_cli(monkeypatch, *results, exc=None):
    """Script subprocess.run: pop one result per call (last one repeats), or
    raise `exc` on every call. Returns the recorded call list."""
    calls: list[dict] = []
    queue = list(results)

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if exc is not None:
            raise exc
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _call(model="claude-sonnet-4-6"):
    return llm_client.call_llm(
        "SYS-PROMPT", "USER-MSG", provider="anthropic", model=model, api_key="k"
    )


def test_no_login_no_cli_attempt(outage_provider, monkeypatch):
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: False)
    calls = _patch_cli(monkeypatch, _CliResult(stdout='{"ok": true}'))
    with pytest.raises(LLMOutageError):
        _call()
    assert calls == []


def test_login_present_cli_serves_the_call(outage_provider, monkeypatch):
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, _CliResult(stdout='```json\n{"score": 91}\n```'))
    assert _call() == {"score": 91}
    assert len(calls) == 1
    assert calls[0]["cmd"] == ["claude", "-p", "--model", "claude-sonnet-4-6"]
    # Prompt rides STDIN (argv would hit the Windows ~32K limit on real prompts)
    assert "SYS-PROMPT" in calls[0]["input"] and "USER-MSG" in calls[0]["input"]


def test_cli_fallback_pins_requested_model(outage_provider, monkeypatch):
    """THE regression test for the 2026-08-07..10 incident: the verdict judge
    (Haiku) must stay Haiku when served through the CLI — an unpinned call
    lands on the subscription's default model and shifts the scoring scale."""
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, _CliResult(stdout='{"score": 92}'))
    assert _call(model="claude-haiku-4-5-20251001") == {"score": 92}
    assert calls[0]["cmd"] == ["claude", "-p", "--model", "claude-haiku-4-5-20251001"]


def test_pinned_model_rejected_retries_once_unpinned_and_succeeds(outage_provider, monkeypatch):
    """A subscription may not offer every dated model ID — a default-model
    answer still beats re-raising the outage."""
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(
        monkeypatch,
        _CliResult(returncode=1, stderr="unknown model"),
        _CliResult(stdout='{"ok": true}'),
    )
    assert _call() == {"ok": True}
    assert len(calls) == 2
    assert calls[0]["cmd"] == ["claude", "-p", "--model", "claude-sonnet-4-6"]
    assert calls[1]["cmd"] == ["claude", "-p"]


def test_unpinned_retry_also_fails_reraises_original_outage(outage_provider, monkeypatch):
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, _CliResult(returncode=1, stderr="not logged in"))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()
    assert len(calls) == 2  # pinned attempt + one unpinned retry, then re-raise


def test_cli_missing_reraises_original_outage(outage_provider, monkeypatch):
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, exc=FileNotFoundError("claude not on PATH"))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()
    assert len(calls) == 1  # a missing binary is not retried unpinned


def test_timeout_does_not_retry_unpinned(outage_provider, monkeypatch):
    """A second 300s wait would double the worst-case latency of a call that
    already burned its budget."""
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, exc=subprocess.TimeoutExpired(cmd="claude", timeout=300))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()
    assert len(calls) == 1


def test_cli_garbage_output_reraises_outage_not_parse_error(outage_provider, monkeypatch):
    """Unparseable CLI output must NOT surface as a plain LLMError — that would
    downgrade the outage to a FAIL row in the batch loops. The model answered,
    so a different model isn't more likely to emit JSON: no unpinned retry."""
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, _CliResult(stdout="I'm sorry, something went wrong"))
    with pytest.raises(LLMOutageError, match="credit balance"):
        _call()
    assert len(calls) == 1


def test_dual_shadow_override_never_falls_back(outage_provider, monkeypatch):
    """The shadow A/B run forces a specific model — serving it from the
    subscription would poison the comparison."""
    from hunter import llm_profiles

    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
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
    monkeypatch.setattr(llm_client, "cli_credentials_present", lambda: True)
    calls = _patch_cli(monkeypatch, _CliResult(stdout='{"ok": true}'))
    with pytest.raises(llm_client.LLMError):
        llm_client.call_llm("s", "u", provider="anthropic", api_key="k", max_retries=2)
    assert calls == []
