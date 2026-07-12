"""Tests for the Anthropic call path in llm_client: prompt caching + effort gating."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic

import llm_client


def _fake_anthropic(monkeypatch, captured: dict):
    """Patch anthropic.Anthropic with a fake that records create() kwargs."""

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": 1}')])

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)


# ── model-capability gating ───────────────────────────────────────────────────


def test_supports_effort():
    assert llm_client._supports_effort("claude-sonnet-4-6")
    assert llm_client._supports_effort("claude-opus-4-8")
    assert llm_client._supports_effort("claude-fable-5")
    # Haiku 4.5, Sonnet 4 (dated), Sonnet 4.5 do NOT support effort
    assert not llm_client._supports_effort("claude-haiku-4-5")
    assert not llm_client._supports_effort("claude-sonnet-4-20250514")
    assert not llm_client._supports_effort("claude-sonnet-4-5")


def test_supports_disabled_thinking():
    assert llm_client._supports_disabled_thinking("claude-sonnet-4-6")
    assert llm_client._supports_disabled_thinking("claude-opus-4-8")
    # Fable 5 400s on explicit disabled; Haiku doesn't get it either
    assert not llm_client._supports_disabled_thinking("claude-fable-5")
    assert not llm_client._supports_disabled_thinking("claude-haiku-4-5")


# ── prompt caching (always applied) ───────────────────────────────────────────


def test_system_prompt_is_cache_wrapped(monkeypatch):
    captured: dict = {}
    _fake_anthropic(monkeypatch, captured)
    out = llm_client._call_anthropic("SYSTEM", "USER", "claude-sonnet-4-6", "key", 8192)
    assert out == '{"ok": 1}'
    sys_blocks = captured["system"]
    assert isinstance(sys_blocks, list)
    assert sys_blocks[0]["text"] == "SYSTEM"
    assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}


# ── effort / thinking gating in the actual call ───────────────────────────────


def test_sonnet46_gets_effort_and_disabled_thinking(monkeypatch):
    captured: dict = {}
    _fake_anthropic(monkeypatch, captured)
    llm_client._call_anthropic("S", "U", "claude-sonnet-4-6", "key", 8192, effort="low")
    assert captured["output_config"] == {"effort": "low"}
    assert captured["thinking"] == {"type": "disabled"}


def test_haiku_judge_call_omits_effort_and_thinking(monkeypatch):
    """The judge runs on Haiku 4.5 — effort/thinking must be omitted (else 400),
    but caching still applies."""
    captured: dict = {}
    _fake_anthropic(monkeypatch, captured)
    llm_client._call_anthropic("S", "U", "claude-haiku-4-5", "key", 2048, effort="low")
    assert "output_config" not in captured
    assert "thinking" not in captured
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_effort_empty_string_skips_param(monkeypatch):
    captured: dict = {}
    _fake_anthropic(monkeypatch, captured)
    llm_client._call_anthropic("S", "U", "claude-sonnet-4-6", "key", 8192, effort="")
    assert "output_config" not in captured
    # thinking is gated only by model, not effort → still present on sonnet-4-6
    assert captured["thinking"] == {"type": "disabled"}


def test_call_llm_threads_effort_through(monkeypatch):
    """call_llm(effort=...) reaches the anthropic create() as output_config."""
    captured: dict = {}
    _fake_anthropic(monkeypatch, captured)
    result = llm_client.call_llm(
        "sys",
        "usr",
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="key",
        effort="medium",
    )
    assert result == {"ok": 1}
    assert captured["output_config"] == {"effort": "medium"}
