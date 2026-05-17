"""
Unit tests for hunter/gdrive_client.py.

All Google API calls are mocked — no network access needed.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from hunter.gdrive_client import (
    _q,
    folder_url,
    get_or_create_folder,
    upload_file,
    upload_folder,
    _find_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mock_service() -> MagicMock:
    """Return a MagicMock that chains .files().list/create/update().execute()."""
    return MagicMock()


def _files_list_result(files: list[dict]) -> MagicMock:
    """Return a mock execute() result for files().list()."""
    m = MagicMock()
    m.execute.return_value = {"files": files}
    return m


# ---------------------------------------------------------------------------
# _q (query escaping)
# ---------------------------------------------------------------------------

class TestQ:
    def test_simple(self):
        assert _q("hello") == "'hello'"

    def test_with_single_quote(self):
        assert _q("it's") == "'it\\'s'"

    def test_with_backslash(self):
        assert _q("a\\b") == "'a\\\\b'"


# ---------------------------------------------------------------------------
# folder_url
# ---------------------------------------------------------------------------

class TestFolderUrl:
    def test_format(self):
        assert folder_url("abc123") == "https://drive.google.com/drive/folders/abc123"


# ---------------------------------------------------------------------------
# get_or_create_folder
# ---------------------------------------------------------------------------

class TestGetOrCreateFolder:
    def test_reuses_existing_folder(self):
        svc = mock_service()
        svc.files().list.return_value = _files_list_result([{"id": "folder123", "name": "MyFolder"}])

        result = get_or_create_folder(svc, "MyFolder", parent_id="parent1")

        assert result == "folder123"
        svc.files().create.assert_not_called()

    def test_creates_when_missing(self):
        svc = mock_service()
        svc.files().list.return_value = _files_list_result([])
        svc.files().create.return_value.execute.return_value = {"id": "new_folder"}

        result = get_or_create_folder(svc, "NewFolder", parent_id="parent1")

        assert result == "new_folder"
        svc.files().create.assert_called_once()
        create_body = svc.files().create.call_args.kwargs["body"]
        assert create_body["name"] == "NewFolder"
        assert create_body["parents"] == ["parent1"]

    def test_creates_root_folder_no_parent(self):
        svc = mock_service()
        svc.files().list.return_value = _files_list_result([])
        svc.files().create.return_value.execute.return_value = {"id": "root_id"}

        result = get_or_create_folder(svc, "Job Hunter", parent_id=None)

        assert result == "root_id"
        create_body = svc.files().create.call_args.kwargs["body"]
        assert "parents" not in create_body

    def test_list_query_includes_parent(self):
        svc = mock_service()
        svc.files().list.return_value = _files_list_result([{"id": "f1", "name": "x"}])

        get_or_create_folder(svc, "MyFolder", parent_id="pid")

        q = svc.files().list.call_args.kwargs["q"]
        assert "'pid' in parents" in q
        assert "trashed = false" in q


# ---------------------------------------------------------------------------
# _find_file
# ---------------------------------------------------------------------------

class TestFindFile:
    def test_returns_id_when_found(self):
        svc = mock_service()
        svc.files().list.return_value = _files_list_result([{"id": "file_id"}])

        result = _find_file(svc, "cv.pdf", "parent1")

        assert result == "file_id"

    def test_returns_none_when_not_found(self):
        svc = mock_service()
        svc.files().list.return_value = _files_list_result([])

        result = _find_file(svc, "cv.pdf", "parent1")

        assert result is None


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

class TestUploadFile:
    def test_creates_new_file_when_not_existing(self, tmp_path):
        f = tmp_path / "cv.pdf"
        f.write_bytes(b"pdf content")

        svc = mock_service()
        svc.files().list.return_value = _files_list_result([])
        svc.files().create.return_value.execute.return_value = {"id": "new_file_id"}

        with patch("hunter.gdrive_client.MediaFileUpload"):
            result = upload_file(svc, f, "parent1")

        assert result == "new_file_id"
        svc.files().create.assert_called_once()
        svc.files().update.assert_not_called()

    def test_updates_existing_file(self, tmp_path):
        f = tmp_path / "cv.pdf"
        f.write_bytes(b"pdf content")

        svc = mock_service()
        # _find_file will return existing ID
        svc.files().list.return_value = _files_list_result([{"id": "existing_id"}])
        svc.files().update.return_value.execute.return_value = {"id": "existing_id"}

        with patch("hunter.gdrive_client.MediaFileUpload"):
            result = upload_file(svc, f, "parent1")

        assert result == "existing_id"
        svc.files().update.assert_called_once()
        svc.files().create.assert_not_called()


# ---------------------------------------------------------------------------
# upload_folder
# ---------------------------------------------------------------------------

class TestUploadFolder:
    def test_uploads_all_files(self, tmp_path):
        # Create a flat folder with 3 files
        folder = tmp_path / "2026-05-15" / "Acme"
        folder.mkdir(parents=True)
        (folder / "CV_EN.pdf").write_bytes(b"cv")
        (folder / "Cover_Letter_EN.pdf").write_bytes(b"cl")
        (folder / "job_posting.txt").write_text("job")

        svc = mock_service()
        # get_or_create_folder → returns company_folder_id
        svc.files().list.return_value = _files_list_result([])
        svc.files().create.return_value.execute.return_value = {"id": "company_folder_id"}

        with patch("hunter.gdrive_client.MediaFileUpload"), \
             patch("hunter.gdrive_client.get_or_create_folder", return_value="company_folder_id") as mock_goc, \
             patch("hunter.gdrive_client.upload_file", return_value="fid") as mock_uf:
            result = upload_folder(svc, folder, "date_folder_id")

        assert result == "company_folder_id"
        mock_goc.assert_called_once_with(svc, "Acme", "date_folder_id")
        assert mock_uf.call_count == 3

    def test_returns_folder_id(self, tmp_path):
        folder = tmp_path / "EmptyCompany"
        folder.mkdir()

        with patch("hunter.gdrive_client.get_or_create_folder", return_value="fid123"), \
             patch("hunter.gdrive_client.upload_file"):
            result = upload_folder(mock_service(), folder, "parent")

        assert result == "fid123"
