"""Tests for the OpenRouter call path in llm_client.

OpenRouter is the OpenAI-compatible gateway to DeepSeek, Gemini, Qwen, etc.
We reuse the openai SDK with base_url=https://openrouter.ai/api/v1. These
tests pin down the wire-level contract: base_url, JSON-mode kwarg, X-Title
header, usage mapping (incl. DeepSeek's prefix-cache fields), rate-limit
translation, content (not reasoning) read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# The OpenRouter path reuses the openai SDK. Skip the whole module (rather than
# erroring at collection) when openai isn't installed — e.g. a minimal CI image
# with only the anthropic provider.
openai = pytest.importorskip("openai")

import llm_client


def _fake_openai(monkeypatch, captured: dict, *, usage=None, content='{"ok": 1}'):
    """Patch openai.OpenAI with a fake that records constructor + create() kwargs.

    captured["init"] gets the OpenAI() kwargs (api_key, base_url, default_headers).
    captured["create"] gets the chat.completions.create() kwargs.
    """

    class FakeCompletions:
        def create(self, **kwargs):
            captured["create"] = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=usage,
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kw):
            captured["init"] = kw
            self.chat = FakeChat()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)


# ── base URL + JSON-mode kwargs ───────────────────────────────────────────────


def test_uses_openrouter_base_url(monkeypatch):
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    llm_client._call_openrouter("SYS", "USER", "deepseek/deepseek-r1", "sk-or-v1-x", 8192)
    assert captured["init"]["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["init"]["api_key"] == "sk-or-v1-x"


def test_sets_x_title_header(monkeypatch):
    """OpenRouter recommends X-Title for fair-use rate limits + attribution."""
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    llm_client._call_openrouter("SYS", "USER", "deepseek/deepseek-r1", "k", 8192)
    headers = captured["init"]["default_headers"]
    assert headers["X-Title"] == "job-hunter-bot"


def test_forces_json_response_format(monkeypatch):
    """JSON mode forced so R1's reasoning preamble doesn't leak into .content."""
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    llm_client._call_openrouter("SYS", "USER", "deepseek/deepseek-r1", "k", 8192)
    assert captured["create"]["response_format"] == {"type": "json_object"}
    assert captured["create"]["model"] == "deepseek/deepseek-r1"
    assert captured["create"]["max_tokens"] == 8192


def test_passes_messages_in_openai_shape(monkeypatch):
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    llm_client._call_openrouter("SYSTEM PROMPT", "USER MSG", "m", "k", 8192)
    msgs = captured["create"]["messages"]
    assert msgs == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "USER MSG"},
    ]


def test_returns_content_not_reasoning(monkeypatch):
    """R1 emits hidden CoT to message.reasoning — we must read .content only.

    If we accidentally returned .reasoning the JSON parser would fail downstream.
    The fake here doesn't even set .reasoning, so the assertion is really:
    we never look for it and never fall back to it.
    """
    captured: dict = {}
    _fake_openai(monkeypatch, captured, content='{"resume_en": "..."}')
    out = llm_client._call_openrouter("S", "U", "deepseek/deepseek-r1", "k", 8192)
    assert out == '{"resume_en": "..."}'


# ── usage mapping ─────────────────────────────────────────────────────────────


def test_usage_maps_cache_hits_to_cache_read(monkeypatch):
    """DeepSeek exposes prompt_cache_hit_tokens via OpenRouter — map to
    cache_read_input_tokens so llm_cost prices the cached portion correctly."""
    captured: dict = {}
    usage = SimpleNamespace(
        prompt_tokens=10_000,
        completion_tokens=2_000,
        prompt_cache_hit_tokens=8_000,
    )
    _fake_openai(monkeypatch, captured, usage=usage)
    log = llm_client.push_usage_log()
    try:
        llm_client._call_openrouter("S", "U", "deepseek/deepseek-r1", "k", 8192)
    finally:
        llm_client.pop_usage_log()
    assert len(log) == 1
    rec = log[0]
    assert rec["model"] == "deepseek/deepseek-r1"
    # 10_000 prompt - 8_000 cache hit = 2_000 truly new input
    assert rec["input_tokens"] == 2_000
    assert rec["cache_read_input_tokens"] == 8_000
    assert rec["output_tokens"] == 2_000
    assert rec["cache_creation_input_tokens"] == 0


def test_usage_handles_missing_cache_fields(monkeypatch):
    """Non-DeepSeek models (Gemini, Qwen) won't return prompt_cache_hit_tokens.
    Mapping must not blow up — input_tokens = prompt_tokens unchanged."""
    captured: dict = {}
    usage = SimpleNamespace(prompt_tokens=500, completion_tokens=100)
    _fake_openai(monkeypatch, captured, usage=usage)
    log = llm_client.push_usage_log()
    try:
        llm_client._call_openrouter("S", "U", "google/gemini-2.5-flash", "k", 8192)
    finally:
        llm_client.pop_usage_log()
    rec = log[0]
    assert rec["input_tokens"] == 500
    assert rec["cache_read_input_tokens"] == 0
    assert rec["output_tokens"] == 100


def test_usage_clamps_negative_input(monkeypatch):
    """Defensive: if cache_hit > prompt_tokens (provider weirdness), clamp to 0
    instead of recording a negative count."""
    captured: dict = {}
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=50,
        prompt_cache_hit_tokens=500,  # absurd, but don't break on it
    )
    _fake_openai(monkeypatch, captured, usage=usage)
    log = llm_client.push_usage_log()
    try:
        llm_client._call_openrouter("S", "U", "deepseek/deepseek-r1", "k", 8192)
    finally:
        llm_client.pop_usage_log()
    assert log[0]["input_tokens"] == 0


def test_no_usage_no_record(monkeypatch):
    """Mocked SDK with usage=None must not crash; record is simply skipped."""
    captured: dict = {}
    _fake_openai(monkeypatch, captured, usage=None)
    log = llm_client.push_usage_log()
    try:
        out = llm_client._call_openrouter("S", "U", "m", "k", 8192)
    finally:
        llm_client.pop_usage_log()
    assert out == '{"ok": 1}'
    assert log == []


# ── error translation ────────────────────────────────────────────────────────


def _fake_response(code: int):
    """openai SDK needs response.request for its error __init__."""
    return SimpleNamespace(status_code=code, request=SimpleNamespace(), headers={})


def test_rate_limit_translated(monkeypatch):
    """openai.RateLimitError → LLMRateLimitError so call_llm's retry kicks in."""

    def raise_rate(**kw):
        raise openai.RateLimitError(message="rate", response=_fake_response(429), body=None)

    class RateLimitClient:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=raise_rate))

    monkeypatch.setattr(openai, "OpenAI", RateLimitClient)
    with pytest.raises(llm_client.LLMRateLimitError):
        llm_client._call_openrouter("S", "U", "m", "k", 8192)


