"""Tests for Drive URL tracking in gdrive_sync.

Verifies:
- upload_application_folder writes Drive URL to tracker when job_url is given
- upload_missing_folders skips rows that already have a Drive URL
- upload_missing_folders writes Drive URL after each new upload
- already_uploaded count is correct
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from hunter import tracker


def run(coro):
    return asyncio.run(coro)


def _make_tracker_row(tmp_path, url: str, folder_rel: str, drive_url: str = "", row_id: str = "abc12345"):
    """Create a minimal tracker.xlsx with one row."""
    import openpyxl
    tracker_path = tmp_path / "tracker.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Company", "Job Title", "Stack", "ATS %", "URL",
               "Folder", "Sent", "Re-application", "To Learn", "ID", "Drive URL"])
    ws.append(["2026-05-22", "Acme", "Dev", "Angular", "85%",
               url, folder_rel, "", "", "", row_id, drive_url])
    wb.save(tracker_path)
    return tracker_path


# ---------------------------------------------------------------------------
# upload_application_folder: writes Drive URL to tracker when job_url given
# ---------------------------------------------------------------------------

def test_upload_application_folder_writes_drive_url(tmp_path, monkeypatch):
    folder = tmp_path / "2026-05-22" / "Acme"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    job_url = "https://example.com/jobs/1"
    drive_url = "https://drive.google.com/drive/folders/abc"
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)
    _make_tracker_row(tmp_path, job_url, str(folder))

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root123"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="date_id"),
        patch("hunter.gdrive_client.upload_folder", return_value="co_id"),
        patch("hunter.gdrive_client.folder_url", return_value=drive_url),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(folder, job_url=job_url))

    assert result == drive_url
    assert tracker.get_drive_url_by_url(job_url) == drive_url


def test_upload_application_folder_no_tracker_write_when_no_job_url(tmp_path, monkeypatch):
    folder = tmp_path / "2026-05-22" / "Acme"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    drive_url = "https://drive.google.com/drive/folders/abc"

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root123"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="date_id"),
        patch("hunter.gdrive_client.upload_folder", return_value="co_id"),
        patch("hunter.gdrive_client.folder_url", return_value=drive_url),
        patch("hunter.tracker.set_drive_url") as mock_set,
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_application_folder(folder))  # no job_url

    assert result == drive_url
    mock_set.assert_not_called()


# ---------------------------------------------------------------------------
# upload_missing_folders: skip rows with existing Drive URL
# ---------------------------------------------------------------------------

def test_upload_missing_skips_already_uploaded_rows(tmp_path, monkeypatch):
    job_url = "https://example.com/jobs/1"
    drive_url = "https://drive.google.com/drive/folders/existing"
    folder = tmp_path / "Applications" / "2026-05-22" / "Acme"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)
    _make_tracker_row(tmp_path, job_url, str(folder), drive_url=drive_url)

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.upload_folder") as mock_upload,
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_missing_folders(tmp_path))

    mock_upload.assert_not_called()
    assert result["uploaded"] == 0
    assert result["already_uploaded"] == 1
    assert result["skipped_missing"] == 0


def test_upload_missing_uploads_and_writes_drive_url(tmp_path, monkeypatch):
    job_url = "https://example.com/jobs/2"
    folder = tmp_path / "Applications" / "2026-05-22" / "Corp"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)
    # Folder path relative to tmp_path
    _make_tracker_row(tmp_path, job_url, str(folder), drive_url="")

    new_drive_url = "https://drive.google.com/drive/folders/newone"

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="date_id"),
        patch("hunter.gdrive_client.upload_folder", return_value="co_id"),
        patch("hunter.gdrive_client.folder_url", return_value=new_drive_url),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_missing_folders(tmp_path))

    assert result["uploaded"] == 1
    assert result["already_uploaded"] == 0
    assert result["errors"] == []
    assert tracker.get_drive_url_by_url(job_url) == new_drive_url


def test_upload_missing_counts_already_and_new_separately(tmp_path, monkeypatch):
    import openpyxl
    folder_done = tmp_path / "Applications" / "2026-05-22" / "DoneCorpX"
    folder_done.mkdir(parents=True)
    (folder_done / "cv.pdf").write_bytes(b"x")
    folder_new = tmp_path / "Applications" / "2026-05-22" / "NewCorpY"
    folder_new.mkdir(parents=True)
    (folder_new / "cv.pdf").write_bytes(b"y")

    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Company", "Job Title", "Stack", "ATS %", "URL",
               "Folder", "Sent", "Re-application", "To Learn", "ID", "Drive URL"])
    ws.append(["2026-05-22", "DoneCorpX", "Dev", "", "85%",
               "https://example.com/jobs/done", str(folder_done),
               "", "", "", "id111111", "https://drive.google.com/drive/folders/done"])
    ws.append(["2026-05-22", "NewCorpY", "Dev", "", "80%",
               "https://example.com/jobs/new", str(folder_new),
               "", "", "", "id222222", ""])
    wb.save(tracker_path)

    new_url = "https://drive.google.com/drive/folders/fresh"
    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync.GDRIVE_ROOT_FOLDER_ID", "root_id"),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.get_or_create_folder", return_value="date_id"),
        patch("hunter.gdrive_client.upload_folder", return_value="co_id"),
        patch("hunter.gdrive_client.folder_url", return_value=new_url),
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_missing_folders(tmp_path))

    assert result["uploaded"] == 1
    assert result["already_uploaded"] == 1
    assert result["skipped_missing"] == 0
    assert tracker.get_drive_url_by_url("https://example.com/jobs/new") == new_url
    # Already-uploaded row must remain unchanged
    assert tracker.get_drive_url_by_url("https://example.com/jobs/done") == "https://drive.google.com/drive/folders/done"
