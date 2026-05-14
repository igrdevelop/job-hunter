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
