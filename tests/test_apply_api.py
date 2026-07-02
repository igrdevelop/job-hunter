"""Tests for hunter/apply_api.py — API pipeline entry point."""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ── Import sanity ─────────────────────────────────────────────────────────────

def test_main_api_is_importable() -> None:
    from hunter.apply_api import main_api
    assert callable(main_api)


def test_main_api_accepts_keyword_args() -> None:
    """Signature must accept skip_dedup and full_mode as keyword-only args."""
    import inspect
    from hunter.apply_api import main_api
    sig = inspect.signature(main_api)
    params = sig.parameters
    assert "url" in params
    assert "paste_text" in params
    assert "skip_dedup" in params
    assert "full_mode" in params
    assert "jobleads_company" in params
    assert "jobleads_title" in params
    # keyword-only params after *
    kw_only = [
        name for name, p in params.items()
        if p.kind == inspect.Parameter.KEYWORD_ONLY
    ]
    assert "skip_dedup" in kw_only
    assert "full_mode" in kw_only


def test_main_api_skip_dedup_defaults_false() -> None:
    import inspect
    from hunter.apply_api import main_api
    sig = inspect.signature(main_api)
    assert sig.parameters["skip_dedup"].default is False
    assert sig.parameters["full_mode"].default is False


# ── Dedup short-circuit ───────────────────────────────────────────────────────

def test_main_api_skips_when_already_processed(monkeypatch, capsys) -> None:
    """main_api should return early (no LLM call) when URL is in tracker."""
    monkeypatch.setattr(
        "hunter.apply_api._already_processed",
        lambda url, skip_dedup=False: True,
    )
    # If it tries to call LLM it would fail — but it should short-circuit
    from hunter.apply_api import main_api

    with patch("hunter.apply_api.notify") as mock_notify:
        main_api("https://example.com/job/1")

    # Should have notified about already-processed
    mock_notify.assert_called_once()
    assert "tracker" in mock_notify.call_args[0][0].lower() or "skipped" in mock_notify.call_args[0][0].lower()


def test_main_api_skip_dedup_true_bypasses_tracker(monkeypatch, capsys) -> None:
    """skip_dedup=True must not call _already_processed (returns False immediately)."""
    calls = []

    def fake_already_processed(url, skip_dedup=False):
        calls.append(skip_dedup)
        return False  # let it proceed, but we'll stop it at fetch

    monkeypatch.setattr("hunter.apply_api._already_processed", fake_already_processed)

    # Intercept the fetch step so we don't actually hit the network
    def _boom(url):
        raise RuntimeError("fetch stopped intentionally")

    with patch("hunter.sources.fetch_job_text", _boom):
        with pytest.raises(SystemExit):
            from hunter.apply_api import main_api
            main_api("https://example.com/job/2", skip_dedup=True)

    # _already_processed was called with skip_dedup=True
    assert calls == [True]


# ── Paste flow ────────────────────────────────────────────────────────────────

def test_main_api_uses_paste_text_without_fetch(monkeypatch) -> None:
    """When paste_text is provided, fetch should not be called."""
    fetch_called = []

    def _fetch(url):
        fetch_called.append(url)
        return "job text"

    monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)

    with patch("hunter.sources.fetch_job_text", _fetch):
        # is_job_text_too_short is imported lazily inside main_api — patch at source
        with patch("hunter.validation.is_job_text_too_short", return_value=True):
            with pytest.raises(SystemExit):
                from hunter.apply_api import main_api
                main_api(
                    "paste://no-url",
                    paste_text="This is pasted job text with enough content.",
                )

    assert fetch_called == [], "fetch_job_text should NOT be called when paste_text is provided"


# ── No globals: multiple calls independent ────────────────────────────────────

def test_main_api_no_shared_global_state(monkeypatch) -> None:
    """Calling main_api twice with different flags should be independent."""
    from hunter.apply_api import main_api

    results = []

    def fake_already_processed(url, skip_dedup=False):
        results.append(skip_dedup)
        return True  # short-circuit both calls

    monkeypatch.setattr("hunter.apply_api._already_processed", fake_already_processed)

    with patch("hunter.apply_api.notify"):
        main_api("https://example.com/job/a", skip_dedup=False)
        main_api("https://example.com/job/b", skip_dedup=True)

    assert results == [False, True], "Each call must use its own skip_dedup value"


# ── Verdict tracker stamp (Phase 2 M3) ───────────────────────────────────────
# Full-pipeline execution of Step 7.7 needs a dozen mocked stages; the repo's
# precedent for wiring guarantees is source inspection (see
# test_apply_dispatcher.test_apply_agent_is_thin). These guards assert the
# stamp exists, lives inside the verdict block, and skips the paste flow.

def _source_of(module_name: str) -> str:
    import importlib
    import inspect
    return inspect.getsource(importlib.import_module(module_name))


def test_api_pipeline_stamps_verdict_on_tracker() -> None:
    src = _source_of("hunter.apply_api")
    assert "set_ats_verdict" in src
    # The stamp must be guarded against the paste flow (no URL to match).
    verdict_block = src.split("run_llm_verdict(folder=output_folder")[1]
    stamp_pos = verdict_block.index("set_ats_verdict")
    guard_pos = verdict_block.index("PASTE_NO_URL_PLACEHOLDER")
    assert guard_pos < stamp_pos


def test_cli_pipeline_stamps_verdict_on_tracker() -> None:
    src = _source_of("hunter.apply_cli")
    assert "set_ats_verdict" in src
    verdict_block = src.split("run_llm_verdict(folder=folder_path")[1]
    stamp_pos = verdict_block.index("set_ats_verdict")
    guard_pos = verdict_block.index("paste://")
    assert guard_pos < stamp_pos
