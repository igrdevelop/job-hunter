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
import os
import random
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

        _USAGE_STACK[-1].append(
            {
                "model": model,
                "input_tokens": _get("input_tokens"),
                "output_tokens": _get("output_tokens"),
                "cache_creation_input_tokens": _get("cache_creation_input_tokens"),
                "cache_read_input_tokens": _get("cache_read_input_tokens"),
            }
        )
    except Exception as e:
        logger.warning("[LLM] usage record failed for %s: %s", model, e)


class LLMError(Exception):
    """Non-retryable LLM error."""


class LLMRateLimitError(LLMError):
    """Rate limit or overloaded — retryable."""


class LLMOutageError(LLMError):
    """Account-level failure: drained balance, bad/rotated key, revoked access.

    Not the vacancy's fault and not transient at call scale — retrying the same
    call is pointless, but the job itself is fine and should be retried once the
    account recovers. Callers map this to APPLY_LLM_OUTAGE_EXIT_CODE (46) so the
    hunt loop stops the batch WITHOUT writing FAIL rows or escalating fail_count
    (docs/LLM_OUTAGE_RESILIENCE_PLAN.md M1). Subclass of LLMError so existing
    `except LLMError` callers keep working unless they opt in.
    """


# Account-level failure signatures (docs/LLM_OUTAGE_RESILIENCE_PLAN.md M1).
# 401/402/403 are always a key/account problem, never a request problem. A 400
# is normally a request bug (must stay a plain LLMError — misclassifying a code
# bug as an outage would retry it forever), EXCEPT the billing-shaped messages:
# Anthropic reports a drained balance as 400 invalid_request_error ("Your credit
# balance is too low…"). OpenAI reports a drained quota as 429 insufficient_quota
# ("…check your plan and billing details") — the message check pulls it out of
# the retry ladder, where it would burn the full ~10-min backoff per vacancy and
# make the outage slower to detect. OpenRouter uses a plain 402.
_OUTAGE_ALWAYS_STATUSES = {401, 402, 403}
_OUTAGE_MSG_RE = re.compile(
    r"credit balance|insufficient[_ ]quota|billing|spend(?:ing)? limit|payment required",
    re.IGNORECASE,
)


def is_outage_signature(status_code: int | None, message: str) -> bool:
    """True if an API error is an account-level outage (billing/auth), shared by
    all three providers so they classify identically."""
    if status_code in _OUTAGE_ALWAYS_STATUSES:
        return True
    return bool(_OUTAGE_MSG_RE.search(message or ""))


# ── CLI (Pro subscription) fallback for account outages ───────────────────────
# docs/LLM_OUTAGE_RESILIENCE_PLAN.md M4b: when LLM_OUTAGE_FALLBACK_CLI is on,
# an LLMOutageError from ANY provider retries the same prompt ONCE through
# `claude -p` (subscription — separate billing pool). Living here, at the one
# choke point every LLM call goes through, it covers the cheap stages too
# (claim judge, PDF verdict, refine rewrites, translate, outreach) — the
# pipeline-level M4 fallback in apply_agent.main() only covered the main
# generation call; the Haiku-tier calls just went best-effort-skipped when the
# Anthropic balance was the one that died.


def _cli_fallback_enabled() -> bool:
    """Flag on AND not inside a dual-apply shadow run.

    The shadow forces a specific generator model via llm_profiles.set_override
    for an A/B comparison — silently serving its calls with the subscription's
    model would poison the comparison, so the shadow never falls back.
    """
    if os.getenv("LLM_OUTAGE_FALLBACK_CLI", "false").lower() not in ("true", "1", "yes"):
        return False
    try:
        from hunter import llm_profiles

        if llm_profiles._override is not None:
            return False
    except Exception:  # noqa: BLE001 — llm_client stays importable standalone
        pass
    return True


