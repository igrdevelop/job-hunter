"""
Tests for the doomed-vacancy gate wiring (docs/DOOMED_GATE_PLAN.md, milestone M2;
docs/DOOMED_GATE_PASTE_PLAN.md for the force-vs-paste split):

1. `hunter.apply_shared.run_doomed_gate` — the shared gate-wiring helper used
   by both pipelines (hard→skip+SKIP-row; hard+force-override→warn+continue;
   soft→warn+continue; gate disabled→noop; assess_job_text failure→best-effort
   continue). A manual paste is NOT an override anymore — only `/force`
   (skip_dedup) degrades a HARD finding to a warning.
2. `hunter.apply_api.main_api` — Step 1.5f sits after the manual screen
   (Step 1.5e) and before the first LLM call; a HARD finding aborts before
   Step 2, a False return lets the pipeline continue past it.
3. `hunter.apply_cli.main_cli` — symmetric wiring, mirrored inside the
   `if job_text:` block, before the `claude -p` subprocess is spawned.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hunter.filters import GateFinding


# ── run_doomed_gate (shared helper) ──────────────────────────────────────────


def _hard_finding(
    rule: str = "foreign_onsite_hybrid", evidence: str = "hybrid in McLean, Virginia"
) -> GateFinding:
    return GateFinding(rule=rule, severity="hard", evidence=evidence)


def _soft_finding(
    rule: str = "stack_mismatch_non_candidate_framework", evidence: str = "Vue 3 / Nuxt"
) -> GateFinding:
    return GateFinding(rule=rule, severity="soft", evidence=evidence)


class TestRunDoomedGateDisabled:
    def test_disabled_returns_false_without_calling_assess(self) -> None:
        from hunter.apply_shared import run_doomed_gate

        with patch("hunter.config.DOOMED_GATE_ENABLED", False):
            with patch("hunter.filters.assess_job_text") as mock_assess:
                result = run_doomed_gate("some job text", "https://example.com/1")
        assert result is False
        mock_assess.assert_not_called()


class TestRunDoomedGateHardSkip:
    def test_hard_finding_returns_true_and_writes_skip_row(self) -> None:
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.config.DOOMED_GATE_HARD_ACTION", "skip"),
            patch("hunter.filters.assess_job_text", return_value=[_hard_finding()]),
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
        ):
            result = run_doomed_gate(
                "job text",
                "https://example.com/2",
                title="Senior Angular Dev",
                company="Acme",
            )
        assert result is True
        mock_add_skipped.assert_called_once()
        job_arg = mock_add_skipped.call_args[0][0]
        assert job_arg.url == "https://example.com/2"
        assert job_arg.company == "Acme"
        assert job_arg.title == "Senior Angular Dev"
        mock_notify.assert_called_once()
        assert "Skipped before generation" in mock_notify.call_args[0][0]
        assert "foreign_onsite_hybrid" in mock_notify.call_args[0][0]

    def test_tracker_write_failure_does_not_change_the_abort_decision(self) -> None:
        """A tracker/DB error while writing the SKIP row must not un-abort —
        the gate already decided to skip; the write is best-effort."""
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.config.DOOMED_GATE_HARD_ACTION", "skip"),
            patch("hunter.filters.assess_job_text", return_value=[_hard_finding()]),
            patch("hunter.apply_shared.notify"),
            patch("hunter.tracker.add_skipped", side_effect=RuntimeError("db locked")),
        ):
            result = run_doomed_gate("job text", "https://example.com/3")
        assert result is True


class TestRunDoomedGateForceOverride:
    def test_hard_finding_with_force_override_degrades_to_warn(self) -> None:
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.config.DOOMED_GATE_HARD_ACTION", "skip"),
            patch("hunter.filters.assess_job_text", return_value=[_hard_finding()]),
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
        ):
            result = run_doomed_gate(
                "job text",
                "https://example.com/4",
                is_force_override=True,
            )
        assert result is False
        mock_add_skipped.assert_not_called()
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][0]
        assert "Heads-up" in msg
        assert "force override" in msg
        assert "foreign_onsite_hybrid" in msg

    def test_hard_action_warn_config_degrades_without_force_override(self) -> None:
        """DOOMED_GATE_HARD_ACTION=warn is the emergency lever — degrades
        every HARD finding to a warning even without /force."""
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.config.DOOMED_GATE_HARD_ACTION", "warn"),
            patch("hunter.filters.assess_job_text", return_value=[_hard_finding()]),
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
        ):
            result = run_doomed_gate("job text", "https://example.com/5")
        assert result is False
        mock_add_skipped.assert_not_called()
        mock_notify.assert_called_once()


class TestRunDoomedGateSoft:
    def test_soft_finding_warns_and_continues(self) -> None:
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.config.DOOMED_GATE_HARD_ACTION", "skip"),
            patch("hunter.filters.assess_job_text", return_value=[_soft_finding()]),
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
        ):
            result = run_doomed_gate("job text", "https://example.com/6")
        assert result is False
        mock_add_skipped.assert_not_called()
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][0]
        assert "Heads-up" in msg
        assert "force override" not in msg
        assert "stack_mismatch_non_candidate_framework" in msg


class TestRunDoomedGateNoFindings:
    def test_no_findings_no_notify_no_skip(self) -> None:
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.filters.assess_job_text", return_value=[]),
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
        ):
            result = run_doomed_gate("clean job text", "https://example.com/7")
        assert result is False
        mock_notify.assert_not_called()
        mock_add_skipped.assert_not_called()


class TestRunDoomedGateBestEffort:
    def test_assess_job_text_exception_is_swallowed(self) -> None:
        from hunter.apply_shared import run_doomed_gate

        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.filters.assess_job_text", side_effect=RuntimeError("boom")),
            patch("hunter.apply_shared.notify") as mock_notify,
        ):
            result = run_doomed_gate("job text", "https://example.com/8")
        assert result is False
        mock_notify.assert_not_called()


# ── apply_api.py wiring ───────────────────────────────────────────────────────


def _patch_api_pre_gate(monkeypatch, job_text: str = "Full job posting text " * 20) -> None:
    """Neutralize every pipeline stage BEFORE Step 1.5f so the gate call is
    reached deterministically without network/filesystem side effects."""
    monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: job_text)
    monkeypatch.setattr("hunter.validation.is_job_text_too_short", lambda text, *a, **kw: False)
    monkeypatch.setattr("hunter.expired_check.is_job_expired", lambda text: False)
    monkeypatch.setattr("hunter.apply_api.is_react_only_job_text", lambda text: False)
    monkeypatch.setattr("hunter.apply_api.is_backend_only_job_text", lambda text: False)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: None)


def test_api_pipeline_aborts_before_step2_when_gate_returns_true(monkeypatch) -> None:
    _patch_api_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", lambda *a, **kw: True)
    # If the pipeline continued past the gate it would look for real prompt
    # files under a bogus PROMPTS_DIR and sys.exit(1) — never patched, so a
    # SystemExit here would mean the gate did NOT stop the pipeline.
    monkeypatch.setattr(
        "hunter.apply_api.PROMPTS_DIR", __import__("pathlib").Path("/nonexistent/prompts")
    )

    from hunter.apply_api import main_api

    with patch("hunter.apply_api.notify"):
        result = main_api("https://example.com/api-hard")

    assert result is None


def test_api_pipeline_continues_past_gate_when_it_returns_false(monkeypatch) -> None:
    _patch_api_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", lambda *a, **kw: False)
    # A bogus PROMPTS_DIR makes Step 2 sys.exit(1) — proves we reached Step 2.
    monkeypatch.setattr(
        "hunter.apply_api.PROMPTS_DIR", __import__("pathlib").Path("/nonexistent/prompts")
    )

    from hunter.apply_api import main_api

    with patch("hunter.apply_api.notify"):
        with pytest.raises(SystemExit):
            main_api("https://example.com/api-continue")


def test_api_pipeline_passes_force_override_false_for_paste(monkeypatch) -> None:
    """A manual paste is NOT an override anymore (docs/DOOMED_GATE_PASTE_PLAN.md)
    — a HARD finding on a pasted job blocks generation exactly like an
    auto-discovered one."""
    _patch_api_pre_gate(monkeypatch)
    calls = {}

    def _capture(job_text, url, *, title="", company="", is_force_override=False):
        calls["is_force_override"] = is_force_override
        return True  # abort immediately, don't care about the rest

    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", _capture)

    from hunter.apply_api import main_api
    from hunter.apply_shared import PASTE_NO_URL_PLACEHOLDER

    with patch("hunter.apply_api.notify"):
        main_api(PASTE_NO_URL_PLACEHOLDER, paste_text="Pasted job text " * 20)

    assert calls["is_force_override"] is False


def test_api_pipeline_passes_force_override_true_for_skip_dedup(monkeypatch) -> None:
    _patch_api_pre_gate(monkeypatch)
    calls = {}

    def _capture(job_text, url, *, title="", company="", is_force_override=False):
        calls["is_force_override"] = is_force_override
        return True

    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", _capture)

    from hunter.apply_api import main_api

    with patch("hunter.apply_api.notify"):
        main_api("https://example.com/api-force", skip_dedup=True)

    assert calls["is_force_override"] is True


def test_api_pipeline_passes_force_override_false_for_normal_hunt_job(monkeypatch) -> None:
    _patch_api_pre_gate(monkeypatch)
    calls = {}

    def _capture(job_text, url, *, title="", company="", is_force_override=False):
        calls["is_force_override"] = is_force_override
        return True

    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", _capture)

    from hunter.apply_api import main_api

    with patch("hunter.apply_api.notify"):
        main_api("https://example.com/api-normal")

    assert calls["is_force_override"] is False


def test_api_pipeline_suppresses_manual_screen_warn_when_gate_enabled(monkeypatch) -> None:
    """Owner report 2026-07-11: Step 1.5e and the doomed gate both run
    assess_job_text, so every flagged paste warned TWICE with the same
    evidence. With the gate enabled, the coarser Step 1.5e message must not
    be sent — the gate's own warning (rule + evidence) covers it."""
    _patch_api_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: "some reason")
    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", lambda *a, **kw: True)

    from hunter.apply_api import main_api

    with (
        patch("hunter.config.DOOMED_GATE_ENABLED", True),
        patch("hunter.apply_api.notify") as mock_notify,
    ):
        main_api("https://example.com/api-screen-dup")

    assert not any("would normally be filtered" in c.args[0] for c in mock_notify.call_args_list)