def test_retryable_status_translated(monkeypatch):
    """503/529 → LLMRateLimitError (retried by call_llm); other 4xx → LLMError."""

    def make_client(code):
        def raise_status(**kw):
            raise openai.APIStatusError(
                message=f"http {code}",
                response=_fake_response(code),
                body=None,
            )

        class BoomClient:
            def __init__(self, **kw):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=raise_status))

        return BoomClient

    # 503 — retryable
    monkeypatch.setattr(openai, "OpenAI", make_client(503))
    with pytest.raises(llm_client.LLMRateLimitError):
        llm_client._call_openrouter("S", "U", "m", "k", 8192)

    # 400 — permanent
    monkeypatch.setattr(openai, "OpenAI", make_client(400))
    with pytest.raises(llm_client.LLMError) as exc_info:
        llm_client._call_openrouter("S", "U", "m", "k", 8192)
    assert not isinstance(exc_info.value, llm_client.LLMRateLimitError)


# ── dispatch through call_llm ─────────────────────────────────────────────────


def test_call_llm_dispatches_to_openrouter(monkeypatch):
    """provider='openrouter' must route through _call_openrouter, not openai/anthropic."""
    captured: dict = {}
    _fake_openai(monkeypatch, captured, content='{"routed": true}')
    out = llm_client.call_llm(
        "SYS",
        "USER",
        provider="openrouter",
        model="deepseek/deepseek-r1",
        api_key="sk-or-v1-x",
    )
    assert out == {"routed": True}
    assert captured["init"]["base_url"] == "https://openrouter.ai/api/v1"
