"""hunter/llm_profiles.py — Named LLM provider profiles with runtime switching.

A profile is a (provider, model, api_key) triple identified by a short name
like "sonnet" or "deepseek-r1". The active profile is persisted in tracker.db
so it survives container restarts and is shared across all apply pipelines
without a bot restart.

Usage in call sites:
    from hunter.llm_profiles import get_active
    p = get_active()
    result = call_llm(..., provider=p.provider, model=p.model, api_key=p.api_key)

Telegram /llm command uses set_active(name) + list_available().
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Profile dataclass ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    model: str
    env_key: str          # env-var name that holds the API key for this provider

    @property
    def api_key(self) -> str:
        return os.getenv(self.env_key, "") or os.getenv("LLM_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def cost_estimate(self) -> str:
        """Rough per-vacancy cost string for display (8 calls, typical token sizes)."""
        from hunter.llm_cost import _resolve_pricing
        rates = _resolve_pricing(self.model)
        # Rough estimate: 8 calls, avg 3k input + 4k output tokens per call
        est = (8 * (3000 * rates["input"] + 4000 * rates["output"])) / 1_000_000
        return f"~${est:.2f}/vacancy"


# ── Profile registry ───────────────────────────────────────────────────────────
# Add a new model here — it becomes available everywhere (API, /llm, cost display)
# without any other code change. The profile is available only if its env_key
# resolves to a non-empty value, so unused providers stay invisible in the UI.

PROFILES: dict[str, Profile] = {
    # ── Anthropic ──────────────────────────────────────────────────────────────
    "sonnet": Profile(
        name="sonnet",
        provider="anthropic",
        model="claude-sonnet-4-6",
        env_key="ANTHROPIC_API_KEY",
    ),
    # ── DeepSeek via OpenRouter ───────────────────────────────────────────────
    "deepseek-r1": Profile(
        name="deepseek-r1",
        provider="openrouter",
        model="deepseek/deepseek-r1",
        env_key="OPENROUTER_API_KEY",
    ),
    "deepseek-v3": Profile(
        name="deepseek-v3",
        provider="openrouter",
        model="deepseek/deepseek-chat",
        env_key="OPENROUTER_API_KEY",
    ),
    # ── OpenAI GPT ────────────────────────────────────────────────────────────
    # Requires OPENAI_API_KEY in .env. Uses the openai SDK directly (no gateway).
    # Pricing as of 2026-06 (openai.com/pricing); update llm_cost.py if rates change.
    "gpt-4.1": Profile(
        name="gpt-4.1",
        provider="openai",
        model="gpt-4.1",
        env_key="OPENAI_API_KEY",
    ),
    "gpt-4.1-mini": Profile(
        name="gpt-4.1-mini",
        provider="openai",
        model="gpt-4.1-mini",
        env_key="OPENAI_API_KEY",
    ),
    "gpt-4o": Profile(
        name="gpt-4o",
        provider="openai",
        model="gpt-4o",
        env_key="OPENAI_API_KEY",
    ),
}

_DB_KEY = "active_llm_profile"
_DUAL_KEY = "dual_apply_enabled"
_DUAL_SHADOW_KEY = "dual_shadow_profile"
_DEFAULT_SHADOW = "deepseek-v3"

# Process-local override of the active profile. Set transiently by the dual-apply
# shadow run so that every building block that resolves through get_active()
# (_ats_check_loop, scrubs, lang-gate) uses the shadow model — without mutating
# the DB-persisted boevoy choice. Always cleared in a finally block by the caller.
_override: "Profile | None" = None


def set_override(profile: "Profile | None") -> None:
    """Temporarily force get_active() to return `profile` (None clears it).

    Used by hunter.dual_apply.run_shadow so the shadow generation runs entirely
    on the shadow model. The shadow runs sequentially AFTER the primary apply
    completes in the same process, so this never affects the boevoy run.
    """
    global _override
    _override = profile


# ── DB persistence (config key-value table) ────────────────────────────────────

def _get_db_path() -> Path:
    from hunter.config import TRACKER_DB_PATH
    return TRACKER_DB_PATH


def _ensure_config_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS config "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()


def _db_get(key: str) -> str | None:
    import sqlite3
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            _ensure_config_table(conn)
            row = conn.execute(
                "SELECT value FROM config WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning("[llm_profiles] DB read failed: %s", e)
        return None


def _db_set(key: str, value: str) -> None:
    import sqlite3
    try:
        with sqlite3.connect(_get_db_path()) as conn:
            _ensure_config_table(conn)
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
    except Exception as e:
        logger.warning("[llm_profiles] DB write failed: %s", e)


# ── Public API ─────────────────────────────────────────────────────────────────

def list_available() -> list[Profile]:
    """Profiles whose API key is present in the environment."""
    return [p for p in PROFILES.values() if p.is_available()]


def get_active() -> Profile:
    """Return the active profile.

    Resolution order:
    1. DB row (active_llm_profile) — set via set_active() or /llm command
    2. LLM_DEFAULT_PROFILE env var
    3. LLM_PROVIDER + LLM_MODEL env vars — backward-compat with existing .env
    4. First available profile in PROFILES registry
    5. Hard-coded "sonnet" fallback (even if unavailable — better than crashing)

    A process-local override (set_override) wins over all of the above — it is
    how the dual-apply shadow run forces the shadow model for its own LLM calls.
    """
    # 0. Process-local override (dual-apply shadow run)
    if _override is not None:
        return _override

    # 1. DB-persisted choice
    name = _db_get(_DB_KEY)
    if name and name in PROFILES and PROFILES[name].is_available():
        return PROFILES[name]

    # 2. Explicit default env var
    name = os.getenv("LLM_DEFAULT_PROFILE", "").strip()
    if name and name in PROFILES and PROFILES[name].is_available():
        return PROFILES[name]

    # 3. Backward-compat: honour LLM_PROVIDER + LLM_MODEL if they match a known profile
    provider = os.getenv("LLM_PROVIDER", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if provider and model:
        for p in PROFILES.values():
            if p.provider == provider and p.model == model and p.is_available():
                return p

    # 4. First available profile
    available = list_available()
    if available:
        return available[0]

    # 5. Hard fallback — returns sonnet even if the key is missing so callers
    #    get a clean "No API key" error from call_llm rather than a KeyError here.
    return PROFILES["sonnet"]


def set_active(name: str) -> Profile:
    """Persist `name` as the active profile. Returns the profile.

    Raises ValueError if the name is unknown or the profile is unavailable
    (missing API key).
    """
    if name not in PROFILES:
        known = ", ".join(PROFILES)
        raise ValueError(f"Unknown profile '{name}'. Known: {known}")
    profile = PROFILES[name]
    if not profile.is_available():
        raise ValueError(
            f"Profile '{name}' is not available — set {profile.env_key} in .env"
        )
    _db_set(_DB_KEY, name)
    logger.info("[llm_profiles] active profile → %s (%s)", name, profile.model)
    return profile


# ── Dual-apply (A/B comparison) mode ────────────────────────────────────────────
# When enabled, after the primary (boevoy) apply produces its docs the bot runs a
# second, side-by-side generation with the shadow profile into a {model} subfolder
# (see hunter.dual_apply). Toggled at runtime via the /dual Telegram command.

def dual_enabled() -> bool:
    """True if dual-apply (shadow comparison) mode is on."""
    return _db_get(_DUAL_KEY) == "1"


def set_dual(enabled: bool) -> None:
    """Persist dual-apply mode on/off in tracker.db."""
    _db_set(_DUAL_KEY, "1" if enabled else "0")
    logger.info("[llm_profiles] dual-apply mode → %s", "on" if enabled else "off")


def shadow_profile() -> Profile | None:
    """The profile used for the shadow generation.

    Resolution: DB (dual_shadow_profile) → DUAL_SHADOW_PROFILE env → default
    ("deepseek-v3"). Returns None if the resolved profile is unknown or its API
    key is missing, so the shadow run quietly no-ops rather than crashing.
    """
    name = (
        _db_get(_DUAL_SHADOW_KEY)
        or os.getenv("DUAL_SHADOW_PROFILE", "").strip()
        or _DEFAULT_SHADOW
    )
    prof = PROFILES.get(name)
    return prof if (prof is not None and prof.is_available()) else None
