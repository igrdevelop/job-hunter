#!/usr/bin/env python3
"""
hunter/__main__.py — Package entry point.

Supports all three invocation styles:
  python hunter.py          (legacy, via root shim)
  python -m hunter          (package mode)
  hunter                    (CLI script after pip install)
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PROJECT_DIR
from hunter.telegram_bot import build_application
from hunter.db import init_db, TRACKER_DB_PATH

logger = logging.getLogger("hunter")


def _setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "hunter_errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    logging.root.setLevel(logging.DEBUG)
    logging.root.addHandler(console)
    logging.root.addHandler(file_handler)


def _check_config() -> bool:
    ok = True
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env")
        ok = False
    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID is not set in .env")
        ok = False
    return ok


def main() -> None:
    _setup_logging()

    if not _check_config():
        sys.exit(1)

    init_db(TRACKER_DB_PATH)

    run_now = "--now" in sys.argv

    app = build_application()

    if run_now:
        logger.info("--now flag detected: will run hunt after startup")

        async def _post_init(application):
            from hunter.main import run_hunt

            await run_hunt(application)

        app.post_init = _post_init

    logger.info("🤖 Job Hunter Bot started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
