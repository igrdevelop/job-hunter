"""Tests for apply_agent.main() dispatcher and apply_agent.py as a thin entry point.

Verifies Phase 4 steps 4.3 (callable as import) and 4.4 (thin CLI entry point).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ── 4.4 — apply_agent.py is a thin shim (≤ 230 lines) ───────────────────────


def test_apply_agent_is_thin() -> None:
    """apply_agent.py must stay a thin shim after the Phase 4 refactor.

    Ceiling was 200 pre-`ruff format`; the formatter's line-wrapping style
    costs ~18 lines with zero added logic, so the guard became 230. Raised to
    255 for the M4 outage→CLI fallback (docs/LLM_OUTAGE_RESILIENCE_PLAN.md,
    2026-07-18): ~25 lines of genuine PIPELINE-CHOICE logic, which is exactly
    the dispatcher's job — pipeline internals still live in apply_api/apply_cli.
    """
    here = Path(__file__).parent.parent / "apply_agent.py"
    lines = here.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 255, (
        f"apply_agent.py has {len(lines)} lines — expected ≤ 255 (thin dispatcher guard)"
    )


def test_apply_agent_has_no_pipeline_logic() -> None:
    """apply_agent.py must NOT contain the pipeline logic (moved to hunter/)."""
    here = Path(__file__).parent.parent / "apply_agent.py"
    src = here.read_text(encoding="utf-8")
    # These are internal implementation details that should only live in apply_shared/api/cli
    for symbol in ("_review_cover_letter", "_translate_cover_letter_pl", "_ats_check_loop"):
        assert symbol + " = " not in src and "def " + symbol not in src, (
            f"apply_agent.py must not define {symbol!r} (it belongs in hunter/apply_shared.py)"
        )


# ── 4.3 — pipelines are callable as imports (no subprocess required) ─────────


def test_main_api_callable_as_import() -> None:
    """main_api must be importable and callable without running a subprocess."""
    from hunter.apply_api import main_api
    import inspect

    assert callable(main_api)
    sig = inspect.signature(main_api)
    # Key property: no module-level state — all config via parameters
    assert "skip_dedup" in sig.parameters
    assert "full_mode" in sig.parameters


def test_main_cli_callable_as_import() -> None:
    """main_cli must be importable and callable without subprocess."""
    from hunter.apply_cli import main_cli
    import inspect

    assert callable(main_cli)
    sig = inspect.signature(main_cli)
    assert "skip_dedup" in sig.parameters
    assert "full_mode" in sig.parameters


def test_pipelines_have_no_module_level_globals() -> None:
    """Both pipeline modules must not define mutable module-level state flags."""
    import hunter.apply_api as api_mod
    import hunter.apply_cli as cli_mod

    for mod in (api_mod, cli_mod):
        # These were the old globals in apply_agent.py — must not exist anymore
        assert not hasattr(mod, "_SKIP_DEDUP"), f"{mod.__name__} still has _SKIP_DEDUP global"
        assert not hasattr(mod, "_FULL_MODE"), f"{mod.__name__} still has _FULL_MODE global"
        assert not hasattr(mod, "_APPLY_META_COMPANY"), (
            f"{mod.__name__} still has _APPLY_META_COMPANY global"
        )
        assert not hasattr(mod, "_APPLY_META_TITLE"), (
            f"{mod.__name__} still has _APPLY_META_TITLE global"
        )


# ── main() dispatcher — paste flow ────────────────────────────────────────────


def test_main_paste_text_with_cli_still_uses_api(monkeypatch) -> None:
    """API key set → API primary, even with a logged-in CLI present.

    (Was `..._uses_cli`: the old "CLI detected → try CLI first" auto-preference
    was removed 2026-07-18 — the CLI is reserved for the outage fallback.)
    """
    api_calls = []

    def fake_main_api(
        url,
        paste_text="",
        *,
        skip_dedup=False,
        full_mode=False,
        jobleads_company="",
        jobleads_title="",
        permalink="",
    ):
        api_calls.append((url, paste_text))

    monkeypatch.setattr("apply_agent.main_api", fake_main_api)
    monkeypatch.setattr("apply_agent.main_cli", lambda *a, **k: pytest.fail("CLI must not run"))
    monkeypatch.setattr("apply_agent._is_cli_available", lambda: True)
    monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)
    monkeypatch.setattr("apply_agent.LLM_API_KEY", "test-key")

    import apply_agent

    apply_agent.main("https://example.com/job/1", paste_text="Job posting text here.")

    assert len(api_calls) == 1
    assert api_calls[0][1] == "Job posting text here."


def test_main_paste_text_without_cli_uses_api(monkeypatch) -> None:
    """When paste_text is provided and CLI is unavailable, main() uses API."""
    api_calls = []

    def fake_main_api(
        url,
        paste_text="",
        *,
        skip_dedup=False,
        full_mode=False,
        jobleads_company="",
        jobleads_title="",
        permalink="",
    ):
        api_calls.append((url, paste_text))

    monkeypatch.setattr("apply_agent.main_api", fake_main_api)
    monkeypatch.setattr("apply_agent._is_cli_available", lambda: False)
    monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)
    monkeypatch.setattr("apply_agent.LLM_API_KEY", "test-key")

    import apply_agent

    apply_agent.main("https://example.com/job/1", paste_text="Job posting text here.")

    assert len(api_calls) == 1
    assert api_calls[0][1] == "Job posting text here."


def test_main_paste_without_api_key_exits(monkeypatch) -> None:
    """main() with paste_text, no API key and no CLI login must sys.exit(1).

    _is_cli_available is pinned False: unpatched, this test would take the real
    CLI path on any dev machine with a logged-in `claude` install.
    """
    monkeypatch.setattr("apply_agent.LLM_API_KEY", "")
    monkeypatch.setattr("apply_agent._is_cli_available", lambda: False)
    import apply_agent

    with pytest.raises(SystemExit) as exc:
        apply_agent.main("https://example.com/job/1", paste_text="Some text.")
    assert exc.value.code == 1


# ── main() dispatcher — force_cli flag ────────────────────────────────────────


def test_main_force_cli_calls_main_cli_directly(monkeypatch) -> None:
    """--cli flag must send directly to main_cli without checking CLI availability."""
    cli_calls = []

    def fake_main_cli(url, *, skip_dedup=False, full_mode=False, paste_text="", permalink=""):
        cli_calls.append(url)

    monkeypatch.setattr("apply_agent.main_cli", fake_main_cli)
    monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)

    import apply_agent

    apply_agent.main("https://example.com/job/2", force_cli=True)

    assert cli_calls == ["https://example.com/job/2"]


# ── main() dispatcher — API-only fallback ────────────────────────────────────


def test_main_no_cli_calls_main_api(monkeypatch) -> None:
    """When CLI is unavailable and API key exists, main() must call main_api."""
    api_calls = []

    def fake_main_api(
        url,
        paste_text="",
        *,
        skip_dedup=False,
        full_mode=False,
        jobleads_company="",
        jobleads_title="",
        permalink="",
    ):
        api_calls.append(url)

    def fake_is_cli_available():
        return False

    monkeypatch.setattr("apply_agent.main_api", fake_main_api)
    monkeypatch.setattr("apply_agent._is_cli_available", fake_is_cli_available)
    monkeypatch.setattr("apply_agent.LLM_API_KEY", "test-key")
    monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)

    import apply_agent

    apply_agent.main("https://example.com/job/3")

    assert api_calls == ["https://example.com/job/3"]


def test_main_no_cli_no_api_key_exits(monkeypatch) -> None:
    """When neither CLI nor API key is available, main() must sys.exit(1)."""
    monkeypatch.setattr("apply_agent._is_cli_available", lambda: False)
    monkeypatch.setattr("apply_agent.LLM_API_KEY", "")
    monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)

    import apply_agent

    with pytest.raises(SystemExit) as exc:
        apply_agent.main("https://example.com/job/4")
    assert exc.value.code == 1


# ── main() dispatcher — CLI-first with API fallback ──────────────────────────


def test_main_cli_failure_falls_back_to_api(monkeypatch) -> None:
    """When CLI raises ApplyError and API key is set, main() falls back to main_api."""
    from hunter.apply_shared import ApplyError

    api_calls = []

    def fake_main_cli(url, *, skip_dedup=False, full_mode=False, paste_text="", permalink=""):
        raise ApplyError("CLI failed")

    def fake_main_api(
        url,
        paste_text="",
        *,
        skip_dedup=False,
        full_mode=False,
        jobleads_company="",
        jobleads_title="",
        permalink="",
    ):
        api_calls.append(url)

    monkeypatch.setattr("apply_agent.main_cli", fake_main_cli)
    monkeypatch.setattr("apply_agent.main_api", fake_main_api)
    monkeypatch.setattr("apply_agent._is_cli_available", lambda: True)
    monkeypatch.setattr("apply_agent.LLM_API_KEY", "test-key")
    monkeypatch.setattr("apply_agent.APPLY_USE_CLI", False)

    with patch("apply_agent.notify"):
        import apply_agent

        apply_agent.main("https://example.com/job/5")

    assert api_calls == ["https://example.com/job/5"]


# ── parse_apply_cli_argv ──────────────────────────────────────────────────────


def test_parse_apply_cli_argv_stays_in_apply_agent() -> None:
    """parse_apply_cli_argv must remain in apply_agent.py (tests import it directly)."""
    import apply_agent

    assert hasattr(apply_agent, "parse_apply_cli_argv")
    assert callable(apply_agent.parse_apply_cli_argv)


def test_parse_apply_cli_argv_force_and_full() -> None:
    from apply_agent import parse_apply_cli_argv

    url, force_cli, force, full, co, ti, paste_file, notify_start, permalink = parse_apply_cli_argv(
        ["apply_agent.py", "https://example.com/j/1", "--force", "--full"]
    )
    assert url == "https://example.com/j/1"
    assert force is True
    assert full is True
    assert force_cli is False
    assert permalink == ""
