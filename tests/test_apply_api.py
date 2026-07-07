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


# ── Verdict-only interfaces (VERDICT_REFINE_PLAN M4) ─────────────────────────
# The owner asked for a single ATS number in Telegram: the independent verdict,
# not "verdict | self: self-score". The generator's self-score stays in
# content.json only.

def test_api_pipeline_telegram_has_no_self_score_suffix() -> None:
    src = _source_of("hunter.apply_api")
    assert "(independent, PDF)" in src
    assert "self:" not in src


def test_api_pipeline_wires_refine_loop_before_verdict_stamp() -> None:
    """Step 7.7b: the refine loop must run (when applicable) BEFORE the
    verdict is stamped on the tracker / persisted, so the tracker/Telegram
    always see the FINAL (possibly refined) verdict."""
    src = _source_of("hunter.apply_api")
    assert "from hunter.verdict_refine import refine_loop" in src
    verdict_block = src.split("run_llm_verdict(folder=output_folder")[1]
    refine_pos = verdict_block.index("refine_loop(")
    stamp_pos = verdict_block.index("set_ats_verdict")
    assert refine_pos < stamp_pos


def test_cli_pipeline_wires_refine_loop_before_verdict_stamp() -> None:
    src = _source_of("hunter.apply_cli")
    assert "from hunter.verdict_refine import refine_loop" in src
    verdict_block = src.split("run_llm_verdict(folder=folder_path")[1]
    refine_pos = verdict_block.index("refine_loop(")
    stamp_pos = verdict_block.index("set_ats_verdict")
    assert refine_pos < stamp_pos


# ── Review Fix 1: to_learn stamp (VERDICT_REFINE_PLAN review) ────────────────
# The tracker row already exists (Step 7) when the refine loop's round-2
# stretch additions land in content["to_learn"] — the row must be patched
# post-hoc, same shape as the verdict stamp.

def test_api_pipeline_stamps_to_learn_after_refine_loop() -> None:
    src = _source_of("hunter.apply_api")
    assert "from hunter.tracker import set_to_learn" in src
    verdict_block = src.split("run_llm_verdict(folder=output_folder")[1]
    refine_pos = verdict_block.index("refine_loop(")
    to_learn_pos = verdict_block.index("set_to_learn(")
    assert refine_pos < to_learn_pos
    # Gated on an actual change vs. the pre-loop value.
    assert "_to_learn_before_refine" in verdict_block
    assert 'content.get("to_learn") != _to_learn_before_refine' in verdict_block


def test_cli_pipeline_stamps_to_learn_after_refine_loop() -> None:
    src = _source_of("hunter.apply_cli")
    assert "from hunter.tracker import set_to_learn" in src
    verdict_block = src.split("run_llm_verdict(folder=folder_path")[1]
    refine_pos = verdict_block.index("refine_loop(")
    to_learn_pos = verdict_block.index("set_to_learn(")
    assert refine_pos < to_learn_pos
    assert "_to_learn_before_refine" in verdict_block


# ── Review Fix 2: refine-loop regen must be tracker-row-safe ────────────────
# The tracker row already exists when a refine round re-renders — the regen
# command must pass --no-tracker (skip the row write) and never --force (a
# force-mode apply would otherwise DELETE+INSERT the row on every round).

def test_api_pipeline_refine_regen_uses_no_tracker_not_force() -> None:
    src = _source_of("hunter.apply_api")
    verdict_block = src.split("run_llm_verdict(folder=output_folder")[1]
    regen_block = verdict_block.split("def _regen_for_refine")[1].split("def ")[0]
    assert "no_tracker=True" in regen_block
    assert "force=False" in regen_block
    # Must build its OWN command, not reuse the Step 7 `gen_cmd` (built with
    # force=skip_dedup).
    assert "gen_cmd," not in regen_block
    assert "build_generate_docs_cmd(" in regen_block


def test_cli_pipeline_refine_regen_uses_no_tracker_not_force() -> None:
    src = _source_of("hunter.apply_cli")
    verdict_block = src.split("run_llm_verdict(folder=folder_path")[1]
    regen_block = verdict_block.split("def _regen_for_refine")[1].split("def ")[0]
    assert "no_tracker=True" in regen_block
    assert "force=False" in regen_block
    assert "force=skip_dedup" not in regen_block
    assert "build_generate_docs_cmd(" in regen_block


# ── Default is 3 (honest ×2 + stretch), owner decision 2026-07-07 ───────────

def test_ats_verdict_max_refines_default_is_three() -> None:
    """Owner decision 2026-07-07: max 3 rounds — two honest visibility passes,
    stretch (openly add posting skills) only on the third."""
    src = _source_of("hunter.config")
    assert 'os.getenv("ATS_VERDICT_MAX_REFINES", "3")' in src


# ── Cost re-stamp: tracker row must get the post-refine total ────────────────
# The row is created in Step 7 with the Step 6.5 (pre-verdict, pre-refine)
# figure; the refine loop can more than double the real spend. The pipeline
# must re-price the usage log AFTER refine_loop and re-stamp the row via
# tracker.set_cost so the DB / Sheet column M show the true per-vacancy total.

def test_api_pipeline_restamps_cost_after_refine_loop() -> None:
    src = _source_of("hunter.apply_api")
    assert "from hunter.tracker import set_cost" in src
    verdict_block = src.split("run_llm_verdict(folder=output_folder")[1]
    refine_pos = verdict_block.index("refine_loop(")
    reprice_pos = verdict_block.index("_price_usage2(_usage_log)")
    stamp_pos = verdict_block.index("set_cost(")
    assert refine_pos < reprice_pos < stamp_pos
    # Paste flow has no URL to match a row by — the stamp must be gated.
    assert "url != PASTE_NO_URL_PLACEHOLDER" in verdict_block


# ── Verdict gap_report line in the Telegram success message ─────────────────
# The owner asked to see WHY the verdict isn't higher, not just the number —
# the judge's gap_report rides the success notification as its own line.

def test_api_pipeline_sends_verdict_gap_report_line() -> None:
    src = _source_of("hunter.apply_api")
    assert "format_gap_report" in src
    assert "{gap_line}" in src
