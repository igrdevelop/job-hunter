"""Loader behaviour for hunter.gen_profile (docs/
GENERATION_ARCHITECTURE_ANALYSIS.md §6 wave 3).

Covers: missing file = builtins, YAML merge, unknown key/section warn,
type/range/choices validation, env-override priority (env > YAML > builtin),
and mtime-based cache invalidation. Mirrors tests/test_filter_profile.py.
"""

from __future__ import annotations

import logging
import time

import pytest
import yaml

from hunter import gen_profile
from hunter.gen_profile import (
    builtin_defaults,
    clear_gen_profile_cache,
    get,
    load_gen_profile,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_gen_profile_cache()
    yield
    clear_gen_profile_cache()


def _write_yaml(path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_missing_file_returns_builtins(tmp_path):
    missing = tmp_path / "nope.yaml"
    assert load_gen_profile(missing) == builtin_defaults()


def test_yaml_overrides_builtin(tmp_path):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"ats": {"threshold": 90}, "judge": {"mode": "block"}})
    profile = load_gen_profile(path)
    assert profile["ats"]["threshold"] == 90.0
    assert profile["judge"]["mode"] == "block"
    # untouched keys keep Layer 1
    assert profile["ats"]["honest_rounds"] == 2
    assert profile["gates"]["doomed_enabled"] is True


def test_unknown_key_warn_and_ignore(tmp_path, caplog):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"ats": {"not_a_real_knob": 123, "threshold": 80}})
    with caplog.at_level(logging.WARNING, logger="hunter.gen_profile"):
        profile = load_gen_profile(path)
    assert profile["ats"]["threshold"] == 80.0
    assert "not_a_real_knob" not in profile["ats"]
    assert any("unknown key" in r.message for r in caplog.records)


def test_unknown_section_warn_and_ignore(tmp_path, caplog):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"bogus_section": {"x": 1}})
    with caplog.at_level(logging.WARNING, logger="hunter.gen_profile"):
        profile = load_gen_profile(path)
    assert "bogus_section" not in profile
    assert any("unknown key" in r.message for r in caplog.records)


def test_non_mapping_section_warn_and_ignore(tmp_path, caplog):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"ats": "not-a-mapping"})
    with caplog.at_level(logging.WARNING, logger="hunter.gen_profile"):
        profile = load_gen_profile(path)
    assert profile["ats"] == builtin_defaults()["ats"]
    assert any("must be a mapping" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "override,expected_default",
    [
        ({"ats": {"threshold": "not-a-number"}}, 95.0),
        ({"ats": {"honest_rounds": -1}}, 2),
        ({"ats": {"honest_rounds": True}}, 2),  # bool is not an int here
        ({"gates": {"prescreen_min_confidence": 1.5}}, 0.9),
        ({"gates": {"prescreen_min_confidence": -0.1}}, 0.9),
        ({"judge": {"mode": "yolo"}}, "warn"),
        ({"gates": {"doomed_hard_action": "delete"}}, "skip"),
        ({"verdict": {"enabled": "true"}}, True),  # string, not bool
    ],
)
def test_invalid_value_keeps_default(tmp_path, caplog, override, expected_default):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, override)
    with caplog.at_level(logging.WARNING, logger="hunter.gen_profile"):
        profile = load_gen_profile(path)
    section, leaf = next(iter(override.items()))
    key = next(iter(leaf.keys()))
    assert profile[section][key] == expected_default
    assert caplog.records


def test_root_not_a_mapping(tmp_path, caplog):
    path = tmp_path / "generation.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="hunter.gen_profile"):
        profile = load_gen_profile(path)
    assert profile == builtin_defaults()
    assert any("must be a mapping" in r.message for r in caplog.records)


def test_env_overrides_yaml_overrides_builtin(tmp_path, monkeypatch):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"verdict": {"target": 80}})

    # No env set: YAML wins over builtin.
    assert load_gen_profile(path)["verdict"]["target"] == 80.0

    # Env set: env wins over YAML.
    monkeypatch.setenv("ATS_VERDICT_TARGET", "55")
    assert load_gen_profile(path)["verdict"]["target"] == 55.0


