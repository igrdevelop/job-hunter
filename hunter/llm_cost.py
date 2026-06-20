"""Per-call LLM cost accounting (Anthropic pricing as of 2026-06).

The apply pipeline fires 7–14 LLM calls per vacancy (generation, ATS rewrite
rounds, cover-letter self-review, claim-judge verification + repair, …). We
need per-vacancy USD so we can see where the money goes — and so the user
can compare what we record against the Anthropic Console total. Console
breakdowns are flat per-day; ours are per-vacancy.

Pricing per 1M tokens. Numbers reflect public Anthropic rates as of 2026-06;
update PRICING when rates move. Substring match on the model id so dated
snapshots (sonnet-4-6, opus-4-7, …) and aliases all resolve. The least-
specific match wins ONLY if nothing more specific matched (sonnet-4-6
overrides sonnet-4 overrides sonnet).

Cache mechanics (Anthropic ephemeral cache):
  cache_creation_input_tokens : written to the cache this turn  (paid at write rate)
  cache_read_input_tokens     : read from a previously-written cache (paid at read rate)
  input_tokens                : non-cached input (paid at base input rate)
  output_tokens               : generation (paid at output rate)

We sum each bucket separately so a sudden cache-miss spike shows up clearly
in the per-vacancy breakdown.
"""

from __future__ import annotations

from typing import Iterable

# Per-1M-token rates in USD. Keys are substrings of the model id (lowercase).
# Order does not matter — _resolve_pricing picks the longest matching key.
PRICING: dict[str, dict[str, float]] = {
    # Sonnet 4 family — current generator default.
    "sonnet-4": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    # Haiku 4.5 — judge.
    "haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "haiku-4": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    # Opus 4.x — not currently used in the apply pipeline but priced for completeness.
    "opus-4": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    # Fable 5 — used if explicitly opted into.
    "fable-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    # DeepSeek (via OpenRouter). Numbers are approximate — OpenRouter applies a
    # small markup on top of the provider's published rates, and the dashboard
    # is the source of truth for actual billing. We bake in approximate listed
    # rates so per-vacancy telemetry stays meaningful (off by a few percent at
    # worst). cache_write left at the input rate — DeepSeek's auto-cache writes
    # are not separately billed but the field has to be non-zero or a partial
    # log would price the same payload as $0.00.
    "deepseek-r1":   {"input": 0.55, "output": 2.19, "cache_write": 0.55, "cache_read": 0.14},
    "deepseek-chat": {"input": 0.27, "output": 1.10, "cache_write": 0.27, "cache_read": 0.07},
}

# Fallback used when the model id matches nothing in PRICING. We pick
# Sonnet-tier rates so an unknown model yields an over-estimate rather than
# a free pass — a $0.00 row when reality is $0.10 hides the regression.
_FALLBACK = PRICING["sonnet-4"]


def _resolve_pricing(model: str) -> dict[str, float]:
    """Return per-1M-token rates for `model`. Longest matching key wins."""
    m = (model or "").lower()
    if not m:
        return _FALLBACK
    best_key = ""
    for key in PRICING:
        if key in m and len(key) > len(best_key):
            best_key = key
    return PRICING[best_key] if best_key else _FALLBACK


def usd_for_call(model: str, usage: dict) -> float:
    """Return USD cost for a single call given its anthropic usage dict.

    usage carries any of: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens — missing keys are
    treated as zero. Unknown extra keys are ignored.
    """
    rates = _resolve_pricing(model)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    return (
        inp * rates["input"]
        + out * rates["output"]
        + cw * rates["cache_write"]
        + cr * rates["cache_read"]
    ) / 1_000_000


def price_usage(log: Iterable[dict]) -> dict:
    """Aggregate a list of call records into a summary.

    Each record has keys: model, input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens. Returns:
      {
        "total_usd":   0.4712,
        "by_model":    {"claude-sonnet-4-6": 0.4521, "claude-haiku-4-5": 0.0191},
        "calls":       8,
        "input_tokens": 12345, "output_tokens": 4567,
        "cache_read_tokens": 87654, "cache_write_tokens": 25000,
      }
    Rounded to 4 decimals for display stability. Empty log → zeroed dict.
    """
    by_model: dict[str, float] = {}
    total = 0.0
    n_calls = 0
    sum_in = sum_out = sum_cr = sum_cw = 0
    for rec in log:
        if not isinstance(rec, dict):
            continue
        model = str(rec.get("model") or "")
        cost = usd_for_call(model, rec)
        by_model[model] = by_model.get(model, 0.0) + cost
        total += cost
        n_calls += 1
        sum_in += int(rec.get("input_tokens", 0) or 0)
        sum_out += int(rec.get("output_tokens", 0) or 0)
        sum_cr += int(rec.get("cache_read_input_tokens", 0) or 0)
        sum_cw += int(rec.get("cache_creation_input_tokens", 0) or 0)
    return {
        "total_usd": round(total, 4),
        "by_model": {k: round(v, 4) for k, v in by_model.items()},
        "calls": n_calls,
        "input_tokens": sum_in,
        "output_tokens": sum_out,
        "cache_read_tokens": sum_cr,
        "cache_write_tokens": sum_cw,
    }


def format_summary(cost: dict) -> str:
    """One-line summary for the Telegram notification."""
    total = cost.get("total_usd", 0.0)
    calls = cost.get("calls", 0)
    return f"Cost: ${total:.4f} ({calls} LLM call{'s' if calls != 1 else ''})"
