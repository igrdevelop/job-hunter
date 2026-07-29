"""Loader for the user's job-matching filter rules (FILTER dict).

The actual FILTER dict lives in `filter_config.py` at the repo root — a
per-user, gitignored file (see `filter_config.example.py` for the template
and setup instructions). This module just locates and loads it, so every
existing `from hunter.config import FILTER` / `from hunter.filter_config
import FILTER` import keeps working unchanged regardless of who's running
the bot.

Moved here from a hardcoded dict (2026-07-29, docs/SHARE_FILTER_CONFIG_PLAN.md)
so a second person can clone the repo, copy the `.example` template, and run
the bot with their own search criteria without editing tracked code.
"""

import importlib.util
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "filter_config.py"

if not _CONFIG_PATH.exists():
    print(
        "\n[FATAL] filter_config.py not found.\n"
        "  Copy the template:  cp filter_config.example.py filter_config.py\n"
        "  Then edit it to match your job search criteria.\n"
        "  See prompts/README.md for details.\n"
    )
    sys.exit(1)

# Load FILTER dict from the user's config file at the repo root.
_spec = importlib.util.spec_from_file_location("_user_filter_config", _CONFIG_PATH)
if _spec is None or _spec.loader is None:
    print(f"\n[FATAL] Could not load filter_config.py from {_CONFIG_PATH}\n")
    sys.exit(1)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
FILTER = _mod.FILTER