def test_api_pipeline_manual_screen_still_warns_when_gate_disabled(monkeypatch) -> None:
    """With the doomed gate off, Step 1.5e is the only manual-paste warning
    left and must keep firing."""
    _patch_api_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: "some reason")
    # Gate disabled → returns False; abort at Step 2 via bogus PROMPTS_DIR.
    monkeypatch.setattr(
        "hunter.apply_api.PROMPTS_DIR", __import__("pathlib").Path("/nonexistent/prompts")
    )

    from hunter.apply_api import main_api

    with (
        patch("hunter.config.DOOMED_GATE_ENABLED", False),
        patch("hunter.apply_api.notify") as mock_notify,
    ):
        with pytest.raises(SystemExit):
            main_api("https://example.com/api-screen-solo")

    assert any("would normally be filtered" in c.args[0] for c in mock_notify.call_args_list)


def test_api_pipeline_gate_runs_after_manual_screen_source_order() -> None:
    """Source-position guard (repo precedent, see test_apply_api.py): Step 1.5f
    must sit after the Step 1.5e manual-screen block and before Step 2 (prompt
    read / first LLM call)."""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module("hunter.apply_api"))
    screen_pos = src.index("Step 1.5e")
    gate_pos = src.index("Step 1.5f")
    step2_pos = src.index("Step 2 —")
    assert screen_pos < gate_pos < step2_pos
    assert "run_doomed_gate(" in src


