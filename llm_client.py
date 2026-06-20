"""
llm_client.py — Unified LLM caller with retry logic and JSON parsing.

Supports Anthropic and OpenAI providers. Provider/model/key are passed in
so the module stays stateless and testable.

Per-call usage accounting (account_usage / current_log) is a context manager
the apply pipeline wraps around its entire LLM-using flow. When the stack is
non-empty, every successful call_llm appends one record to the innermost
frame's log — model + four anthropic token counters. The caller passes the
log to hunter.llm_cost.price_usage to convert to USD. Zero overhead when
the stack is empty, so non-apply callers (tests, ad-hoc scripts) are
unaffected.
"""

import json
import logging
import re
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Retry-eligible HTTP status codes
_RETRYABLE = {429, 500, 502, 503, 529}

# Stack of active usage logs. The apply pipeline pushes one on entry and
# pops it on exit; nested calls add to the innermost frame (we don't expect
# nesting in practice but the stack keeps account_usage reentrant-safe).
_USAGE_STACK: list[list[dict]] = []


@contextmanager
def account_usage():
    """Context manager: yield a list that gets one record per LLM call inside.

    Each record is a dict with keys: model, input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens. Empty if no LLM
    call ran inside the block. Pass the list to hunter.llm_cost.price_usage
    to convert to USD + per-model breakdown.
    """
    log = push_usage_log()
    try:
        yield log
    finally:
        pop_usage_log()


def push_usage_log() -> list[dict]:
    """Begin a new accounting frame. Returns the list that will collect records.

    Manual counterpart to account_usage() — useful when the pipeline body
    isn't easily nestable under a `with` (apply_api.main_api has a half-dozen
    early returns and multiple sys.exit paths; wrapping it in an explicit
    push/pop pair keeps the diff small).
    """
    log: list[dict] = []
    _USAGE_STACK.append(log)
    return log


def pop_usage_log() -> list[dict] | None:
    """End the innermost accounting frame. Returns the collected log, or None
    if the stack was empty (defensive — calling pop without a matching push
    is a bug but we don't want it to crash the apply pipeline)."""
    return _USAGE_STACK.pop() if _USAGE_STACK else None


def _record_usage(model: str, usage) -> None:
    """Push one usage entry onto the innermost active log, if any.

    Accepts the raw anthropic SDK Usage object (has attribute access) or
    a plain dict. Missing fields default to 0. Best-effort: any error here
    must never affect the call's return value.
    """
    if not _USAGE_STACK:
        return
    try:
        def _get(name: str) -> int:
            if isinstance(usage, dict):
                return int(usage.get(name) or 0)
            return int(getattr(usage, name, 0) or 0)

        _USAGE_STACK[-1].append({
            "model": model,
            "input_tokens": _get("input_tokens"),
            "output_tokens": _get("output_tokens"),
            "cache_creation_input_tokens": _get("cache_creation_input_tokens"),
            "cache_read_input_tokens": _get("cache_read_input_tokens"),
        })
    except Exception as e:
        logger.warning("[LLM] usage record failed for %s: %s", model, e)


class LLMError(Exception):
    """Non-retryable LLM error."""


class LLMRateLimitError(LLMError):
    """Rate limit or overloaded — retryable."""


def call_llm(
    system_prompt: str,
    user_message: str,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    api_key: str = "",
    max_retries: int = 3,
    max_tokens: int = 8192,
    effort: str = "low",
) -> dict:
    """Send prompt to LLM and return parsed JSON dict.

    Retries on 429 / 5xx with exponential backoff.
    Raises LLMError on permanent failure or invalid JSON.

    `effort` (anthropic only) sets ``output_config.effort`` on models that
    support it (Sonnet 4.6, Opus 4.5+, Fable 5); ``low`` keeps the structured
    generation task fast and cheap. Silently skipped on models without the
    param (e.g. Haiku 4.5 used by the judge) so those calls never 400.
    """
    if not api_key:
        raise LLMError(f"No API key provided for {provider}")

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            if provider == "anthropic":
                raw = _call_anthropic(
                    system_prompt, user_message, model, api_key, max_tokens, effort=effort
                )
            elif provider == "openai":
                raw = _call_openai(system_prompt, user_message, model, api_key, max_tokens)
            elif provider == "openrouter":
                raw = _call_openrouter(system_prompt, user_message, model, api_key, max_tokens)
            else:
                raise LLMError(f"Unknown LLM provider: {provider}")

            return _parse_json(raw)

        except LLMRateLimitError as e:
            last_err = e
            if attempt < max_retries:
                wait = min(2 ** attempt * 10, 120)
                logger.warning(f"[LLM] Rate limit (attempt {attempt}/{max_retries}), waiting {wait}s")
                time.sleep(wait)
            else:
                raise LLMError(f"Rate limit after {max_retries} retries: {e}") from e

        except LLMError:
            raise

        except Exception as e:
            last_err = e
            if attempt < max_retries and _is_retryable_exception(e):
                wait = min(2 ** attempt * 10, 120)
                logger.warning(f"[LLM] Retryable error (attempt {attempt}/{max_retries}): {e}")
                time.sleep(wait)
            else:
                raise LLMError(f"LLM call failed: {e}") from e

    raise LLMError(f"LLM call failed after {max_retries} retries: {last_err}")


# ── Provider implementations ──────────────────────────────────────────────────

