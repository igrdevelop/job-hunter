"""tests/test_tracks.py — CANDIDATE_TRACKS / active_tracks() / React-gate tests.

docs/quality/09-multi-track-react.md

Readiness criterion: at the default track set (angular-only), every existing
filter/pipeline assertion in the suite is unchanged (verified by running the
full suite unmodified — see the commit message). This file adds NEW coverage
for the track-gating behavior itself: default is angular-only (today's
behavior, bit-for-bit); enabling react turns the three React-only exclusion
points into no-ops without deleting them.
"""

from __future__ import annotations

import pytest

import hunter.config as config_module
from hunter.models import Job


@pytest.fixture()
def tracks_db(tmp_path, monkeypatch):
    """Isolated tmp DB for active_tracks()/set_active_tracks() — these read
    hunter.config.TRACKER_DB_PATH directly (not hunter.tracker.DB_PATH, so
    the shared `tracker_db` fixture doesn't cover this)."""
    db = tmp_path / "tracks.db"
    monkeypatch.setattr(config_module, "TRACKER_DB_PATH", db)
    return db


def _react_only_job(**overrides) -> Job:
    # Title must contain a title_keywords hit ("frontend") to clear
    # classify_job's earlier title_kw gate before reaching the react check.
    defaults = {
        "title": "Senior Frontend Developer (React)",
        "company": "Acme",
        "location": "Remote",
        "salary": None,
        "url": "https://example.com/jobs/react-only",
        "source": "justjoin",
        "raw": {},
    }
    defaults.update(overrides)
    return Job(**defaults)


# ── config: CANDIDATE_TRACKS env parsing (pure function, no reload needed) ──


def test_default_tracks_is_angular_only():
    assert config_module._parse_tracks("angular") == frozenset({"angular"})


def test_candidate_tracks_parses_comma_list():
    assert config_module._parse_tracks("angular, react") == frozenset({"angular", "react"})


def test_blank_candidate_tracks_falls_back_to_angular():
    assert config_module._parse_tracks("") == frozenset({"angular"})


# ── config: active_tracks() / set_active_tracks() (DB wins over env) ───────


def test_active_tracks_defaults_to_env(tracks_db, monkeypatch):
    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    assert config_module.active_tracks() == frozenset({"angular"})


def test_set_active_tracks_overrides_env(tracks_db, monkeypatch):
    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    config_module.set_active_tracks({"angular", "react"})
    assert config_module.active_tracks() == frozenset({"angular", "react"})


def test_set_active_tracks_persists_across_calls(tracks_db, monkeypatch):
    """DB-backed, not in-memory — must survive as a fresh read each call
    (mirrors the apply subprocess boundary hunter.best_effort/source_health
    already rely on this same guarantee for)."""
    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    config_module.set_active_tracks({"react"})
    assert config_module.active_tracks() == frozenset({"react"})
    # A second, independent read (simulating a fresh call) sees the same value.
    assert config_module.active_tracks() == frozenset({"react"})


def test_set_active_tracks_can_be_switched_back(tracks_db, monkeypatch):
    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    config_module.set_active_tracks({"angular", "react"})
    assert "react" in config_module.active_tracks()
    config_module.set_active_tracks({"angular"})
    assert config_module.active_tracks() == frozenset({"angular"})


def test_active_tracks_falls_back_to_env_on_bare_db(tmp_path, monkeypatch):
    """No init_db() ever ran against this DB — active_tracks() must not crash,
    just fall back to the env default (best-effort, matches source_health /
    best_effort's own lazy-table-create pattern)."""
    monkeypatch.setattr(config_module, "TRACKER_DB_PATH", tmp_path / "bare.db")
    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    assert config_module.active_tracks() == frozenset({"angular"})


# ── filters: _react_track_active() gate ─────────────────────────────────────


def test_react_track_active_false_by_default(tracks_db, monkeypatch):
    from hunter.filters import _react_track_active

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    assert _react_track_active() is False


def test_react_track_active_true_when_react_enabled(tracks_db, monkeypatch):
    from hunter.filters import _react_track_active

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular", "react"}))
    assert _react_track_active() is True


# ── filters: React-only exclusion becomes a no-op when react track active ──