def test_env_override_applies_even_without_yaml_file(tmp_path, monkeypatch):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setenv("JUDGE_MODE", "BLOCK")
    profile = load_gen_profile(missing)
    assert profile["judge"]["mode"] == "block"


def test_env_override_takes_effect_without_cache_clear(tmp_path, monkeypatch):
    """Env overrides are re-applied on every load, independent of the
    (path, mtime) cache key — a monkeypatched env var must be observed
    immediately, with no file touched at all."""
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {})
    first = load_gen_profile(path)
    assert first["gates"]["repost_window_days"] == 60

    monkeypatch.setenv("REPOST_WINDOW_DAYS", "10")
    second = load_gen_profile(path)
    assert second["gates"]["repost_window_days"] == 10


def test_bad_env_value_raises(tmp_path, monkeypatch):
    """Unlike a bad YAML value (warn + keep default), a malformed env
    override is fatal — env is the emergency lever, and a silently-ignored
    typo there is worse than crashing loudly."""
    monkeypatch.setenv("ATS_VERDICT_MAX_REFINES", "not-an-int")
    with pytest.raises(gen_profile.GenProfileEnvError, match="ATS_VERDICT_MAX_REFINES"):
        load_gen_profile(tmp_path / "nope.yaml")


def test_bad_bool_env_value_does_not_raise(tmp_path, monkeypatch):
    """Bool/str env casters accept any string (matching the pre-gen_profile
    inline `os.getenv(...).lower() in (...)` casts, which were never
    validated against a fixed set either) — only int/float casters can fail
    and raise."""
    monkeypatch.setenv("JUDGE_ENABLED", "definitely-not-a-bool")
    profile = load_gen_profile(tmp_path / "nope.yaml")
    assert profile["judge"]["enabled"] is False


def test_mtime_cache_invalidation(tmp_path):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"ats": {"threshold": 80}})
    first = load_gen_profile(path)
    assert first["ats"]["threshold"] == 80.0

    time.sleep(0.02)
    _write_yaml(path, {"ats": {"threshold": 70}})
    second = load_gen_profile(path)
    assert second["ats"]["threshold"] == 70.0
    # first result was a deepcopy — mutating it must not poison the cache
    first["ats"]["threshold"] = 1.0
    third = load_gen_profile(path)
    assert third["ats"]["threshold"] == 70.0


def test_get_reads_dotpath(tmp_path, monkeypatch):
    path = tmp_path / "generation.yaml"
    _write_yaml(path, {"ats": {"threshold": 88}})
    monkeypatch.setenv("GENERATION_YAML_PATH", str(path))
    assert get("ats.threshold") == 88.0
    assert get("ats.honest_rounds") == 2
    assert get("does.not.exist", "fallback") == "fallback"


def test_get_missing_file_falls_back_to_builtin(tmp_path, monkeypatch):
    """No file, no env override => get() returns the builtin default for a
    known key, and the caller's default for an unknown one."""
    monkeypatch.setenv("GENERATION_YAML_PATH", str(tmp_path / "nope.yaml"))
    assert get("ats.threshold") == 95.0
    assert get("does.not.exist", 12.0) == 12.0


def test_resolve_path_prefers_explicit_env_over_candidate_sibling(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.yaml"
    _write_yaml(explicit, {"ats": {"threshold": 77}})
    candidate_dir = tmp_path / "somewhere"
    candidate_dir.mkdir()
    monkeypatch.setenv("CANDIDATE_YAML_PATH", str(candidate_dir / "candidate.yaml"))
    monkeypatch.setenv("GENERATION_YAML_PATH", str(explicit))
    assert get("ats.threshold") == 77.0


def test_resolve_path_falls_back_to_candidate_sibling(tmp_path, monkeypatch):
    candidate_dir = tmp_path / "somewhere"
    candidate_dir.mkdir()
    sibling = candidate_dir / "generation.yaml"
    _write_yaml(sibling, {"ats": {"threshold": 66}})
    monkeypatch.delenv("GENERATION_YAML_PATH", raising=False)
    monkeypatch.setenv("CANDIDATE_YAML_PATH", str(candidate_dir / "candidate.yaml"))
    assert get("ats.threshold") == 66.0
