"""M4 (docs/LLM_OUTAGE_RESILIENCE_PLAN.md): opt-in CLI (Pro subscription)
fallback when the API account is down.

Contract: LLM_OUTAGE_FALLBACK_CLI=false (default) preserves today's behavior
byte-for-byte. When true AND the CLI is available:
  - the CLI-first auto-preference is skipped (CLI is reserved as fallback,
    the paid API stays primary);
  - an API exit 46 retries ONCE via main_cli — success is a normal apply
    (no pause armed), any CLI failure re-reports exit 46 so M1/M2 take over.
"""

import pytest

import apply_agent
from hunter.apply_shared import APPLY_LLM_OUTAGE_EXIT_CODE, ApplyError


class _Recorder:
    def __init__(self):
        self.calls: list = []


@pytest.fixture()
def rig(monkeypatch):
    """Patch apply_agent's seams; configure per-test via attributes."""
    rec = _Recorder()
    monkeypatch.setattr(apply_agent, "APPLY_USE_CLI", False)
    monkeypatch.setattr(apply_agent, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(apply_agent, "notify", lambda msg: rec.calls.append(("notify", msg)))
    monkeypatch.setattr(
        apply_agent,
        "_maybe_run_shadow",
        lambda folder, full: rec.calls.append(("shadow", folder)),
    )

    rec.api_effect: object = "api-folder"  # value → return; exception → raise
    rec.cli_effect: object = "cli-folder"

    def fake_api(*a, **k):
        rec.calls.append("api")
        if isinstance(rec.api_effect, BaseException):
            raise rec.api_effect
        return rec.api_effect

    def fake_cli(*a, **k):
        rec.calls.append("cli")
        if isinstance(rec.cli_effect, BaseException):
            raise rec.cli_effect
        return rec.cli_effect

    monkeypatch.setattr(apply_agent, "main_api", fake_api)
    monkeypatch.setattr(apply_agent, "main_cli", fake_cli)
    return rec


def _set(monkeypatch, *, flag: bool, cli_ok: bool):
    monkeypatch.setattr(apply_agent, "LLM_OUTAGE_FALLBACK_CLI", flag)
    monkeypatch.setattr(apply_agent, "_is_cli_available", lambda: cli_ok)


# ── flag OFF: today's behavior preserved ──────────────────────────────────────


def test_flag_off_outage_propagates(rig, monkeypatch):
    _set(monkeypatch, flag=False, cli_ok=False)
    rig.api_effect = SystemExit(APPLY_LLM_OUTAGE_EXIT_CODE)
    with pytest.raises(SystemExit) as ei:
        apply_agent.main("https://example.com/job/1")
    assert ei.value.code == APPLY_LLM_OUTAGE_EXIT_CODE
    assert "cli" not in rig.calls


def test_flag_off_cli_still_tried_first_when_available(rig, monkeypatch):
    _set(monkeypatch, flag=False, cli_ok=True)
    apply_agent.main("https://example.com/job/1")
    assert rig.calls[0] == "cli"
    assert "api" not in rig.calls


# ── flag ON: API primary, CLI reserved as fallback ────────────────────────────


def test_flag_on_api_is_primary(rig, monkeypatch):
    _set(monkeypatch, flag=True, cli_ok=True)
    apply_agent.main("https://example.com/job/1")
    assert rig.calls[0] == "api"
    assert "cli" not in rig.calls
    assert ("shadow", "api-folder") in rig.calls


def test_flag_on_outage_falls_back_to_cli(rig, monkeypatch):
    _set(monkeypatch, flag=True, cli_ok=True)
    rig.api_effect = SystemExit(APPLY_LLM_OUTAGE_EXIT_CODE)
    apply_agent.main("https://example.com/job/1")  # no SystemExit — normal apply
    assert [c for c in rig.calls if c in ("api", "cli")] == ["api", "cli"]
    assert ("shadow", "cli-folder") in rig.calls


def test_flag_on_cli_failure_reports_outage(rig, monkeypatch):
    _set(monkeypatch, flag=True, cli_ok=True)
    rig.api_effect = SystemExit(APPLY_LLM_OUTAGE_EXIT_CODE)
    rig.cli_effect = ApplyError("CLI died too")
    with pytest.raises(SystemExit) as ei:
        apply_agent.main("https://example.com/job/1")
    assert ei.value.code == APPLY_LLM_OUTAGE_EXIT_CODE


def test_flag_on_no_cli_installed_reports_outage(rig, monkeypatch):
    _set(monkeypatch, flag=True, cli_ok=False)
    rig.api_effect = SystemExit(APPLY_LLM_OUTAGE_EXIT_CODE)
    with pytest.raises(SystemExit) as ei:
        apply_agent.main("https://example.com/job/1")
    assert ei.value.code == APPLY_LLM_OUTAGE_EXIT_CODE
    assert "cli" not in rig.calls


def test_flag_on_non_outage_exits_propagate(rig, monkeypatch):
    """A normal skip path (sys.exit(0)) must never trigger the CLI fallback."""
    _set(monkeypatch, flag=True, cli_ok=True)
    rig.api_effect = SystemExit(0)
    with pytest.raises(SystemExit) as ei:
        apply_agent.main("https://example.com/job/1")
    assert ei.value.code == 0
    assert "cli" not in rig.calls
