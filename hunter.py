#!/usr/bin/env python3
"""
hunter.py — Entry point for the Job Hunter Bot.

Usage:
  python hunter.py          # start bot with scheduled hunts
  python hunter.py --now    # start bot AND run one hunt immediately
"""

import logging
import sys

from hunter.config import PROJECT_DIR, LOG_FORMAT, validate_config
from hunter.logging_setup import setup_logging
from hunter.app import build_application

setup_logging(log_dir=PROJECT_DIR / "logs", log_format=LOG_FORMAT)
logger = logging.getLogger("hunter")


def main() -> None:
    validate_config()

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
