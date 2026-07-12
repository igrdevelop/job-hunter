"""
tools/telegram_user_login.py — One-time script to log into YOUR OWN Telegram
account (not the bot) and save a Telethon (MTProto) user session.

Why: linkedin_scout/telegram_relay.py needs to send a `/scoutfound <payload>`
command TO the bot, but Telegram never delivers a bot's own outgoing messages
back to itself as an incoming update — so the scout can't just message the
bot using the bot's own token. It has to send as a genuinely different
account (yours), which requires a real user login (phone number + code, and
your 2FA password if you have one set) via Telegram's user API.

Run this once (or if the session ever gets revoked):
  python tools/telegram_user_login.py

Prerequisites — get these from https://my.telegram.org (Log in -> API
development tools -> create an app if you don't have one yet):
  TELEGRAM_API_ID     (a number)
  TELEGRAM_API_HASH   (a hex string)
Also set:
  TELEGRAM_BOT_USERNAME   the bot's own @username (send target), e.g. @my_job_hunter_bot

Security note: the saved session file grants FULL access to your Telegram
account (read/send messages, etc.) to whoever holds it — treat it like a
password, same as .secrets/linkedin_storage_state.json. It stays local to
this machine; never commit it.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).parent.parent
load_dotenv(PROJECT_DIR / ".env")

SECRETS_DIR = PROJECT_DIR / ".secrets"
DEFAULT_SESSION_PATH = SECRETS_DIR / "telegram_user_session"

ENV_PATH = PROJECT_DIR / ".env"


def main():
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        print("ERROR: telethon not installed.")
        print("Run:  pip install telethon")
        sys.exit(1)

    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        print("ERROR: TELEGRAM_API_ID / TELEGRAM_API_HASH not set in .env.")
        print("Get them from https://my.telegram.org (API development tools),")
        print("then add to .env:")
        print("  TELEGRAM_API_ID=12345678")
        print("  TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789")
        sys.exit(1)

    SECRETS_DIR.mkdir(exist_ok=True)

    session_path = DEFAULT_SESSION_PATH
    env_val = os.environ.get("TELEGRAM_USER_SESSION", "").strip()
    if env_val:
        session_path = Path(env_val)
        session_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n[telegram_user_login] Session will be saved to: {session_path}")
    print("[telegram_user_login] You'll be asked for your phone number, then the")
    print("[telegram_user_login] login code Telegram sends you, and your 2FA")
    print("[telegram_user_login] password if you have one set.\n")

    with TelegramClient(str(session_path), int(api_id), api_hash) as client:
        client.start()  # interactive: phone -> code -> (2FA password)
        me = client.get_me()
        print(f"\n[telegram_user_login] Logged in as: {me.first_name} (@{me.username})")

    print(f"[telegram_user_login] Session saved to: {session_path}.session")

    if ENV_PATH.exists():
        env_text = ENV_PATH.read_text(encoding="utf-8")
        key = "TELEGRAM_USER_SESSION"
        if key not in env_text:
            with open(ENV_PATH, "a", encoding="utf-8") as f:
                f.write(
                    f"\n# Telegram user session for linkedin_scout/telegram_relay.py\n{key}={session_path}\n"
                )
            print(f"[telegram_user_login] Added {key}={session_path} to .env")
        else:
            print(f"[telegram_user_login] {key} already set in .env — no changes.")

    print("\nDone! Make sure TELEGRAM_BOT_USERNAME is also set in .env (the bot's")
    print("own @username), then linkedin_scout/run.py can relay candidates.")


if __name__ == "__main__":
    main()
