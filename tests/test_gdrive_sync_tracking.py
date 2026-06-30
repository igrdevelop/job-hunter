"""Tests for Drive URL tracking in gdrive_sync.

Verifies:
- upload_application_folder writes Drive URL to tracker when job_url is given
- upload_missing_folders skips rows that already have a Drive URL
- upload_missing_folders writes Drive URL after each new upload
- already_uploaded count is correct
"""

import asyncio
import uuid
from unittest.mock import MagicMock, patch


from hunter import tracker
from hunter.db import get_db
from hunter.tracker import normalize_url


def run(coro):
    return asyncio.run(coro)


def _insert_row(tracker_db, *, url: str, folder_rel: str,
                drive_url: str = "", row_id: str = "") -> None:
    """Insert a minimal application row directly into the test SQLite DB."""
    rid = row_id or uuid.uuid4().hex[:8]
    norm = normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, folder, drive_url)
            VALUES (?, '2026-05-22', 'Acme', 'Dev', '85%', ?, ?, ?, ?)
            """,
            (rid, url, norm, folder_rel, drive_url),
        )


# ---------------------------------------------------------------------------
# upload_application_folder: writes Drive URL to tracker when job_url given
# ---------------------------------------------------------------------------

def test_upload_application_folder_writes_drive_url(tmp_path, tracker_db):
    folder = tmp_path / "2026-05-22" / "Acme"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    job_url = "https://example.com/jobs/1"
    drive_url = "https://drive.google.com/drive/folders/abc"
    _insert_row(tracker_db, url=job_url, folder_rel=str(folder))

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


def test_upload_application_folder_no_tracker_write_when_no_job_url(tmp_path, tracker_db):
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

def test_upload_missing_skips_already_uploaded_rows(tmp_path, tracker_db):
    job_url = "https://example.com/jobs/1"
    drive_url = "https://drive.google.com/drive/folders/existing"
    folder = tmp_path / "Applications" / "2026-05-22" / "Acme"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    _insert_row(tracker_db, url=job_url, folder_rel=str(folder), drive_url=drive_url)

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


def test_upload_missing_uploads_and_writes_drive_url(tmp_path, tracker_db):
    job_url = "https://example.com/jobs/2"
    folder = tmp_path / "Applications" / "2026-05-22" / "Corp"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")

    _insert_row(tracker_db, url=job_url, folder_rel=str(folder), drive_url="")

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


def test_upload_missing_counts_already_and_new_separately(tmp_path, tracker_db):
    folder_done = tmp_path / "Applications" / "2026-05-22" / "DoneCorpX"
    folder_done.mkdir(parents=True)
    (folder_done / "cv.pdf").write_bytes(b"x")
    folder_new = tmp_path / "Applications" / "2026-05-22" / "NewCorpY"
    folder_new.mkdir(parents=True)
    (folder_new / "cv.pdf").write_bytes(b"y")

    url_done = "https://example.com/jobs/done"
    url_new  = "https://example.com/jobs/new"

    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, folder, drive_url)
            VALUES ('id111111', '2026-05-22', 'DoneCorpX', 'Dev', '85%', ?, ?, ?, ?)
            """,
            (url_done, normalize_url(url_done), str(folder_done),
             "https://drive.google.com/drive/folders/done"),
        )
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, folder, drive_url)
            VALUES ('id222222', '2026-05-22', 'NewCorpY', 'Dev', '80%', ?, ?, ?, '')
            """,
            (url_new, normalize_url(url_new), str(folder_new)),
        )

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
    assert tracker.get_drive_url_by_url(url_new) == new_url
    # Already-uploaded row must remain unchanged
    assert tracker.get_drive_url_by_url(url_done) == "https://drive.google.com/drive/folders/done"


# ---------------------------------------------------------------------------
# upload_missing_folders: dual-apply shadow subfolders (no tracker row of their own)
# ---------------------------------------------------------------------------

def test_upload_missing_also_uploads_shadow_subfolder(tmp_path, tracker_db):
    """A {company}/{shadow_profile}/ subfolder has no Drive URL column of its own —
    it must still be picked up and uploaded even when the company row is already
    marked as uploaded."""
    job_url = "https://example.com/jobs/3"
    folder = tmp_path / "Applications" / "2026-05-22" / "ShadowCorp"
    folder.mkdir(parents=True)
    (folder / "cv.pdf").write_bytes(b"x")
    shadow_sub = folder / "deepseek-v3"
    shadow_sub.mkdir()
    (shadow_sub / "cv_ats88.pdf").write_bytes(b"y")

    # Company already uploaded — primary upload_folder must NOT be called again.
    _insert_row(
        tracker_db, url=job_url, folder_rel=str(folder),
        drive_url="https://drive.google.com/drive/folders/existing",
    )

    with (
        patch("hunter.gdrive_sync.GDRIVE_ENABLED", True),
        patch("hunter.gdrive_sync._get_service", return_value=MagicMock()),
        patch("hunter.gdrive_client.upload_folder") as mock_upload_folder,
        patch(
            "hunter.gdrive_sync.upload_shadow_folder",
            return_value="https://drive.google.com/drive/folders/shadow",
        ) as mock_upload_shadow,
    ):
        from hunter import gdrive_sync
        result = run(gdrive_sync.upload_missing_folders(tmp_path))

    mock_upload_folder.assert_not_called()  # company folder unchanged
    mock_upload_shadow.assert_called_once_with(folder, shadow_sub)
    assert result["uploaded"] == 0
    assert result["already_uploaded"] == 1
    assert result["shadow_uploaded"] == 1
    assert result["shadow_errors"] == []
