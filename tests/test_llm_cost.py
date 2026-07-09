"""Unit tests for hunter.llm_cost — pricing + log aggregation."""

import pytest

from hunter.llm_cost import (
    PRICING,
    _resolve_pricing,
    format_summary,
    price_usage,
    usd_for_call,
)


def test_resolve_pricing_longest_match_wins() -> None:
    # haiku-4-5 is more specific than haiku-4; both contain "haiku".
    rates = _resolve_pricing("claude-haiku-4-5-20251001")
    assert rates is PRICING["haiku-4-5"]
    rates_4 = _resolve_pricing("claude-haiku-4-1")
    assert rates_4 is PRICING["haiku-4"]


def test_resolve_pricing_sonnet_dated_snapshot() -> None:
    assert _resolve_pricing("claude-sonnet-4-20250514") is PRICING["sonnet-4"]
    assert _resolve_pricing("claude-sonnet-4-6") is PRICING["sonnet-4"]


def test_resolve_pricing_deepseek_via_openrouter() -> None:
    """OpenRouter prefixes model ids ('deepseek/deepseek-r1') — substring match
    must still pick R1/V3-specific rates rather than the Sonnet fallback."""
    assert _resolve_pricing("deepseek/deepseek-r1") is PRICING["deepseek-r1"]
    assert _resolve_pricing("deepseek/deepseek-chat") is PRICING["deepseek-chat"]
    # And the bare ids (in case some caller drops the prefix)
    assert _resolve_pricing("deepseek-r1") is PRICING["deepseek-r1"]


def test_resolve_pricing_v4_pro_and_glm() -> None:
    """2026-07 shadow candidates: v4-pro must not fall through to the shorter
    'deepseek-chat'/'deepseek-r1' keys or the Sonnet fallback; glm-5.2 likewise."""
    assert _resolve_pricing("deepseek/deepseek-v4-pro") is PRICING["deepseek-v4-pro"]
    assert _resolve_pricing("z-ai/glm-5.2") is PRICING["glm-5.2"]
    assert PRICING["deepseek-v4-pro"]["output"] == 0.87
    assert PRICING["glm-5.2"]["output"] == 1.98


def test_deepseek_r1_per_call_cost() -> None:
    """Pin the arithmetic on R1 so an accidental rate edit shows up in CI.

    1000 input + 2000 output + 5000 cache_read on R1:
      (1000 * 0.55 + 2000 * 2.19 + 5000 * 0.14) / 1_000_000
      = (550 + 4380 + 700) / 1M = 0.00563
    """
    cost = usd_for_call("deepseek/deepseek-r1", {
        "input_tokens": 1000,
        "output_tokens": 2000,
        "cache_read_input_tokens": 5000,
    })
    assert abs(cost - 0.00563) < 1e-6


def test_resolve_pricing_unknown_falls_back_to_sonnet_rates() -> None:
    # Unknown model → fallback rates. A future model that we forgot to add
    # should be over-estimated rather than reported as free.
    rates = _resolve_pricing("claude-future-9")
    assert rates == PRICING["sonnet-4"]


def test_usd_for_call_sonnet_basic() -> None:
    # 1000 input tokens at $3/M = $0.003; 500 output at $15/M = $0.0075. Total $0.0105.
    usage = {"input_tokens": 1000, "output_tokens": 500}
    assert usd_for_call("claude-sonnet-4-6", usage) == pytest.approx(0.0105, abs=1e-9)


def test_usd_for_call_includes_cache_tokens() -> None:
    # cache_write at 1.25× input rate, cache_read at 0.1× input rate.
    usage = {
        "input_tokens": 1000,         # 1000 * 3 / 1M = 0.003
        "output_tokens": 0,
        "cache_creation_input_tokens": 1000,  # 1000 * 3.75 / 1M = 0.00375
        "cache_read_input_tokens": 1000,      # 1000 * 0.30 / 1M = 0.0003
    }
    assert usd_for_call("claude-sonnet-4-6", usage) == pytest.approx(0.00705, abs=1e-9)


