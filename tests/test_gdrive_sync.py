"""
Unit tests for hunter/gdrive_sync.py.

All tests are fully mocked — no network, no Drive API calls.
Uses synchronous asyncio.run() wrappers (no pytest-asyncio dependency).
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _ready()
# ---------------------------------------------------------------------------

def test_ready_false_when_disabled():
    with patch("hunter.gdrive_sync.GDRIVE_ENABLED", False):
        from hunter import gdrive_sync
        assert not gdrive_sync._ready()


def test_ready_false_when_no_service():
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=None),
    ):
        from hunter import gdrive_sync
        assert not gdrive_sync._ready()


def test_ready_true_when_enabled_and_service():
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gdrive_sync
        assert gdrive_sync._ready()


# ---------------------------------------------------------------------------
# upload_application_folder — no-op cases
# ---------------------------------------------------------------------------

def test_upload_noop_when_disabled():
    with patch("hunter.gdrive_sync.GDRIVE_ENABLED", False):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(Path("/tmp/fake")))
    assert result is None


def test_upload_noop_when_folder_missing(tmp_path):
    missing = tmp_path / "nonexistent"
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(missing))
    assert result is None


# ---------------------------------------------------------------------------
# upload_application_folder — happy path
# ---------------------------------------------------------------------------

def test_upload_creates_drive_structure(tmp_path):
    # Create Applications/2026-05-15/Acme with a file
    folder = tmp_path / "2026-05-15" / "Acme"
    folder.mkdir(parents=True)
    (folder / "CV_EN.pdf").write_bytes(b"cv")

    mock_svc = MagicMock()
    company_folder_id = "company_folder_id"

    # Patch at the source module because gdrive_sync imports lazily inside the function.
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", ""),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_NAME", "Job Hunter"),
        patch("hunter.gdrive_sync._get_service", return_value=mock_svc),
        patch("hunter.gdrive_client.get_or_create_folder") as mock_goc,
        patch("hunter.gdrive_client.upload_folder", return_value=company_folder_id) as mock_uf,
        patch("hunter.gdrive_client.folder_url", return_value="https://drive.google.com/drive/folders/company_folder_id"),
    ):
        mock_goc.side_effect = ["root_id", "date_id"]
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(folder))

    # Should create root "Job Hunter", then date folder, then upload company folder
    assert mock_goc.call_count == 2
    root_call = mock_goc.call_args_list[0]
    assert root_call.args[1] == "Job Hunter"
    assert root_call.args[2] is None

    date_call = mock_goc.call_args_list[1]
    assert date_call.args[1] == "2026-05-15"

    mock_uf.assert_called_once()
    assert result == "https://drive.google.com/drive/folders/company_folder_id"


def test_upload_uses_root_folder_id_when_set(tmp_path):
    folder = tmp_path / "2026-05-15" / "TechCorp"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "my_root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="date_id") as mock_goc,
        patch("hunter.gdrive_client.upload_folder", return_value="company_id"),
        patch("hunter.gdrive_client.folder_url", return_value="https://drive.google.com/drive/folders/company_id"),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(folder))

    # Should skip root folder creation — only 1 call for the date folder
    assert mock_goc.call_count == 1
    date_call = mock_goc.call_args_list[0]
    assert date_call.args[1] == "2026-05-15"
    assert date_call.args[2] == "my_root_id"

    assert result is not None


# ---------------------------------------------------------------------------
# upload_application_folder — error handling
# ---------------------------------------------------------------------------

def test_upload_returns_none_on_error(tmp_path):
    folder = tmp_path / "2026-05-15" / "Broken"
    folder.mkdir(parents=True)

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", ""),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", side_effect=RuntimeError("API down")),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(folder))

    # Error must be swallowed — never propagated
    assert result is None


# ---------------------------------------------------------------------------
# upload_log_file
# ---------------------------------------------------------------------------

def test_upload_log_file_noop_when_disabled(tmp_path):
    log_file = tmp_path / "hunter_errors.log"
    log_file.write_text("some errors")
    with patch("hunter.gdrive_sync.GDRIVE_ENABLED", False):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file))
    assert result is None


def test_upload_log_file_noop_when_missing(tmp_path):
    missing = tmp_path / "nonexistent.log"
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(missing))
    assert result is None


def test_upload_log_file_happy_path(tmp_path):
    log_file = tmp_path / "hunter_errors.log"
    log_file.write_text("2026-05-27 [ERROR] something bad")

    mock_svc = MagicMock()
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", ""),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_NAME", "Job Hunter"),
        patch("hunter.gdrive_sync._get_service", return_value=mock_svc),
        patch("hunter.gdrive_client.get_or_create_folder") as mock_goc,
        patch("hunter.gdrive_client.upload_file", return_value="file123") as mock_uf,
    ):
        mock_goc.side_effect = ["root_id", "logs_folder_id"]
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file))

    # Creates root → Logs subfolder → uploads the file
    assert mock_goc.call_count == 2
    assert mock_goc.call_args_list[0].args[1] == "Job Hunter"
    assert mock_goc.call_args_list[0].args[2] is None   # root has no parent
    assert mock_goc.call_args_list[1].args[1] == "Logs"
    assert mock_goc.call_args_list[1].args[2] == "root_id"  # Logs is inside root
    mock_uf.assert_called_once_with(mock_svc, log_file, "logs_folder_id")
    # Returns a file view URL (not a folder URL)
    assert result == "https://drive.google.com/file/d/file123/view"


def test_upload_log_file_uses_existing_root_id(tmp_path):
    log_file = tmp_path / "hunter_errors.log"
    log_file.write_text("errors")

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "preset_root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="logs_id") as mock_goc,
        patch("hunter.gdrive_client.upload_file", return_value="fid"),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file))

    # Only one get_or_create_folder call (for Logs/ subfolder — root is preset)
    assert mock_goc.call_count == 1
    assert mock_goc.call_args_list[0].args[1] == "Logs"
    assert mock_goc.call_args_list[0].args[2] == "preset_root_id"
    assert result == "https://drive.google.com/file/d/fid/view"


def test_upload_log_file_returns_none_on_error(tmp_path):
    log_file = tmp_path / "hunter_errors.log"
    log_file.write_text("errors")

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", side_effect=RuntimeError("API down")),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file))

    # Best-effort — error swallowed
    assert result is None
