"""
hunter/pipeline/profiles.py — LLM profile resolution for the apply pipeline.
Moved out of hunter/apply_shared.py (docs/GENERATION_ARCHITECTURE_ANALYSIS.md
wave 1) — see hunter.apply_shared for the backward-compat re-export.
"""

from __future__ import annotations


def _llm_p():
    """Return the currently active LLM profile. Resolved fresh each call so a
    /llm switch in Telegram takes effect on the next vacancy without restart."""
    from hunter.llm_profiles import get_active

    return get_active()


def _translate_p():
    """Resolve the translate profile (Haiku-tier by default — mechanical
    PL<->EN translation, not worth the main profile's $/output-token rate).
    See docs/LLM_COST_REDUCTION_PLAN.md M5. Falls back to the main LLM
    profile when no translate key resolves (TRANSLATE_API_KEY unset AND no
    ANTHROPIC_API_KEY/LLM_API_KEY fallback) — a translation call must never
    fail outright just because the cheaper profile has no key configured."""
    from hunter.config import TRANSLATE_API_KEY, TRANSLATE_MODEL, TRANSLATE_PROVIDER

    if not TRANSLATE_API_KEY:
        return _llm_p()
    from types import SimpleNamespace

    return SimpleNamespace(
        provider=TRANSLATE_PROVIDER, model=TRANSLATE_MODEL, api_key=TRANSLATE_API_KEY
    )
