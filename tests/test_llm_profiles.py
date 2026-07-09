"""Tests for hunter.llm_profiles — profile registry and active-profile resolution."""

import pytest

import hunter.llm_profiles as lp


# ── Profile registry ────────────────────────────────────────────────────────────

def test_all_expected_profiles_present():
    names = set(lp.PROFILES)
    assert "sonnet" in names
    assert "deepseek-r1" in names
    assert "deepseek-v3" in names
    assert "gpt-4.1" in names
    assert "gpt-4.1-mini" in names
    assert "gpt-4o" in names


def test_gpt_profiles_use_openai_provider():
    for name in ("gpt-4.1", "gpt-4.1-mini", "gpt-4o"):
        p = lp.PROFILES[name]
        assert p.provider == "openai", f"{name} should use provider='openai'"
        assert p.env_key == "OPENAI_API_KEY", f"{name} env_key should be OPENAI_API_KEY"


def test_gpt_models_map_to_correct_model_ids():
    assert lp.PROFILES["gpt-4.1"].model == "gpt-4.1"
    assert lp.PROFILES["gpt-4.1-mini"].model == "gpt-4.1-mini"
    assert lp.PROFILES["gpt-4o"].model == "gpt-4o"


def test_deepseek_profiles_use_openrouter():
    assert lp.PROFILES["deepseek-r1"].provider == "openrouter"
    assert lp.PROFILES["deepseek-v3"].provider == "openrouter"
    assert lp.PROFILES["deepseek-r1"].env_key == "OPENROUTER_API_KEY"


def test_openrouter_2026_profiles_present():
    """deepseek-v4-pro + glm-5.2 (added 2026-07 for the dual-apply A/B)."""
    for name, model in (
        ("deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
        ("glm-5.2", "z-ai/glm-5.2"),
    ):
        p = lp.PROFILES[name]
        assert p.provider == "openrouter"
        assert p.model == model
        assert p.env_key == "OPENROUTER_API_KEY"


# ── API-key availability ────────────────────────────────────────────────────────

def test_profile_is_available_when_env_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    assert lp.PROFILES["gpt-4.1"].is_available()
    assert lp.PROFILES["gpt-4.1-mini"].is_available()
    assert lp.PROFILES["gpt-4o"].is_available()


def test_profile_unavailable_when_env_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert not lp.PROFILES["gpt-4.1"].is_available()


def test_profile_falls_back_to_llm_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-generic")
    assert lp.PROFILES["gpt-4.1"].is_available()
    assert lp.PROFILES["gpt-4.1"].api_key == "sk-generic"


# ── get_active() resolution order ──────────────────────────────────────────────

def test_get_active_prefers_llm_default_profile_env(monkeypatch, tmp_path):
    """LLM_DEFAULT_PROFILE wins over backward-compat LLM_PROVIDER+LLM_MODEL."""
    # Patch DB to return nothing (simulating empty DB)
    monkeypatch.setattr(lp, "_db_get", lambda _key: None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_DEFAULT_PROFILE", "gpt-4.1")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    p = lp.get_active()
    assert p.name == "gpt-4.1"


def test_get_active_falls_back_to_first_available(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _key: None)
    monkeypatch.delenv("LLM_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    p = lp.get_active()
    # gpt-4.1 is the first GPT profile in the registry order, but we
    # just need *something* from OpenAI since only that key is set.
    assert p.provider == "openai"


def test_get_active_db_overrides_env(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _key: "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.delenv("LLM_DEFAULT_PROFILE", raising=False)
    p = lp.get_active()
    assert p.name == "gpt-4o"


# ── Cost estimate ───────────────────────────────────────────────────────────────

def test_cost_estimate_returns_string_with_dollar():
    for name, prof in lp.PROFILES.items():
        est = prof.cost_estimate()
        assert est.startswith("~$"), f"{name}.cost_estimate() = {est!r}"


def test_cost_estimate_gpt_mini_cheapest_gpt():
    mini = lp.PROFILES["gpt-4.1-mini"].cost_estimate()
    full = lp.PROFILES["gpt-4.1"].cost_estimate()
    # Extract dollar amounts for comparison
    mini_val = float(mini.replace("~$", "").replace("/vacancy", ""))
    full_val = float(full.replace("~$", "").replace("/vacancy", ""))
    assert mini_val < full_val


# ── set_active validation ───────────────────────────────────────────────────────

def test_set_active_unknown_name_raises(monkeypatch):
    monkeypatch.setattr(lp, "_db_set", lambda *a: None)
    with pytest.raises(ValueError, match="Unknown profile"):
        lp.set_active("nonexistent-model")


def test_set_active_missing_key_raises(monkeypatch):
    monkeypatch.setattr(lp, "_db_set", lambda *a: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="not available"):
        lp.set_active("gpt-4o")


def test_set_active_persists_and_returns_profile(monkeypatch):
    written = {}
    monkeypatch.setattr(lp, "_db_set", lambda k, v: written.update({k: v}))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    p = lp.set_active("gpt-4.1")
    assert p.name == "gpt-4.1"
    assert written.get(lp._DB_KEY) == "gpt-4.1"
