"""
llm_client.py — Unified LLM caller with retry logic and JSON parsing.

Supports Anthropic and OpenAI providers. Provider/model/key are passed in
so the module stays stateless and testable.
"""

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

# Retry-eligible HTTP status codes
_RETRYABLE = {429, 500, 502, 503, 529}


class LLMError(Exception):
    """Non-retryable LLM error."""


class LLMRateLimitError(LLMError):
    """Rate limit or overloaded — retryable."""


def call_llm(
    system_prompt: str,
    user_message: str,
    provider: str = "anthropic",
    model: str = "claude-3-5-haiku-20241022",
    api_key: str = "",
    max_retries: int = 3,
    max_tokens: int = 8192,
) -> dict:
    """Send prompt to LLM and return parsed JSON dict.

    Retries on 429 / 5xx with exponential backoff.
    Raises LLMError on permanent failure or invalid JSON.
    """
    if not api_key:
        raise LLMError(f"No API key provided for {provider}")

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            if provider == "anthropic":
                raw = _call_anthropic(system_prompt, user_message, model, api_key, max_tokens)
            elif provider == "openai":
                raw = _call_openai(system_prompt, user_message, model, api_key, max_tokens)
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

def _call_anthropic(system: str, user: str, model: str, key: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError:
        raise LLMError("Package 'anthropic' not installed. Run: pip install anthropic")

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
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
        return response.choices[0].message.content
    except openai.RateLimitError as e:
        raise LLMRateLimitError(str(e)) from e
    except openai.APIStatusError as e:
        if e.status_code in _RETRYABLE:
            raise LLMRateLimitError(str(e)) from e
        raise LLMError(f"OpenAI API error {e.status_code}: {e}") from e


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
