#!/usr/bin/env python3
"""
hunter.py — Entry point for the Job Hunter Bot.

Usage:
  python hunter.py          # start bot with scheduled hunts
  python hunter.py --now    # start bot AND run one hunt immediately
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PROJECT_DIR
from hunter.telegram_bot import build_application

# ── Console handler (INFO+) ───────────────────────────────────────────────────
_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)

# ── File handler (WARNING+ with full tracebacks) ──────────────────────────────
_log_dir = PROJECT_DIR / "logs"
_log_dir.mkdir(exist_ok=True)
_file_handler = RotatingFileHandler(
    _log_dir / "hunter_errors.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=10,              # keep 10 rotated files → up to 50 MB history
    encoding="utf-8",
)
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(_fmt)

logging.root.setLevel(logging.DEBUG)
logging.root.addHandler(_console)
logging.root.addHandler(_file_handler)

logger = logging.getLogger("hunter")


def _check_config() -> bool:
    ok = True
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env")
        ok = False
    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID is not set in .env")
        ok = False
    # ANTHROPIC_API_KEY not required — apply_agent uses claude CLI (Pro plan)
    return ok


def main() -> None:
    if not _check_config():
        sys.exit(1)

    run_now = "--now" in sys.argv

    app = build_application()

    if run_now:
        logger.info("--now flag detected: will run hunt after startup")

        async def _post_init(application):
            from hunter.main import run_hunt
            await run_hunt(application)  # application acts as context here

        app.post_init = _post_init

    logger.info("🤖 Job Hunter Bot started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
