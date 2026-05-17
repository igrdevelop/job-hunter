"""
hunter/gdrive_client.py — Low-level Google Drive API v3 wrapper.

All public functions accept a built `service` object so callers can inject
mocks in tests. No global state.

Uses the same gsheets_token.json / gsheets_credentials.json — the token was
requested with scope drive.file (see gsheets_client.SCOPES), so no separate
OAuth flow is needed.
"""

import logging
import mimetypes
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

_FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def build_service(credentials_file: Path, token_file: Path) -> Any:
    """Load credentials and return a Drive API v3 service object.

    Reuses gsheets_token.json (already has drive.file scope).
    """
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        else:
            raise RuntimeError(
                f"gsheets_token.json is missing or invalid. "
                f"Run: python tools/gsheets_auth.py"
            )

    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Folder management
# ---------------------------------------------------------------------------

def get_or_create_folder(
    service: Any,
    name: str,
    parent_id: str | None = None,
) -> str:
    """Find a folder by name (under parent_id if given), create if missing.

    Returns folder_id.
    Reuses existing folder to handle re-apply (--force) gracefully.
    """
    query_parts = [
        f"name = {_q(name)}",
        f"mimeType = '{_FOLDER_MIME}'",
        "trashed = false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")

    query = " and ".join(query_parts)

    try:
        result = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
            )
            .execute()
        )
    except HttpError as e:
        log.error("gdrive get_or_create_folder list failed for %r: %s", name, e)
        raise

    files = result.get("files", [])
    if files:
        folder_id = files[0]["id"]
        log.debug("gdrive: reusing folder %r id=%s", name, folder_id)
        return folder_id

    # Create new folder
    metadata: dict = {
        "name": name,
        "mimeType": _FOLDER_MIME,
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    try:
        folder = (
            service.files()
            .create(body=metadata, fields="id")
            .execute()
        )
    except HttpError as e:
        log.error("gdrive get_or_create_folder create failed for %r: %s", name, e)
        raise

    folder_id = folder["id"]
    log.debug("gdrive: created folder %r id=%s", name, folder_id)
    return folder_id


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------

def upload_file(service: Any, file_path: Path, parent_id: str) -> str:
    """Upload a file to parent_id. Updates existing file if found by name.

    Returns file_id.
    """
    name = file_path.name
    mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    # Check if file already exists in parent
    existing_id = _find_file(service, name, parent_id)

    media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=False)

    try:
        if existing_id:
            file = (
                service.files()
                .update(fileId=existing_id, media_body=media, fields="id")
                .execute()
            )
            log.debug("gdrive: updated file %r id=%s", name, file["id"])
        else:
            metadata = {"name": name, "parents": [parent_id]}
            file = (
                service.files()
                .create(body=metadata, media_body=media, fields="id")
                .execute()
            )
            log.debug("gdrive: uploaded file %r id=%s", name, file["id"])
    except HttpError as e:
        log.error("gdrive upload_file failed for %r: %s", name, e)
        raise

    return file["id"]


def _find_file(service: Any, name: str, parent_id: str) -> str | None:
    """Return file_id if a file with this name exists in parent, else None."""
    query = (
        f"name = {_q(name)} and '{parent_id}' in parents "
        f"and mimeType != '{_FOLDER_MIME}' and trashed = false"
    )
    try:
        result = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id)", pageSize=1)
            .execute()
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None
    except HttpError as e:
        log.warning("gdrive _find_file failed for %r: %s", name, e)
        return None


# ---------------------------------------------------------------------------
# Folder upload (flat — no sub-directories)
# ---------------------------------------------------------------------------

def upload_folder(service: Any, folder_path: Path, parent_id: str) -> str:
    """Create a Drive folder for folder_path.name under parent_id and upload all files.

    Only uploads direct children (non-recursive) — Applications/ folders are flat.
    Returns folder_id of the created/reused Drive folder.
    """
    folder_id = get_or_create_folder(service, folder_path.name, parent_id)

    files = [f for f in folder_path.iterdir() if f.is_file()]
    log.info("gdrive: uploading %d file(s) from %s", len(files), folder_path)

    for f in files:
        upload_file(service, f, folder_id)

    return folder_id


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------

def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    """Escape a string for a Drive API query (single-quote escaping)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
