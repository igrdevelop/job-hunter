from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = Path(__file__).parent.parent / "gmail_token.json"


def get_gmail_service():
    """Return an authorized Gmail API client, refreshing the token if expired."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"{TOKEN_PATH} not found. Run: python tools/gmail_auth.py"
        )

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)
