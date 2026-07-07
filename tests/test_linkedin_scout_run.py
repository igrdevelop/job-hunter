"""Tests for linkedin_scout.run — CLI/entry-point glue (M4).

No real browser is launched: --track real-run paths are exercised only via
monkeypatched browser.run_once/run_feed_once.
"""

from __future__ import annotations

import linkedin_scout.run as run


# --- env parsing --------------------------------------------------------------


def test_keywords_from_env_defaults_when_unset(monkeypatch):
    monkeypatch.delenv(run._KEYWORDS_ENV, raising=False)
    assert run._keywords_from_env() == list(run.DEFAULT_KEYWORDS)


def test_keywords_from_env_parses_csv(monkeypatch):
    monkeypatch.setenv(run._KEYWORDS_ENV, "angular, angular developer ,  ")
    assert run._keywords_from_env() == ["angular", "angular developer"]


def test_float_env_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
    assert run._float_env("SOME_UNSET_VAR", 1.5) == 1.5


def test_float_env_parses_value(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "0.5")
    assert run._float_env("SOME_VAR", 1.5) == 0.5


def test_float_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "not-a-number")
    assert run._float_env("SOME_VAR", 1.5) == 1.5


def test_storage_state_path_unset(monkeypatch):
    monkeypatch.delenv(run._STORAGE_STATE_ENV, raising=False)
    assert run._storage_state_path() is None


def test_storage_state_path_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(run._STORAGE_STATE_ENV, str(tmp_path / "does_not_exist.json"))
    assert run._storage_state_path() is None


def test_storage_state_path_existing_file(monkeypatch, tmp_path):
    p = tmp_path / "storage_state.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(run._STORAGE_STATE_ENV, str(p))
    assert run._storage_state_path() == p


# --- _track_paths --------------------------------------------------------------


def test_track_paths_search():
    profile_dir, state_path = run._track_paths("search")
    assert profile_dir == run.SEARCH_PROFILE_DIR
    assert state_path == run.SEARCH_STATE_PATH


def test_track_paths_feed():
    profile_dir, state_path = run._track_paths("feed")
    assert profile_dir == run.FEED_PROFILE_DIR
    assert state_path == run.FEED_STATE_PATH


def test_track_paths_unknown_raises():
    import pytest

    with pytest.raises(ValueError):
        run._track_paths("bogus")


def test_search_and_feed_tracks_use_different_paths():
    assert run.SEARCH_PROFILE_DIR != run.FEED_PROFILE_DIR
    assert run.SEARCH_STATE_PATH != run.FEED_STATE_PATH


# --- _maybe_skip_and_jitter ---------------------------------------------------


def test_skip_and_jitter_skips_when_roll_below_chance(monkeypatch):
    monkeypatch.setattr(run.random, "random", lambda: 0.1)
    slept = []
    monkeypatch.setattr(run.time, "sleep", lambda s: slept.append(s))

    proceed = run._maybe_skip_and_jitter(skip_chance=0.3, jitter_max_min=45)

    assert proceed is False
    assert slept == []


def test_skip_and_jitter_proceeds_and_sleeps_when_roll_above_chance(monkeypatch):
    monkeypatch.setattr(run.random, "random", lambda: 0.9)
    monkeypatch.setattr(run.random, "uniform", lambda a, b: 5.0)
    slept = []
    monkeypatch.setattr(run.time, "sleep", lambda s: slept.append(s))

    proceed = run._maybe_skip_and_jitter(skip_chance=0.3, jitter_max_min=45)

    assert proceed is True
    assert slept == [5.0]


# --- _run_dry_run --------------------------------------------------------------


def test_dry_run_prints_expected_candidates(capsys):
    run._run_dry_run(run.DEFAULT_DRY_RUN_FIXTURE)
    out = capsys.readouterr().out
    assert "Deloitte Poland" in out
    assert "Piotr Nowak" in out
    # the US-staffing / candidate-side / spam posts in the fixture must NOT
    # survive the M1 filter
    assert "John Smith" not in out
    assert "Anna Kowalska" not in out
    assert "Growth Academy" not in out


def test_dry_run_reports_no_matches_cleanly(tmp_path, capsys):
    fixture = tmp_path / "empty.txt"
    fixture.write_text("Feed post\n\nSome Author\nFollow\nJust a status update, no hiring here.\n", encoding="utf-8")
    run._run_dry_run(fixture)
    out = capsys.readouterr().out
    assert "no matches" in out