# ── apply_cli.py wiring ───────────────────────────────────────────────────────


def _patch_cli_pre_gate(monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli._already_processed", lambda *a, **kw: False)
    monkeypatch.setattr("hunter.expired_check.is_job_expired", lambda text: False)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: None)


def test_cli_pipeline_aborts_before_subprocess_when_gate_returns_true(monkeypatch) -> None:
    _patch_cli_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.apply_shared.run_doomed_gate", lambda *a, **kw: True)

    def _boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called when the gate aborts")

    monkeypatch.setattr("subprocess.run", _boom)

    from hunter.apply_cli import main_cli

    with patch("hunter.apply_cli.notify"):
        result = main_cli(
            "paste://no-url",
            paste_text="Pasted job text for the CLI pipeline " * 10,
        )

    assert result is None


def test_cli_pipeline_reaches_subprocess_when_gate_returns_false(monkeypatch) -> None:
    _patch_cli_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.apply_shared.run_doomed_gate", lambda *a, **kw: False)

    called = {}

    def _boom(*a, **kw):
        called["reached"] = True
        raise RuntimeError("stop here — proves we passed the gate")

    monkeypatch.setattr("subprocess.run", _boom)

    from hunter.apply_cli import main_cli

    with patch("hunter.apply_cli.notify"):
        with pytest.raises(RuntimeError, match="stop here"):
            main_cli(
                "paste://no-url",
                paste_text="Pasted job text for the CLI pipeline " * 10,
            )

    assert called.get("reached") is True


