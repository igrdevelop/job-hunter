"""
hunter/pipeline/folders.py — output-folder path helpers for the apply
pipeline. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.

``compute_output_folder()`` deliberately re-reads APPLICATIONS_DIR from
``hunter.apply_shared`` (not from hunter.config) at call time: that module
remains the attribute tests monkeypatch, and a plain module-level import
here would silently stop observing that patch once this function moved out
of apply_shared.py.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from hunter.config import PROJECT_DIR

PROMPTS_DIR = PROJECT_DIR / "prompts"
_cyz = os.getenv("CANDIDATE_YAML_PATH")
CANDIDATE_DIR = Path(_cyz).parent if _cyz else PROJECT_DIR / "candidate"


def compute_output_folder(company_name: str) -> Path:
    """Compute Applications/{date}/{Company} with _2, _3 suffixes if needed."""
    from hunter.apply_shared import APPLICATIONS_DIR

    today = date.today().strftime("%Y-%m-%d")
    date_dir = APPLICATIONS_DIR / today
    base = date_dir / company_name
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = date_dir / f"{company_name}_{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_folder_company(name: str) -> str:
    """Safe folder segment from company name (Windows / macOS)."""
    s = _INVALID_FOLDER_CHARS.sub("_", (name or "").strip())
    s = s.strip("._ ")[:120] or "Unknown"
    return s