# Models that accept the GA effort param (output_config.effort). Substring match
# on the model id so dated snapshots (…-6, …-4-8) and aliases both resolve.
_EFFORT_MODEL_TAGS = (
    "sonnet-4-6", "opus-4-5", "opus-4-6", "opus-4-7", "opus-4-8", "fable-5",
)


def _supports_effort(model: str) -> bool:
    """True if `model` accepts output_config.effort (else passing it would 400)."""
    m = (model or "").lower()
    return any(tag in m for tag in _EFFORT_MODEL_TAGS)


def _supports_disabled_thinking(model: str) -> bool:
    """Sonnet 4.6 + Opus 4.5–4.8 accept thinking={'type':'disabled'};
    Fable 5 returns 400 on it (omit the param there instead)."""
    return _supports_effort(model) and "fable" not in (model or "").lower()


def _call_anthropic(
    system: str, user: str, model: str, key: str, max_tokens: int, effort: str = "low",
) -> str:
    try:
        import anthropic
    except ImportError:
        raise LLMError("Package 'anthropic' not installed. Run: pip install anthropic")

    try:
        client = anthropic.Anthropic(api_key=key)
        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            # Cache the large, repeated system prefix (candidate profile + rules +
            # base CV). It is byte-identical across every call within a CV (ATS
            # rewrite loop, cover-letter review, repair passes) and across CVs in a
            # hunt, so after the first write subsequent reads cost ~0.1x — a large
            # saving on the multi-pass apply pipeline.
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": user}],
        }
        # Keep the structured generation fast/cheap on models that support it;
        # gated so judge calls on Haiku (no effort param) never 400.
        if effort and _supports_effort(model):
            create_kwargs["output_config"] = {"effort": effort}
        if _supports_disabled_thinking(model):
            create_kwargs["thinking"] = {"type": "disabled"}
        response = client.messages.create(**create_kwargs)
        _record_usage(model, getattr(response, "usage", None))
        return response.content[0].text
    except anthropic.RateLimitError as e:
        raise LLMRateLimitError(str(e)) from e
    except anthropic.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        raise LLMError(f"Anthropic API error {e.status_code}: {e}") from e


def _call_openai(system: str, user: str, model: str, key: str, max_tokens: int) -> str:
    try:
        import openai
    except ImportError:
        raise LLMError("Package 'openai' not installed. Run: pip install openai")

    try:
        client = openai.OpenAI(api_key=key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
        # OpenAI returns prompt_tokens / completion_tokens — remap to the
        # anthropic-shaped fields so price_usage doesn't need a per-provider
        # branch (cache_*_tokens stay 0 because OpenAI exposes no cache stats
        # in the standard response).
        u = getattr(response, "usage", None)
        if u is not None:
            _record_usage(model, {
                "input_tokens": getattr(u, "prompt_tokens", 0),
                "output_tokens": getattr(u, "completion_tokens", 0),
            })
        return response.choices[0].message.content
    except openai.RateLimitError as e:
        raise LLMRateLimitError(str(e)) from e
    except openai.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        raise LLMError(f"OpenAI API error {e.status_code}: {e}") from e


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _call_openrouter(system: str, user: str, model: str, key: str, max_tokens: int) -> str:
    """Call any model on OpenRouter via the OpenAI-compatible endpoint.

    OpenRouter is the gateway for DeepSeek/Gemini/Qwen/etc. without needing a
    separate account per provider. The wire format is OpenAI chat-completions,
    so we reuse the openai SDK with a custom base_url.

    JSON mode is forced (response_format) — generation_rules.md already tells
    the model to respond with JSON. Reasoning models (R1) emit their CoT to
    a separate message.reasoning field which we ignore — we read .content only,
    so the JSON parser never sees the reasoning trace.

    Usage mapping (DeepSeek via OpenRouter exposes prefix-cache stats):
      prompt_tokens             → input_tokens (minus cache hits)
      prompt_cache_hit_tokens   → cache_read_input_tokens
      completion_tokens         → output_tokens
      cache_creation_input_tokens stays 0 (provider doesn't expose writes).
    """
    try:
        import openai
    except ImportError:
        raise LLMError("Package 'openai' not installed. Run: pip install openai")

    try:
        client = openai.OpenAI(
            api_key=key,
            base_url=_OPENROUTER_BASE_URL,
            default_headers={"X-Title": "job-hunter-bot"},
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        u = getattr(response, "usage", None)
        if u is not None:
            prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
            cache_hit = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)
            _record_usage(model, {
                "input_tokens": max(0, prompt_tokens - cache_hit),
                "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                "cache_read_input_tokens": cache_hit,
            })
        return response.choices[0].message.content
    except openai.RateLimitError as e:
        raise LLMRateLimitError(str(e)) from e
    except openai.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        raise LLMError(f"OpenRouter API error {e.status_code}: {e}") from e


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Extract and parse JSON from LLM output.

    Handles: pure JSON, ```json fenced blocks, text before/after JSON.
    """
    raw = raw.strip()

    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try decoding from each possible JSON-object start position.
    # raw_decode correctly handles nested braces, unlike regex extraction.
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj

    raise LLMError(f"Could not parse JSON from LLM response (first 500 chars): {raw[:500]}")


def _is_retryable_exception(e: Exception) -> bool:
    """Check if a generic exception looks like a transient server issue."""
    msg = str(e).lower()
    return any(kw in msg for kw in ("timeout", "connection", "overloaded", "rate", "503", "529"))
