"""Loader behaviour for hunter.filter_profile (docs/FILTERS_YAML_PLAN.md M1).

Covers merge strategies, invalid-regex drop, unknown-key warn, home-city
carve-out, and mtime-based cache invalidation.
"""

from __future__ import annotations

import logging
import time

import pytest
import yaml

from hunter import candidate
from hunter.filter_profile import (
    _EXTEND_KEYS,
    _REPLACE_KEYS,
    builtin_defaults,
    clear_profile_cache,
    load_profile,
)


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path):
    candidate._set_path(tmp_path / "missing_candidate.yaml")
    clear_profile_cache()
    yield
    candidate._set_path(None)
    clear_profile_cache()


def _write_filters(path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_missing_file_returns_builtins(tmp_path):
    missing = tmp_path / "nope.yaml"
    assert load_profile(missing) == builtin_defaults()


def test_replace_merge_title_keywords(tmp_path):
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"title_keywords": ["react", "frontend"]})
    profile = load_profile(path)
    assert profile["title_keywords"] == ["react", "frontend"]
    # untouched keys keep Layer 1
    assert profile["exclude_ai_training"] is True


def test_extend_only_exclude_companies(tmp_path):
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"exclude_companies": ["localmill", "micro1"]})
    profile = load_profile(path)
    # calibrated defaults kept; user addition appended; duplicate micro1 not doubled
    assert "micro1" in profile["exclude_companies"]
    assert "alignerr" in profile["exclude_companies"]
    assert "localmill" in profile["exclude_companies"]
    assert profile["exclude_companies"].count("micro1") == 1


def test_extend_only_cannot_remove_defaults(tmp_path):
    """extend_only: omitting a calibrated company does NOT remove it."""
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"exclude_companies": ["only-new"]})
    profile = load_profile(path)
    for name in builtin_defaults()["exclude_companies"]:
        assert name in profile["exclude_companies"]
    assert "only-new" in profile["exclude_companies"]


def test_strategy_table_locked():
    """Calibrated protections stay extend_only — a test locks the strategy table."""
    assert "exclude_companies" in _EXTEND_KEYS
    assert "extra_anti_hybrid_cities" in _EXTEND_KEYS
    assert "exclude_companies" not in _REPLACE_KEYS
    assert "title_keywords" in _REPLACE_KEYS
    assert "exclude_patterns" in _REPLACE_KEYS


def test_invalid_regex_dropped_with_warning(tmp_path, caplog):
    path = tmp_path / "filters.yaml"
    _write_filters(
        path,
        {
            "exclude_patterns": [
                r"\bjava\b",
                r"(unbalanced",
                r"\bphp\b",
            ]
        },
    )
    with caplog.at_level(logging.WARNING, logger="hunter.filter_profile"):
        profile = load_profile(path)
    assert r"\bjava\b" in profile["exclude_patterns"]
    assert r"\bphp\b" in profile["exclude_patterns"]
    assert "(unbalanced" not in profile["exclude_patterns"]
    assert any("invalid regex" in r.message for r in caplog.records)


def test_unknown_key_warn_and_ignore(tmp_path, caplog):
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"not_a_real_knob": 123, "title_keywords": ["angular"]})
    with caplog.at_level(logging.WARNING, logger="hunter.filter_profile"):
        profile = load_profile(path)
    assert "not_a_real_knob" not in profile
    assert profile["title_keywords"] == ["angular"]
    assert any("unknown key" in r.message for r in caplog.records)


def test_derived_locations_ignored_from_user_file(tmp_path, caplog):
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"locations": ["mars"]})
    with caplog.at_level(logging.WARNING, logger="hunter.filter_profile"):
        profile = load_profile(path)
    assert "mars" not in profile["locations"]
    assert "remote" in profile["locations"]
    assert any("derived" in r.message for r in caplog.records)


def test_home_city_carve_out(tmp_path):
    """A user whose home city is in extra_anti_hybrid_cities gets it carved out."""
    cand = tmp_path / "candidate.yaml"
    cand.write_text(
        yaml.safe_dump(
            {
                "location": {
                    "home_city": "Berlin",
                    "home_city_aliases": ["berlin", "berlín"],
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    candidate._set_path(cand)
    clear_profile_cache()

    profile = load_profile()
    lowered = [c.lower() for c in profile["extra_anti_hybrid_cities"]]
    assert "berlin" not in lowered
    # sibling cities still present
    assert "munich" in lowered or "münchen" in lowered
    # home aliases still accepted as locations
    assert "berlin" in [a.lower() for a in profile["locations"]]


def test_mtime_cache_invalidation(tmp_path):
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"title_keywords": ["angular"]})
    first = load_profile(path)
    assert first["title_keywords"] == ["angular"]

    # Rewrite with a different value; bump mtime so the cache key changes.
    time.sleep(0.02)
    _write_filters(path, {"title_keywords": ["react", "frontend"]})
    second = load_profile(path)
    assert second["title_keywords"] == ["react", "frontend"]
    # first result was a deepcopy — mutating it must not poison the cache
    first["title_keywords"].append("poison")
    third = load_profile(path)
    assert "poison" not in third["title_keywords"]


def test_wrong_type_keeps_default(tmp_path, caplog):
    path = tmp_path / "filters.yaml"
    _write_filters(path, {"exclude_ai_training": "yes", "title_keywords": "angular"})
    with caplog.at_level(logging.WARNING, logger="hunter.filter_profile"):
        profile = load_profile(path)
    assert profile["exclude_ai_training"] is True
    assert profile["title_keywords"] == builtin_defaults()["title_keywords"]
    assert sum(1 for r in caplog.records if "wrong type" in r.message) >= 2
