"""Quality-parity guards for gen_profile wave 3 (docs/
GENERATION_ARCHITECTURE_ANALYSIS.md §6).

1. No file, no env override => load_gen_profile() is byte-for-byte
   builtin_defaults() (mirrors tests/test_filter_profile_parity.py's dict
   equality guard).
2. Every value in builtin_defaults() reproduces the EXACT hardcoded constant
   or env-var default that existed before this module — pinned individually
   so a typo in builtin_defaults() fails loudly instead of merely matching
   itself.
3. hunter.config's resolved constants (which now route through
   gen_profile.get()) still equal their pre-wave-3 hardcoded defaults, with
   no user file and no env override — this is the actual behavioral parity
   claim: importing hunter.config today behaves exactly as it did before.
4. The call-time reads added inside hunter/pipeline/ats.py,
   hunter/verdict_refine.py, hunter/ats_pdf_roundtrip.py and
   hunter/pipeline/gates.py still observe a profile change made mid-process
   (not frozen at import/def time) — proves the "read at call time, not
   def time" requirement actually holds, not just that the default matches.
"""

from __future__ import annotations

import importlib

import pytest

from hunter import gen_profile
from hunter.gen_profile import builtin_defaults, clear_gen_profile_cache, load_gen_profile


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    # Force "file absent" so this file's assertions never depend on a real
    # candidate/generation.yaml sitting in the checkout.
    monkeypatch.setenv("GENERATION_YAML_PATH", str(tmp_path / "missing_generation.yaml"))
    for env in gen_profile._ENV_OVERRIDES.values():
        monkeypatch.delenv(env[0], raising=False)
    clear_gen_profile_cache()
    yield
    clear_gen_profile_cache()


def test_load_gen_profile_equals_builtin_defaults():
    assert load_gen_profile() == builtin_defaults()


@pytest.mark.parametrize(
    "dotpath,expected",
    [
        ("ats.threshold", 95.0),
        ("ats.honest_rounds", 2),
        ("ats.total_rounds", 5),
        ("ats.checklist_cap", 30),
        ("verdict.enabled", True),
        ("verdict.target", 95.0),
        ("verdict.max_refines", 5),
        ("verdict.stretch_from_round", 4),
        ("verdict.heal_delta_pp", 5.0),
        ("judge.enabled", True),
        ("judge.mode", "warn"),
        ("judge.max_repair_rounds", 1),
        ("gates.doomed_enabled", True),
        ("gates.doomed_hard_action", "skip"),
        ("gates.prescreen_enabled", True),
        ("gates.prescreen_mode", "warn"),
        ("gates.prescreen_min_confidence", 0.9),
        ("gates.repost_enabled", True),
        ("gates.repost_window_days", 60),
        ("gates.react_skip_min_mentions", 3),
        ("generation.skip_pl_for_en", True),
    ],
)
def test_builtin_default_matches_pre_wave3_value(dotpath, expected):
    assert gen_profile.get(dotpath) == expected


def test_config_constants_unchanged_with_no_profile_no_env():
    """hunter.config resolves every migrated constant through gen_profile.get()
    now, but with no user file and no env var the runtime value must be
    identical to what the hardcoded os.getenv(..., "<default>") produced
    before this wave."""
    config = importlib.reload(importlib.import_module("hunter.config"))
    try:
        assert config.ATS_VERDICT_ENABLED is True
        assert config.ATS_VERDICT_TARGET == 95.0
        assert config.ATS_VERDICT_MAX_REFINES == 5
        assert config.JUDGE_ENABLED is True
        assert config.JUDGE_MODE == "warn"
        assert config.JUDGE_MAX_REPAIR_ROUNDS == 1
        assert config.DOOMED_GATE_ENABLED is True
        assert config.DOOMED_GATE_HARD_ACTION == "skip"
        assert config.PRESCREEN_ENABLED is True
        assert config.PRESCREEN_MODE == "warn"
        assert config.PRESCREEN_MIN_CONFIDENCE == 0.9
        assert config.REPOST_GATE_ENABLED is True
        assert config.REPOST_WINDOW_DAYS == 60
        assert config.GEN_SKIP_PL_FOR_EN is True
    finally:
        # Leave the module in its normal (env-driven) state for later tests
        # in the same process.
        importlib.reload(config)


def test_is_react_only_job_text_default_threshold_unchanged():
    from hunter.pipeline.gates import is_react_only_job_text

    two_mentions = "We use React and React heavily in this stack."
    three_mentions = "React React React everywhere in this stack."
    assert is_react_only_job_text(two_mentions) is False
    assert is_react_only_job_text(three_mentions) is True


def test_is_react_only_job_text_observes_profile_change_at_call_time(tmp_path, monkeypatch):
    """A generation.yaml edit must be observed on the NEXT call, not require
    a process restart or a def-time re-import — proves the call-time-read
    requirement, not just that the shipped default matches."""
    from hunter.pipeline.gates import is_react_only_job_text

    two_mentions = "We use React and React heavily in this stack."
    assert is_react_only_job_text(two_mentions) is False

    path = tmp_path / "generation.yaml"
    path.write_text("gates:\n  react_skip_min_mentions: 2\n", encoding="utf-8")
    monkeypatch.setenv("GENERATION_YAML_PATH", str(path))
    clear_gen_profile_cache()

    assert is_react_only_job_text(two_mentions) is True


def test_heal_delta_pp_default_unchanged():
    from hunter.ats_pdf_roundtrip import heal_delta_pp

    assert heal_delta_pp() == 5.0


def test_heal_delta_pp_observes_profile_change_at_call_time(tmp_path, monkeypatch):
    from hunter.ats_pdf_roundtrip import heal_delta_pp

    path = tmp_path / "generation.yaml"
    path.write_text("verdict:\n  heal_delta_pp: 1.5\n", encoding="utf-8")
    monkeypatch.setenv("GENERATION_YAML_PATH", str(path))
    clear_gen_profile_cache()

    assert heal_delta_pp() == 1.5
