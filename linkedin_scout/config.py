"""Standalone env config for the LinkedIn posts scout.

Phase 0 of docs/SCOUT_REPO_SPLIT_PLAN.md: the scout is a separate script (own
machine, own lifecycle) that is about to move into its own private repo, so it
must not import from `hunter` — the split will otherwise carry a live `hunter`
dependency into a package that no longer ships with it. Reads the same `.env`
at the repo root (matches `hunter/config.py`'s own `load_dotenv` call) so
nothing changes for the owner today; after the split this file's `.env` load
becomes the new repo's own root.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: int = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
