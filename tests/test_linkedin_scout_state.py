"""Tests for linkedin_scout.state — circuit breaker + keyword rotation.

Pure JSON-file logic, no Playwright/browser involved.
"""

import pytest

from linkedin_scout.state import ScoutState


def test_fresh_state_not_tripped(tmp_path):
    state = ScoutState(tmp_path / "state.json")
    assert state.is_tripped() is False


def test_trip_sets_tripped_and_reason(tmp_path):
    state = ScoutState(tmp_path / "state.json")
    first = state.trip("redirected to login")
    assert first is True
    assert state.is_tripped() is True
    assert state.trip_reason() == "redirected to login"


def test_trip_is_idempotent_second_call_returns_false():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        state = ScoutState(Path(d) / "state.json")
        assert state.trip("first reason") is True
        assert state.trip("second reason") is False
        # reason from the FIRST trip is preserved — never overwritten silently
        assert state.trip_reason() == "first reason"


def test_reset_clears_trip(tmp_path):
    state = ScoutState(tmp_path / "state.json")
    state.trip("flagged")
    assert state.is_tripped() is True
    state.reset()
    assert state.is_tripped() is False
    assert state.trip_reason() == ""


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    state1 = ScoutState(path)
    state1.trip("flagged")

    state2 = ScoutState(path)
    assert state2.is_tripped() is True
    assert state2.trip_reason() == "flagged"


def test_next_keyword_round_robins(tmp_path):
    state = ScoutState(tmp_path / "state.json")
    keywords = ["a", "b", "c"]
    picked = [state.next_keyword(keywords) for _ in range(5)]
    assert picked == ["a", "b", "c", "a", "b"]


def test_next_keyword_index_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    keywords = ["a", "b", "c"]
    state1 = ScoutState(path)
    assert state1.next_keyword(keywords) == "a"

    state2 = ScoutState(path)
    assert state2.next_keyword(keywords) == "b"


def test_next_keyword_empty_list_raises(tmp_path):
    state = ScoutState(tmp_path / "state.json")
    with pytest.raises(ValueError):
        state.next_keyword([])


def test_next_keyword_list_shrunk_since_last_run_wraps_safely(tmp_path):
    state = ScoutState(tmp_path / "state.json")
    state.next_keyword(["a", "b", "c", "d", "e"])  # index -> 1
    for _ in range(4):
        state.next_keyword(["a", "b", "c", "d", "e"])  # index -> 5 % 5 = 0
    # A shorter keyword list on a later run must not raise/index out of range.
    result = state.next_keyword(["x", "y"])
    assert result in ("x", "y")


def test_corrupt_state_file_treated_as_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not valid json", encoding="utf-8")
    state = ScoutState(path)
    assert state.is_tripped() is False


def test_save_is_atomic_no_leftover_tmp(tmp_path):
    path = tmp_path / "state.json"
    state = ScoutState(path)
    state.trip("x")
    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()
