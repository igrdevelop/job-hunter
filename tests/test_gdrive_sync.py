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
# upload_log_file — per-day files (YYYY-MM-DD.log)
# ---------------------------------------------------------------------------

_TEST_DATE = "2026-05-27"  # fixed date used across all upload_log_file tests


def _make_log(tmp_path, *extra_lines):
    """Write a log file with some entries for _TEST_DATE and one old entry."""
    log_file = tmp_path / "hunter_errors.log"
    lines = [
        f"{_TEST_DATE} 10:00:00 [INFO] hunter.sources.gmail: ✉ from='jobs@linkedin.com'\n",
        f"{_TEST_DATE} 10:00:01 [WARNING] hunter.gmail_enricher: FAILED for https://x.com — timeout\n",
        # Traceback continuation (no timestamp) — must be kept with the WARNING above
        "Traceback (most recent call last):\n",
        '  File "enricher.py", line 42, in _enrich_one\n',
        "TimeoutError\n",
        # Old entry from a different day — must be excluded
        "2020-01-01 00:00:00 [INFO] old day entry — must NOT appear\n",
        *extra_lines,
    ]
    log_file.write_text("".join(lines), encoding="utf-8")
    return log_file


def test_upload_log_file_noop_when_disabled(tmp_path):
    log_file = _make_log(tmp_path)
    with patch("hunter.gdrive_sync.GDRIVE_ENABLED", False):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file, date_str=_TEST_DATE))
    assert result is None


def test_upload_log_file_noop_when_missing(tmp_path):
    missing = tmp_path / "nonexistent.log"
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(missing, date_str=_TEST_DATE))
    assert result is None


def test_upload_log_file_noop_when_no_entries_for_date(tmp_path):
    """If the log has no lines for the requested date, return None without uploading."""
    log_file = tmp_path / "hunter_errors.log"
    log_file.write_text("2020-01-01 00:00:00 [INFO] old stuff\n")
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file, date_str=_TEST_DATE))
    assert result is None


def test_upload_log_file_happy_path(tmp_path):
    log_file = _make_log(tmp_path)

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
        result = run(gdrive_sync.upload_log_file(log_file, date_str=_TEST_DATE))

    # Creates root → Logs subfolder → uploads dated file
    assert mock_goc.call_count == 2
    assert mock_goc.call_args_list[0].args[1] == "Job Hunter"
    assert mock_goc.call_args_list[0].args[2] is None      # root has no parent
    assert mock_goc.call_args_list[1].args[1] == "Logs"
    assert mock_goc.call_args_list[1].args[2] == "root_id" # Logs is inside root
    # The file uploaded must be named YYYY-MM-DD.log (not the original filename)
    uploaded_path = mock_uf.call_args.args[1]
    assert uploaded_path.name == f"{_TEST_DATE}.log"
    assert result == "https://drive.google.com/file/d/file123/view"


def test_upload_log_file_dated_content_excludes_old_entries(tmp_path):
    """Only lines for _TEST_DATE (plus traceback continuations) must be uploaded."""
    log_file = _make_log(tmp_path)
    captured: list[str] = []

    def fake_upload(svc, path, parent_id):
        captured.append(path.read_text(encoding="utf-8"))
        return "fid"

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="logs_id"),
        patch("hunter.gdrive_client.upload_file", side_effect=fake_upload),
    ):
        from hunter import gdrive_sync
        run(gdrive_sync.upload_log_file(log_file, date_str=_TEST_DATE))

    assert captured, "upload_file was not called"
    content = captured[0]
    assert _TEST_DATE in content                     # today's entries present
    assert "old day entry" not in content            # old day excluded
    assert "TimeoutError" in content                 # traceback continuation kept
    assert "Traceback (most recent call last)" in content


def test_upload_log_file_uses_existing_root_id(tmp_path):
    log_file = _make_log(tmp_path)

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "preset_root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="logs_id") as mock_goc,
        patch("hunter.gdrive_client.upload_file", return_value="fid"),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file, date_str=_TEST_DATE))

    # Only one get_or_create_folder call: Logs/ under the preset root (no root lookup)
    assert mock_goc.call_count == 1
    assert mock_goc.call_args_list[0].args[1] == "Logs"
    assert mock_goc.call_args_list[0].args[2] == "preset_root_id"
    assert result == "https://drive.google.com/file/d/fid/view"


def test_upload_log_file_returns_none_on_error(tmp_path):
    log_file = _make_log(tmp_path)

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", side_effect=RuntimeError("API down")),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_log_file(log_file, date_str=_TEST_DATE))

    assert result is None  # best-effort — error swallowed
