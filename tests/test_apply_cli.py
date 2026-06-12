"""Tests for hunter/apply_cli.py — CLI pipeline helpers."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hunter.apply_cli import (
    _find_new_folder,
    _get_existing_folders,
    _is_cli_available,
    main_cli,
)
from hunter.apply_shared import ApplyError


# ── Import sanity ─────────────────────────────────────────────────────────────

def test_main_cli_is_importable() -> None:
    from hunter.apply_cli import main_cli
    assert callable(main_cli)


def test_apply_cli_exports_expected_symbols() -> None:
    import hunter.apply_cli as m
    assert callable(m._get_existing_folders)
    assert callable(m._find_new_folder)
    assert callable(m._is_cli_available)
    assert callable(m.main_cli)


# ── _get_existing_folders ─────────────────────────────────────────────────────

def test_get_existing_folders_empty_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    assert _get_existing_folders() == set()


def test_get_existing_folders_missing_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path / "nonexistent")
    assert _get_existing_folders() == set()


def test_get_existing_folders_new_structure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    # New structure: Applications/2026-05-27/Acme/
    (tmp_path / "2026-05-27" / "Acme").mkdir(parents=True)
    result = _get_existing_folders()
    assert "2026-05-27/Acme" in result


def test_get_existing_folders_legacy_structure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    # Legacy: Applications/Acme_2026-05-27/
    (tmp_path / "Acme_2026-05-27").mkdir()
    result = _get_existing_folders()
    assert "Acme_2026-05-27" in result


def test_get_existing_folders_skips_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    (tmp_path / "some_file.txt").write_text("content")
    result = _get_existing_folders()
    assert "some_file.txt" not in result


def test_get_existing_folders_mixed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    (tmp_path / "2026-05-27" / "Corp1").mkdir(parents=True)
    (tmp_path / "2026-05-27" / "Corp2").mkdir(parents=True)
    (tmp_path / "OldCompany_2025-01-01").mkdir()
    result = _get_existing_folders()
    assert "2026-05-27/Corp1" in result
    assert "2026-05-27/Corp2" in result
    assert "OldCompany_2025-01-01" in result


# ── _find_new_folder ──────────────────────────────────────────────────────────

def test_find_new_folder_detects_new_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    before: set[str] = set()
    # Create a new folder while the function is looking
    (tmp_path / "NewCompany").mkdir()
    result = _find_new_folder(before, timeout=0)
    assert result == "NewCompany"


def test_find_new_folder_returns_none_on_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    before: set[str] = set()
    # Nothing new — should return None immediately (timeout=0)
    result = _find_new_folder(before, timeout=0)
    assert result is None


def test_find_new_folder_ignores_known_folders(tmp_path, monkeypatch) -> None:
    import os
    import time as _time
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
    old_folder = tmp_path / "OldCompany"
    old_folder.mkdir()
    # Back-date its mtime by 60 seconds so the 5-second recency window doesn't fire
    old_ts = _time.time() - 60
    os.utime(old_folder, (old_ts, old_ts))
    before = {"OldCompany"}
    # OldCompany is in 'before' AND old — should not be returned
    result = _find_new_folder(before, timeout=0)
    assert result is None


# ── _is_cli_available ─────────────────────────────────────────────────────────

def test_is_cli_available_when_claude_not_found(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise FileNotFoundError("claude not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert _is_cli_available() is False


def test_is_cli_available_when_nonzero_exit(monkeypatch) -> None:
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    assert _is_cli_available() is False


def test_is_cli_available_when_not_logged_in(monkeypatch) -> None:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Claude CLI not logged in"
    mock_result.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    assert _is_cli_available() is False


def test_is_cli_available_when_ok(monkeypatch) -> None:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "claude 1.0.0"
    mock_result.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    assert _is_cli_available() is True


def test_is_cli_available_on_timeout(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=15)
    monkeypatch.setattr(subprocess, "run", _raise)
    assert _is_cli_available() is False


# ── main_cli — dedup short-circuit ────────────────────────────────────────────

def test_main_cli_skips_when_already_processed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "hunter.apply_cli._already_processed",
        lambda url, skip_dedup=False: True,
    )
    with patch("hunter.apply_cli.notify") as mock_notify:
        main_cli("https://example.com/job/1")
    mock_notify.assert_called_once()
    assert "tracker" in mock_notify.call_args[0][0].lower() or "skipped" in mock_notify.call_args[0][0].lower()


def test_main_cli_skip_dedup_bypasses_tracker(monkeypatch) -> None:
    recorded = []

    def fake_already_processed(url, skip_dedup=False):
        recorded.append(skip_dedup)
        return False  # don't short-circuit

    monkeypatch.setattr("hunter.apply_cli._already_processed", fake_already_processed)
    monkeypatch.setattr("hunter.apply_cli._get_existing_folders", lambda: set())

    # Stop at fetch step
    with patch("hunter.sources.fetch_job_text", side_effect=RuntimeError("stop")):
        with patch("hunter.apply_cli.notify"):
            try:
                main_cli("https://example.com/job/2", skip_dedup=True)
            except Exception:
                pass

    assert recorded == [True]


# ── main_cli — CLI failure raises ApplyError ──────────────────────────────────

def test_main_cli_raises_apply_error_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_cli._already_processed", lambda *a, **kw: False)
    monkeypatch.setattr("hunter.apply_cli._get_existing_folders", lambda: set())
    monkeypatch.setattr("hunter.apply_cli.CLI_MAX_RETRIES", 1)

    # Simulate fetch failure (pre-fetch optional)
    with patch("hunter.sources.fetch_job_text", side_effect=RuntimeError("net error")):
        # Simulate claude CLI returning nonzero
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "some error"
        with patch("subprocess.run", return_value=mock_result):
            with patch("hunter.apply_cli.notify"):
                with pytest.raises(ApplyError, match="CLI exited"):
                    main_cli("https://example.com/job/3")


# ── apply_agent re-exports main_cli and _is_cli_available ────────────────────

def test_apply_agent_reexports_cli_functions() -> None:
    import apply_agent
    assert callable(apply_agent.main_cli)
    assert callable(apply_agent._is_cli_available)
