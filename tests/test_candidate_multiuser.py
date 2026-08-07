"""Multi-user Phase B3 item 4 — per-path candidate cache (hunter/candidate.py)."""

from __future__ import annotations

import pytest

from hunter import candidate


@pytest.fixture(autouse=True)
def _fresh_cache():
    candidate._set_path(None)
    yield
    candidate._set_path(None)


def _yaml(tmp_path, name: str, full_name: str):
    p = tmp_path / name
    p.write_text(f"identity:\n  full_name: {full_name}\n", encoding="utf-8")
    return p


def test_two_users_in_one_process(tmp_path):
    a = _yaml(tmp_path, "a.yaml", "Alice A")
    b = _yaml(tmp_path, "b.yaml", "Bob B")
    assert candidate.get("identity.full_name", path=a) == "Alice A"
    assert candidate.get("identity.full_name", path=b) == "Bob B"
    # and again from cache, still distinct
    assert candidate.load(a)["identity"]["full_name"] == "Alice A"
    assert candidate.load(b)["identity"]["full_name"] == "Bob B"


def test_explicit_path_does_not_poison_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CANDIDATE_YAML_PATH", raising=False)
    other = _yaml(tmp_path, "other.yaml", "Other O")
    assert candidate.get("identity.full_name", path=other) == "Other O"
    default = candidate.get("identity.full_name", "fallback")
    assert default != "Other O"


def test_set_path_override_still_works(tmp_path):
    p = _yaml(tmp_path, "c.yaml", "Cara C")
    candidate._set_path(p)
    assert candidate.get("identity.full_name") == "Cara C"


def test_env_override_resolution(tmp_path, monkeypatch):
    p = _yaml(tmp_path, "env.yaml", "Envy E")
    monkeypatch.setenv("CANDIDATE_YAML_PATH", str(p))
    candidate._load_file.cache_clear()
    assert candidate.get("identity.full_name") == "Envy E"


def test_missing_file_returns_empty(tmp_path):
    assert candidate.load(tmp_path / "nope.yaml") == {}
    assert candidate.get("identity.full_name", "fallback", path=tmp_path / "nope.yaml") == (
        "fallback"
    )


def test_same_path_read_once(tmp_path, monkeypatch):
    p = _yaml(tmp_path, "once.yaml", "Once O")
    candidate.load(p)
    # A later file change is invisible until the cache is cleared — same
    # per-process caching contract as the old maxsize=1 loader.
    p.write_text("identity:\n  full_name: Changed\n", encoding="utf-8")
    assert candidate.load(p)["identity"]["full_name"] == "Once O"
    candidate._load_file.cache_clear()
    assert candidate.load(p)["identity"]["full_name"] == "Changed"
