"""
tools/linkedin_login.py — One-time script to log into LinkedIn and save session.

Run this once (or when session expires):
  python tools/linkedin_login.py

Opens a VISIBLE browser window so you can log in manually (including 2FA).
After you see your LinkedIn feed and press Enter in terminal,
the script saves the session to .secrets/linkedin_storage_state.json.

Set LINKEDIN_STORAGE_STATE in .env to this path to use it in job_fetch.
"""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
SECRETS_DIR = PROJECT_DIR / ".secrets"
DEFAULT_STATE_PATH = SECRETS_DIR / "linkedin_storage_state.json"

ENV_PATH = PROJECT_DIR / ".env"


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed.")
        print("Run:  pip install playwright && playwright install chromium")
        sys.exit(1)

    SECRETS_DIR.mkdir(exist_ok=True)

    state_path = DEFAULT_STATE_PATH
    env_val = os.environ.get("LINKEDIN_STORAGE_STATE", "").strip()
    if env_val:
        state_path = Path(env_val)
        state_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n[linkedin_login] Opening browser...")
    print(f"[linkedin_login] Session will be saved to: {state_path}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,  # visible so you can log in
            args=["--start-maximized"],
        )
        ctx = browser.new_context(no_viewport=True)
        page = ctx.new_page()

        page.goto("https://www.linkedin.com/login")

        print("=" * 60)
        print("Browser opened. Please log in to LinkedIn manually.")
        print("After you are on your feed (or any page after login),")
        print("come back here and press Enter to save the session.")
        print("=" * 60)
        input("\nPress Enter when logged in > ")

        current = page.url
        if "linkedin.com/login" in current or "linkedin.com/checkpoint" in current:
            print("\nWARNING: Still on login page — make sure you are fully logged in.")
            input("Press Enter again when ready, or Ctrl+C to abort > ")

        ctx.storage_state(path=str(state_path))
        browser.close()

    print(f"\n[linkedin_login] Session saved to: {state_path}")

    # Suggest adding to .env if not already there
    if ENV_PATH.exists():
        env_text = ENV_PATH.read_text(encoding="utf-8")
        key = "LINKEDIN_STORAGE_STATE"
        if key not in env_text:
            with open(ENV_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n# LinkedIn session for job_fetch\n{key}={state_path}\n")
            print(f"[linkedin_login] Added {key}={state_path} to .env")
        else:
            print(f"[linkedin_login] {key} already set in .env — no changes.")

    print("\nDone! Run your hunter or apply_agent — LinkedIn fetch should work now.")


if __name__ == "__main__":
    main()
