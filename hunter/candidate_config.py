"""Loader for the user's candidate identity/employment-history config.

The actual values live in `candidate_config.py` at the repo root — a per-user,
gitignored file (see `candidate_config.example.py` for the template and setup
instructions). This module just locates and loads it, re-exporting every
constant so `from hunter.candidate_config import CANDIDATE_NAME` (etc.) keeps
working unchanged regardless of who's running the bot.

Added 2026-07-29 (docs/SHARE_FILTER_CONFIG_PLAN.md) so a second person can
clone the repo, copy the `.example` template, and run the bot with their own
identity/employment history without editing tracked code.
"""

import importlib.util
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "candidate_config.py"

if not _CONFIG_PATH.exists():
    print(
        "\n[FATAL] candidate_config.py not found.\n"
        "  Copy the template:  cp candidate_config.example.py candidate_config.py\n"
        "  Then edit it with your real name/contact/employment history.\n"
        "  See prompts/README.md for details.\n"
    )
    sys.exit(1)

_spec = importlib.util.spec_from_file_location("_user_candidate_config", _CONFIG_PATH)
if _spec is None or _spec.loader is None:
    print(f"\n[FATAL] Could not load candidate_config.py from {_CONFIG_PATH}\n")
    sys.exit(1)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CANDIDATE_NAME: str = _mod.CANDIDATE_NAME
CANDIDATE_SUBTITLE: str = _mod.CANDIDATE_SUBTITLE
CANDIDATE_HEADLINE: str = _mod.CANDIDATE_HEADLINE
CANDIDATE_CONTACT: str = _mod.CANDIDATE_CONTACT
CV_FILENAME_PREFIX: str = _mod.CV_FILENAME_PREFIX
EXPECTED_ROLE_COUNT: int = _mod.EXPECTED_ROLE_COUNT
REAL_COMPANIES: set = _mod.REAL_COMPANIES
REAL_COMPANIES_ORDERED: tuple = _mod.REAL_COMPANIES_ORDERED
PROFILE_TITLES: set = _mod.PROFILE_TITLES
PROTECTED_EMPLOYERS: tuple = _mod.PROTECTED_EMPLOYERS
FLEXIBLE_PROJECTS: tuple = _mod.FLEXIBLE_PROJECTS
EMPLOYER_TECH_TERMS: set = _mod.EMPLOYER_TECH_TERMS