def test_cli_pipeline_passes_force_override_false_for_paste(monkeypatch) -> None:
    """A manual paste is NOT an override anymore (docs/DOOMED_GATE_PASTE_PLAN.md)."""
    _patch_cli_pre_gate(monkeypatch)
    calls = {}

    def _capture(job_text, url, *, title="", company="", is_force_override=False):
        calls["is_force_override"] = is_force_override
        return True

    monkeypatch.setattr("hunter.apply_shared.run_doomed_gate", _capture)

    from hunter.apply_cli import main_cli

    with patch("hunter.apply_cli.notify"):
        main_cli("paste://no-url", paste_text="Pasted job text " * 20)

    assert calls["is_force_override"] is False


def test_cli_pipeline_passes_force_override_true_for_skip_dedup(monkeypatch) -> None:
    _patch_cli_pre_gate(monkeypatch)
    calls = {}

    def _capture(job_text, url, *, title="", company="", is_force_override=False):
        calls["is_force_override"] = is_force_override
        return True

    monkeypatch.setattr("hunter.apply_shared.run_doomed_gate", _capture)

    from hunter.apply_cli import main_cli

    with patch("hunter.apply_cli.notify"):
        main_cli("paste://no-url", paste_text="Pasted job text " * 20, skip_dedup=True)

    assert calls["is_force_override"] is True


def test_cli_pipeline_suppresses_manual_screen_warn_when_gate_enabled(monkeypatch) -> None:
    """Mirror of the apply_api test — the CLI pipeline had the same duplicate
    warning (owner report 2026-07-11)."""
    _patch_cli_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: "some reason")
    monkeypatch.setattr("hunter.apply_shared.run_doomed_gate", lambda *a, **kw: True)

    from hunter.apply_cli import main_cli

    with (
        patch("hunter.config.DOOMED_GATE_ENABLED", True),
        patch("hunter.apply_cli.notify") as mock_notify,
    ):
        main_cli("paste://no-url", paste_text="Pasted job text " * 20)

    assert not any("would normally be filtered" in c.args[0] for c in mock_notify.call_args_list)


def test_cli_pipeline_manual_screen_still_warns_when_gate_disabled(monkeypatch) -> None:
    _patch_cli_pre_gate(monkeypatch)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: "some reason")

    def _stop(*a, **kw):
        raise RuntimeError("stop here — screen already ran")

    monkeypatch.setattr("subprocess.run", _stop)

    from hunter.apply_cli import main_cli

    with (
        patch("hunter.config.DOOMED_GATE_ENABLED", False),
        patch("hunter.apply_cli.notify") as mock_notify,
    ):
        with pytest.raises(RuntimeError, match="stop here"):
            main_cli("paste://no-url", paste_text="Pasted job text " * 20)

    assert any("would normally be filtered" in c.args[0] for c in mock_notify.call_args_list)


def test_cli_pipeline_gate_runs_after_manual_screen_source_order() -> None:
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module("hunter.apply_cli"))
    screen_pos = src.index("Step 1.5e")
    gate_pos = src.index("Step 1.5f")
    subprocess_cmd_pos = src.index('cmd = ["claude"')
    assert screen_pos < gate_pos < subprocess_cmd_pos
    assert "run_doomed_gate(" in src


# ── Config ────────────────────────────────────────────────────────────────────


def test_doomed_gate_config_defaults() -> None:
    from hunter.config import DOOMED_GATE_ENABLED, DOOMED_GATE_HARD_ACTION

    assert DOOMED_GATE_ENABLED is True
    assert DOOMED_GATE_HARD_ACTION == "skip"