def test_usd_for_call_haiku_cheaper() -> None:
    usage = {"input_tokens": 1000, "output_tokens": 500}
    sonnet = usd_for_call("claude-sonnet-4-6", usage)
    haiku = usd_for_call("claude-haiku-4-5-20251001", usage)
    # Haiku $1 / $5 vs Sonnet $3 / $15 — exactly 1/3 the cost on a pure tokens basis.
    assert haiku == pytest.approx(sonnet / 3, abs=1e-9)


def test_usd_for_call_missing_keys_treated_as_zero() -> None:
    assert usd_for_call("sonnet-4-6", {}) == 0.0
    assert usd_for_call("sonnet-4-6", {"input_tokens": None}) == 0.0


def test_price_usage_aggregates_by_model_and_total() -> None:
    log = [
        {"model": "claude-sonnet-4-6",
         "input_tokens": 30000, "output_tokens": 7000,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        {"model": "claude-sonnet-4-6",
         "input_tokens": 0, "output_tokens": 5000,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 30000},
        {"model": "claude-haiku-4-5-20251001",
         "input_tokens": 2000, "output_tokens": 800,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    ]
    out = price_usage(log)

    assert out["calls"] == 3
    assert out["input_tokens"] == 32000
    assert out["output_tokens"] == 12800
    assert out["cache_read_tokens"] == 30000
    assert "claude-sonnet-4-6" in out["by_model"]
    assert "claude-haiku-4-5-20251001" in out["by_model"]
    # Sonnet dominates the total — Haiku is single-digit-percent.
    assert out["by_model"]["claude-sonnet-4-6"] > out["by_model"]["claude-haiku-4-5-20251001"] * 5
    assert out["total_usd"] == pytest.approx(
        sum(out["by_model"].values()), abs=1e-4
    )


def test_price_usage_empty_log_returns_zero_totals() -> None:
    out = price_usage([])
    assert out["total_usd"] == 0.0
    assert out["calls"] == 0
    assert out["by_model"] == {}
    assert out["input_tokens"] == 0
    assert out["output_tokens"] == 0


def test_price_usage_tolerates_garbage_entries() -> None:
    # Garbage records (non-dict, missing model) silently skipped — we should
    # never crash the apply pipeline because the LLM SDK returned something
    # unexpected.
    log = [
        None,                                      # type: ignore[list-item]
        "broken",                                  # type: ignore[list-item]
        {"model": "sonnet-4-6", "input_tokens": 100, "output_tokens": 50},
    ]
    out = price_usage(log)
    assert out["calls"] == 1
    assert out["total_usd"] > 0


def test_resolve_pricing_gpt_models() -> None:
    """GPT model ids resolve to their specific rate entries, not the Sonnet fallback."""
    assert _resolve_pricing("gpt-4.1") is PRICING["gpt-4.1"]
    assert _resolve_pricing("gpt-4.1-mini") is PRICING["gpt-4.1-mini"]
    assert _resolve_pricing("gpt-4o") is PRICING["gpt-4o"]
    # gpt-4.1-mini is more specific than gpt-4.1 — longest match wins
    assert _resolve_pricing("gpt-4.1-mini") is not PRICING["gpt-4.1"]


def test_gpt_4_1_mini_per_call_cost() -> None:
    """Pin gpt-4.1-mini arithmetic: 1000 in + 2000 out = $0.40*1 + $1.60*2 / 1M = $0.0036."""
    cost = usd_for_call("gpt-4.1-mini", {"input_tokens": 1000, "output_tokens": 2000})
    assert abs(cost - (1000 * 0.40 + 2000 * 1.60) / 1_000_000) < 1e-9


def test_gpt_models_cheaper_than_sonnet() -> None:
    """gpt-4.1-mini should be substantially cheaper than Sonnet per token."""
    usage = {"input_tokens": 10000, "output_tokens": 5000}
    mini = usd_for_call("gpt-4.1-mini", usage)
    sonnet = usd_for_call("claude-sonnet-4-6", usage)
    assert mini < sonnet  # ~$0.012 vs ~$0.105


def test_format_summary_renders_total_and_call_count() -> None:
    cost = {"total_usd": 0.0473, "calls": 8}
    s = format_summary(cost)
    assert "$0.0473" in s
    assert "8 LLM call" in s
    # No trailing 's' on singular call
    cost = {"total_usd": 0.0050, "calls": 1}
    assert "1 LLM call" in format_summary(cost)
    assert "calls)" not in format_summary(cost)
