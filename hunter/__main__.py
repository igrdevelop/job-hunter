"""Allow `python -m hunter` as an alternative to `python hunter.py`."""
import logging
import sys

from hunter.config import PROJECT_DIR, LOG_FORMAT, validate_config
from hunter.logging_setup import setup_logging
from hunter.app import build_application


def main() -> None:
    setup_logging(log_dir=PROJECT_DIR / "logs", log_format=LOG_FORMAT)
    validate_config()

    run_now = "--now" in sys.argv
    app = build_application()

    if run_now:
        logging.getLogger("hunter").info("--now flag detected: will run hunt after startup")

        async def _post_init(application):
            from hunter.main import run_hunt
            await run_hunt(application)

        app.post_init = _post_init

    logging.getLogger("hunter").info("Job Hunter Bot started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
