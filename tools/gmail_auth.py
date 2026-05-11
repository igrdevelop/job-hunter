"""
One-time Gmail OAuth authorization.

Run once:
    python tools/gmail_auth.py

A browser window will open — log in to Google and allow access.
gmail_token.json will be created in the project root.
After that, the bot uses the token automatically (no browser needed again).
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
ROOT = Path(__file__).parent.parent
CREDENTIALS_FILE = ROOT / "gmail_credentials.json"
TOKEN_FILE = ROOT / "gmail_token.json"


def main():
    if not CREDENTIALS_FILE.exists():
        print(f"ERROR: {CREDENTIALS_FILE} not found.")
        print("Download it from Google Cloud Console:")
        print("  APIs & Services → Credentials → OAuth 2.0 Client → Download JSON")
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"Done! Token saved to {TOKEN_FILE}")
    print("You can now start the bot — Gmail source will work automatically.")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
