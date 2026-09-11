#!/usr/bin/env python3
"""
hunter/__main__.py — Package entry point.

Supports all three invocation styles:
  python hunter.py          (legacy, via root shim)
  python -m hunter          (package mode)
  hunter                    (CLI script after pip install)
"""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PROJECT_DIR
from hunter.telegram_bot import build_application
from hunter.db import init_db, TRACKER_DB_PATH

logger = logging.getLogger("hunter")

# `bot<id>:<token>` as it appears inside a Telegram API URL. python-telegram-bot
# talks to api.telegram.org/bot<TOKEN>/<method>, and httpx logs the full URL at
# INFO — so every getUpdates poll (one every ~10 s) used to write the live bot
# token into logs/hunter_errors.log, which `scheduled_gdrive_upload_logs` then
# uploaded to Drive daily. Two defences below: httpx is silenced to WARNING
# (those lines are pure noise anyway), and this filter scrubs the pattern out of
# whatever any other logger might print.
_TOKEN_RE = re.compile(r"(bot\d{5,}:)[A-Za-z0-9_-]{20,}")
_REDACTED = "\\1<redacted>"  # keeps the bot<id>: prefix, drops the secret


def _scrub(value: object) -> object:
    return _TOKEN_RE.sub(_REDACTED, value) if isinstance(value, str) else value


class RedactBotToken(logging.Filter):
    """Strip a Telegram bot token out of a record's message and its args.

    A filter rather than a formatter tweak: httpx passes the URL through
    `record.args`, so scrubbing only `record.msg` would miss it entirely.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub(v) for k, v in record.args.items()}
        return True


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

    redact = RedactBotToken()
    console.addFilter(redact)
    file_handler.addFilter(redact)

    logging.root.setLevel(logging.DEBUG)
    logging.root.addHandler(console)
    logging.root.addHandler(file_handler)

    # httpx logs one INFO line per request with the full URL. For the Telegram
    # long-poll that is a line every ~10 s carrying the bot token; for the
    # scrapers it is noise. WARNING keeps the failures and drops the rest.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
