"""candidate.py — loader for candidate.yaml, the single source of truth for the
candidate's identity, location, languages and employer history.

candidate/candidate.yaml is the tracked config file. If it is absent,
load() degrades gracefully —
every caller reads through get(dotpath, default) with an explicit fallback
that reproduces the project owner's original hardcoded behavior, so a missing
file never crashes the bot. One warning is logged the first time it's missing.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "candidate" / "candidate.yaml"
_path_override: Path | None = None

# The project owner's original hardcoded identity, kept here (not inlined at
# each call site) so this personal data lives in exactly one place outside
# candidate.yaml itself. Callers that need the byte-for-byte original
# behavior when candidate.yaml is absent pass these as their default, e.g.
# ``candidate.get("identity.full_name", DEFAULT_FULL_NAME)``.
DEFAULT_FULL_NAME = "Ihar Petrasheuski"
DEFAULT_CV_FILENAME_PREFIX = "Ihar_Petrasheuski_CV"


def _set_path(path) -> None:
    """Test helper: point the loader at a different file and drop the cache."""
    global _path_override
    _path_override = Path(path) if path else None
    load.cache_clear()


def _resolve_path() -> Path:
    if _path_override is not None:
        return _path_override
    env_path = os.environ.get("CANDIDATE_YAML_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_PATH


@lru_cache(maxsize=1)
def load() -> dict:
    """Load and cache candidate.yaml as a dict. Returns {} if the file is
    absent — callers must supply a default via get() for every field."""
    path = _resolve_path()
    if not path.exists():
        logger.warning(
            "candidate.yaml not found at %s — using built-in defaults. "
            "Edit candidate/candidate.yaml to configure your own identity.",
            path,
        )
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def get(dotpath: str, default=None):
    """Read a nested key with dot notation, e.g. get("identity.full_name")."""
    node = load()
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default