def _call_cli_fallback(system_prompt: str, user_message: str) -> dict | None:
    """One `claude -p` run for an outage-hit prompt. Returns the parsed JSON
    dict, or None on ANY failure (no CLI, not logged in, timeout, non-JSON) —
    the caller then re-raises the original LLMOutageError, so a broken fallback
    can never masquerade as a different error class.

    The prompt goes through STDIN, not argv: judge/verdict prompts carry the
    full job text + resume JSON, well past the Windows ~32K argv limit. Plain
    print mode, no --dangerously-skip-permissions — these are pure text-in/
    text-out calls, no tools involved. No usage is recorded (subscription has
    no per-token stats), so the tracker Cost $ column simply won't include
    CLI-served calls.
    """
    import subprocess

    prompt = f"{system_prompt}\n\n---\n\n{user_message}"
    try:
        result = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[LLM] CLI fallback unavailable/failed: %s", e)
        return None
    if result.returncode != 0:
        logger.warning(
            "[LLM] CLI fallback exited %d: %s",
            result.returncode,
            (result.stderr or result.stdout or "")[-300:],
        )
        return None
    try:
        return _parse_json(result.stdout or "")
    except LLMError as e:
        logger.warning("[LLM] CLI fallback returned unparseable output: %s", e)
        return None


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter. attempt is 1-based.

    Schedule (approx): 10s, 20s, 40s, 80s, 160s, 300s (cap), each ±25% jitter.
    """
    base = min(10 * (2 ** (attempt - 1)), 300)
    return base * random.uniform(0.75, 1.25)


def call_llm(
    system_prompt: str,
    user_message: str,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    api_key: str = "",
    max_retries: int = 6,
    max_tokens: int = 8192,
    effort: str = "low",
    fallback_model: str | None = None,
) -> dict:
    """Send prompt to LLM and return parsed JSON dict.

    Retries on 429 / 5xx / 529 with exponential backoff + jitter.
    On overload, after half the retries the call switches to `fallback_model`
    (or env LLM_FALLBACK_MODEL) for the remaining attempts.
    Raises LLMError on permanent failure or invalid JSON.

    `effort` (anthropic only) sets ``output_config.effort`` on models that
    support it (Sonnet 4.6, Opus 4.5+, Fable 5); ``low`` keeps the structured
    generation task fast and cheap. Silently skipped on models without the
    param (e.g. Haiku 4.5 used by the judge) so those calls never 400.
    """
    if not api_key:
        raise LLMError(f"No API key provided for {provider}")

    if fallback_model is None:
        fallback_model = os.getenv("LLM_FALLBACK_MODEL") or None
    switch_after = max(1, max_retries // 2)

    last_err = None
    for attempt in range(1, max_retries + 1):
        active_model = model
        if fallback_model and provider == "anthropic" and attempt > switch_after:
            active_model = fallback_model
        try:
            if provider == "anthropic":
                raw = _call_anthropic(
                    system_prompt, user_message, active_model, api_key, max_tokens, effort=effort
                )
            elif provider == "openai":
                raw = _call_openai(system_prompt, user_message, active_model, api_key, max_tokens)
            elif provider == "openrouter":
                raw = _call_openrouter(
                    system_prompt, user_message, active_model, api_key, max_tokens
                )
            else:
                raise LLMError(f"Unknown LLM provider: {provider}")

            if active_model != model:
                logger.warning(
                    f"[LLM] Succeeded on fallback model {active_model} (attempt {attempt})"
                )
            return _parse_json(raw)

        except LLMRateLimitError as e:
            last_err = e
            if attempt < max_retries:
                wait = _backoff_seconds(attempt)
                next_model = (
                    fallback_model
                    if fallback_model and provider == "anthropic" and (attempt + 1) > switch_after
                    else active_model
                )
                logger.warning(
                    f"[LLM] Overload/rate-limit on {active_model} "
                    f"(attempt {attempt}/{max_retries}), sleeping {wait:.1f}s, next={next_model}"
                )
                time.sleep(wait)
            else:
                raise LLMError(f"Rate limit after {max_retries} retries: {e}") from e

        except LLMOutageError:
            # Account dead (billing/auth) — retrying the API is pointless, but
            # the subscription is a separate billing pool: one `claude -p` shot
            # when the fallback is enabled (M4b). None → re-raise the ORIGINAL
            # outage so exit-46 semantics (stop batch, no FAIL row, pause) hold.
            if _cli_fallback_enabled():
                parsed = _call_cli_fallback(system_prompt, user_message)
                if parsed is not None:
                    logger.warning(
                        f"[LLM] outage on {active_model} — served via Claude CLI fallback"
                    )
                    return parsed
            raise

        except LLMError:
            raise

        except Exception as e:
            last_err = e
            if attempt < max_retries and _is_retryable_exception(e):
                wait = _backoff_seconds(attempt)
                logger.warning(
                    f"[LLM] Retryable error on {active_model} "
                    f"(attempt {attempt}/{max_retries}), sleeping {wait:.1f}s: {e}"
                )
                time.sleep(wait)
            else:
                raise LLMError(f"LLM call failed: {e}") from e

    raise LLMError(f"LLM call failed after {max_retries} retries: {last_err}")


# ── Provider implementations ──────────────────────────────────────────────────

# Models that accept the GA effort param (output_config.effort). Substring match
# on the model id so dated snapshots (…-6, …-4-8) and aliases both resolve.
_EFFORT_MODEL_TAGS = (
    "sonnet-4-6",
    "opus-4-5",
    "opus-4-6",
    "opus-4-7",
    "opus-4-8",
    "fable-5",
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
    system: str,
    user: str,
    model: str,
    key: str,
    max_tokens: int,
    effort: str = "low",
) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("Package 'anthropic' not installed. Run: pip install anthropic") from e

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
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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
        if is_outage_signature(None, str(e)):
            raise LLMOutageError(str(e)) from e
        raise LLMRateLimitError(str(e)) from e
    except anthropic.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        if is_outage_signature(e.status_code, str(e)):
            raise LLMOutageError(f"Anthropic outage ({e.status_code}): {e}") from e
        raise LLMError(f"Anthropic API error {e.status_code}: {e}") from e


def _call_openai(system: str, user: str, model: str, key: str, max_tokens: int) -> str:
    try:
        import openai
    except ImportError as e:
        raise LLMError("Package 'openai' not installed. Run: pip install openai") from e

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
            _record_usage(
                model,
                {
                    "input_tokens": getattr(u, "prompt_tokens", 0),
                    "output_tokens": getattr(u, "completion_tokens", 0),
                },
            )
        return response.choices[0].message.content
    except openai.RateLimitError as e:
        if is_outage_signature(None, str(e)):
            raise LLMOutageError(str(e)) from e
        raise LLMRateLimitError(str(e)) from e
    except openai.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        if is_outage_signature(e.status_code, str(e)):
            raise LLMOutageError(f"OpenAI outage ({e.status_code}): {e}") from e
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
    except ImportError as e:
        raise LLMError("Package 'openai' not installed. Run: pip install openai") from e

    try:
        # R1 reasoning models can take several minutes per call. Set an explicit
        # request timeout (default SDK 600s) to surface hangs as retryable errors
        # rather than silent blocks. APPLY_AGENT_TIMEOUT_SEC (900s) is the outer
        # per-vacancy wall-clock limit; stay well under it at the per-call level.
        import httpx

        client = openai.OpenAI(
            api_key=key,
            base_url=_OPENROUTER_BASE_URL,
            default_headers={"X-Title": "job-hunter-bot"},
            timeout=httpx.Timeout(timeout=300.0, connect=10.0),  # 5-min per call
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
            _record_usage(
                model,
                {
                    "input_tokens": max(0, prompt_tokens - cache_hit),
                    "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                    "cache_read_input_tokens": cache_hit,
                },
            )
        return response.choices[0].message.content
    except openai.RateLimitError as e:
        if is_outage_signature(None, str(e)):
            raise LLMOutageError(str(e)) from e
        raise LLMRateLimitError(str(e)) from e
    except openai.APITimeoutError as e:
        raise LLMRateLimitError(f"OpenRouter timeout: {e}") from e
    except openai.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        if is_outage_signature(e.status_code, str(e)):
            raise LLMOutageError(f"OpenRouter outage ({e.status_code}): {e}") from e
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