@pytest.mark.parametrize(
    "title",
    [
        "Senior React Developer",
        "React Engineer",
        "Frontend Developer (React)",
    ],
)
def test_react_only_title_filtered_at_angular_only(tracks_db, monkeypatch, title):
    from hunter.filters import _is_react_only_title

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    assert _is_react_only_title(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Senior React Developer",
        "React Engineer",
        "Frontend Developer (React)",
    ],
)
def test_react_only_title_passes_when_react_track_active(tracks_db, monkeypatch, title):
    from hunter.filters import _is_react_only_title

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular", "react"}))
    assert _is_react_only_title(title) is False


def test_react_without_angular_filtered_at_angular_only(tracks_db, monkeypatch):
    from hunter.filters import _is_react_without_angular

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    job = _react_only_job(
        raw={"stack": [{"name": "React", "slug": "react"}]},
    )
    assert _is_react_without_angular(job) is True


def test_react_without_angular_passes_when_react_track_active(tracks_db, monkeypatch):
    from hunter.filters import _is_react_without_angular

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular", "react"}))
    job = _react_only_job(
        raw={"stack": [{"name": "React", "slug": "react"}]},
    )
    assert _is_react_without_angular(job) is False


# ── classify_job end-to-end: filtered at angular, passes at angular+react ──


def test_classify_job_react_only_filtered_at_angular_only(tracks_db, monkeypatch):
    from hunter.filters import classify_job

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    job = _react_only_job()
    assert classify_job(job) == "react_no_angular"


def test_classify_job_react_only_passes_at_angular_plus_react(tracks_db, monkeypatch):
    from hunter.filters import classify_job

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular", "react"}))
    job = _react_only_job()
    # Not filtered for being React-only anymore; may still be None (passes)
    # since this fixture job has no other disqualifying signal.
    assert classify_job(job) is None


# ── apply pipeline: Step 1.5c / Step 4.5 gates are track-aware ─────────────
# Full-pipeline execution needs a dozen mocked stages (see
# tests/test_golden_apply_e2e.py for that level of test); the lighter-weight,
# already-established pattern for apply_api.py/_cli.py internals in this repo
# is source inspection (test_apply_api.py's `_source_of` helper).


def _source_of(module_name: str) -> str:
    import importlib
    import inspect

    return inspect.getsource(importlib.import_module(module_name))


def test_api_pipeline_step_1_5c_is_track_aware():
    src = _source_of("hunter.apply_api")
    block = src.split("Step 1.5c")[1].split("Step 1.5d")[0]
    assert "_react_track_active" in block


def test_api_pipeline_step_4_5_is_track_aware():
    src = _source_of("hunter.apply_api")
    block = src.split("Step 4.5")[1].split("Step 4.6")[0]
    assert "_react_track_active" in block


def test_cli_pipeline_react_skip_is_track_aware():
    src = _source_of("hunter.apply_cli")
    assert "_react_track_active" in src


# ── _detect_stack_hint: already-correct routing, verified by track feature ──
# docs/quality/09 explicitly asks this to be verified, not changed.


@pytest.mark.parametrize(
    "job_text,expected",
    [
        ("We need a Senior React Developer with TypeScript and Redux.", "react"),
        ("Senior Angular Developer with RxJS and NgRx experience.", "angular"),
        ("React + Next.js developer needed for our SSR platform.", "fullstack_react_next"),
        ("NestJS backend paired with a React frontend for this role.", "fullstack_react_next"),
        ("NestJS backend with an Angular frontend.", "fullstack_angular_nest"),
    ],
)
def test_detect_stack_hint_routes_react_correctly(job_text, expected):
    from hunter.apply_api import _detect_stack_hint

    assert _detect_stack_hint(job_text) == expected


# ── /tracks command ──────────────────────────────────────────────────────────


def test_cmd_tracks_status_text_shows_default(tracks_db, monkeypatch):
    from hunter.commands.tracks import _status_text

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    text = _status_text()
    assert "angular" in text
    assert "react" not in text.split("\n\n")[0]  # not in the headline line


def test_cmd_tracks_switch_both(tracks_db, monkeypatch):
    from hunter.commands.tracks import _switch

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    text = _switch("both")
    assert "angular" in text and "react" in text
    assert config_module.active_tracks() == frozenset({"angular", "react"})


def test_cmd_tracks_switch_react_only(tracks_db, monkeypatch):
    from hunter.commands.tracks import _switch

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    _switch("react")
    assert config_module.active_tracks() == frozenset({"react"})


def test_cmd_tracks_switch_invalid_preset_shows_usage(tracks_db, monkeypatch):
    from hunter.commands.tracks import _switch

    monkeypatch.setattr(config_module, "TRACKS", frozenset({"angular"}))
    text = _switch("nonsense")
    assert "Usage" in text
