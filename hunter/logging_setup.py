"""
logging_setup.py — Configures root logger based on LOG_FORMAT env var.

Text format (default):
    2026-05-18 12:34:56 [INFO] hunter: message

JSON format (LOG_FORMAT=json — useful in Docker / log aggregators):
    {"asctime": "...", "name": "...", "levelname": "INFO", "message": "..."}
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path | None = None, log_format: str = "text") -> None:
    """Configure root logger with console + optional file handler.

    Args:
        log_dir:    Directory for rotating log file. None disables file logging.
        log_format: "text" (default) or "json".
    """
    fmt_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    if log_format == "json":
        try:
            from pythonjsonlogger import jsonlogger  # type: ignore[import]
            formatter: logging.Formatter = jsonlogger.JsonFormatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt=datefmt,
            )
        except ImportError:
            logging.warning(
                "python-json-logger not installed; falling back to text format. "
                "Run: pip install python-json-logger"
            )
            formatter = logging.Formatter(fmt_str, datefmt=datefmt)
    else:
        formatter = logging.Formatter(fmt_str, datefmt=datefmt)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    handlers: list[logging.Handler] = [console]

    if log_dir is not None:
        log_dir.mkdir(exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "hunter_errors.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.root
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
