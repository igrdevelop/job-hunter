"""
One-time Google Sheets OAuth authorization.

Run once:
    python tools/gsheets_auth.py

A browser window will open — log in to Google and allow access.
gsheets_token.json will be created in the project root.
After that, the bot uses the token automatically (no browser needed again).

Then copy token to VPS:
    scp gsheets_token.json deploy@<host>:/home/deploy/job-hunter/
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
ROOT = Path(__file__).parent.parent
CREDENTIALS_FILE = ROOT / "gsheets_credentials.json"
TOKEN_FILE = ROOT / "gsheets_token.json"


def main():
    if not CREDENTIALS_FILE.exists():
        print(f"ERROR: {CREDENTIALS_FILE} not found.")
        print("Download it from Google Cloud Console:")
        print("  APIs & Services → Credentials → OAuth 2.0 Client → Download JSON")
        print("  (Use a Desktop app credential, same as for Gmail)")
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"Done! Token saved to {TOKEN_FILE}")
    print("Copy it to the VPS, then restart the container.")


if __name__ == "__main__":
    main()