# --- main() CLI dispatch -------------------------------------------------------


def test_main_dry_run_does_not_touch_browser(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(run.browser, "run_once", lambda *a, **k: called.append("run_once"))
    monkeypatch.setattr(run.browser, "run_feed_once", lambda *a, **k: called.append("run_feed_once"))

    exit_code = run.main(["--dry-run"])

    assert exit_code == 0
    assert called == []


def test_main_requires_track_for_real_run(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        run.main([])
    assert exc_info.value.code == 2


def test_main_reset_without_track_resets_both(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "SEARCH_STATE_PATH", tmp_path / "search_state.json")
    monkeypatch.setattr(run, "FEED_STATE_PATH", tmp_path / "feed_state.json")

    run.ScoutState(run.SEARCH_STATE_PATH).trip("flagged")
    run.ScoutState(run.FEED_STATE_PATH).trip("flagged")

    exit_code = run.main(["--reset"])

    assert exit_code == 0
    assert run.ScoutState(run.SEARCH_STATE_PATH).is_tripped() is False
    assert run.ScoutState(run.FEED_STATE_PATH).is_tripped() is False


def test_main_reset_with_track_resets_only_that_one(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "SEARCH_STATE_PATH", tmp_path / "search_state.json")
    monkeypatch.setattr(run, "FEED_STATE_PATH", tmp_path / "feed_state.json")

    run.ScoutState(run.SEARCH_STATE_PATH).trip("flagged")
    run.ScoutState(run.FEED_STATE_PATH).trip("flagged")

    exit_code = run.main(["--reset", "--track", "search"])

    assert exit_code == 0
    assert run.ScoutState(run.SEARCH_STATE_PATH).is_tripped() is False
    assert run.ScoutState(run.FEED_STATE_PATH).is_tripped() is True


def test_main_real_run_calls_run_once_for_search_track(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "SEARCH_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(run, "SEARCH_STATE_PATH", tmp_path / "search_state.json")
    monkeypatch.setattr(run, "SEEN_STORE_PATH", tmp_path / "seen.json")

    calls = []
    monkeypatch.setattr(run.browser, "run_once", lambda *a, **k: calls.append(("run_once", a, k)) or [])

    exit_code = run.main(["--track", "search", "--no-jitter"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == "run_once"


def test_main_real_run_calls_run_feed_once_for_feed_track(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "FEED_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(run, "FEED_STATE_PATH", tmp_path / "feed_state.json")
    monkeypatch.setattr(run, "SEEN_STORE_PATH", tmp_path / "seen.json")

    calls = []
    monkeypatch.setattr(
        run.browser, "run_feed_once", lambda *a, **k: calls.append(("run_feed_once", a, k)) or []
    )

    exit_code = run.main(["--track", "feed", "--no-jitter"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == "run_feed_once"


def test_main_no_jitter_skips_skip_and_jitter(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "SEARCH_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(run, "SEARCH_STATE_PATH", tmp_path / "search_state.json")
    monkeypatch.setattr(run, "SEEN_STORE_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(run.browser, "run_once", lambda *a, **k: [])

    called = []
    monkeypatch.setattr(run, "_maybe_skip_and_jitter", lambda **k: called.append(1) or True)

    run.main(["--track", "search", "--no-jitter"])

    assert called == []


def test_main_sends_notifications_for_real_run_candidates(tmp_path, monkeypatch):
    from linkedin_scout.browser import ScoutCandidate

    monkeypatch.setattr(run, "SEARCH_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(run, "SEARCH_STATE_PATH", tmp_path / "search_state.json")
    monkeypatch.setattr(run, "SEEN_STORE_PATH", tmp_path / "seen.json")

    candidate = ScoutCandidate(
        keyword="angular hiring", author="Jane", body="We're hiring Angular devs", scouted_at="now"
    )
    monkeypatch.setattr(run.browser, "run_once", lambda *a, **k: [candidate])

    sent_texts = []
    monkeypatch.setattr(run.notify, "_send_telegram", lambda text: sent_texts.append(text) or True)

    run.main(["--track", "search", "--no-jitter"])

    assert len(sent_texts) == 1
    assert "Jane" in sent_texts[0]
