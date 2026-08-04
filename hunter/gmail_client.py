import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_PATH = Path(__file__).parent.parent / "gmail_token.json"

LABEL_NAME = "Job Hunter/Processed"

logger = logging.getLogger(__name__)


def get_gmail_service():
    """Return an authorized Gmail API client, refreshing the token if expired."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"{TOKEN_PATH} not found. Run: python tools/gmail_auth.py")

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds.expired and creds.refresh_token:
        from hunter.oauth_alert import refresh_or_alert

        refresh_or_alert(
            creds,
            Request(),
            TOKEN_PATH,
            service="Gmail",
            reauth_cmd="python tools/gmail_auth.py",
        )

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service) -> str | None:
    """Return the label ID for LABEL_NAME, creating it if it doesn't exist."""
    try:
        results = service.users().labels().list(userId="me").execute()
        for label in results.get("labels", []):
            if label["name"] == LABEL_NAME:
                return label["id"]
        body = {
            "name": LABEL_NAME,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = service.users().labels().create(userId="me", body=body).execute()
        logger.info("[gmail] Created label %r (id=%s)", LABEL_NAME, created["id"])
        return created["id"]
    except Exception as e:
        logger.warning("[gmail] Failed to get/create label %r: %s", LABEL_NAME, e)
        return None


def label_messages(service, message_ids: list[str], label_id: str) -> int:
    """Add a label to a list of messages. Returns count of successfully labeled."""
    labeled = 0
    for mid in message_ids:
        try:
            service.users().messages().modify(
                userId="me", id=mid, body={"addLabelIds": [label_id]}
            ).execute()
            labeled += 1
        except Exception:
            pass
    return labeled
