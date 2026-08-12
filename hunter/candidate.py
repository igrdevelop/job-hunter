"""candidate.py — loader for candidate.yaml, the single source of truth for the
candidate's identity, location, languages and employer history.

candidate/candidate.yaml is gitignored (personal data). If it is absent,
load() returns {} and never raises — one warning is logged the first time.
Callers read through get(dotpath, default), and every such `default` is
NEUTRAL: empty, or an obviously-broken placeholder. It must never be the
project owner's real value, however convenient that is for reproducing his
behavior — that is what made a second person's checkout silently generate
CVs under his name. Identity is gated rather than defaulted; see
require_identity() below.

Multi-user (Phase B3): the cache is keyed by the resolved yaml path — one
process can serve several users' identities (the bot process reading
different users' filters, a future fan-out). The default resolution order is
unchanged: explicit argument > _set_path() test override > CANDIDATE_YAML_PATH
env (per-user apply subprocesses inject this) > repo-local candidate/.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "candidate" / "candidate.yaml"
_path_override: Path | None = None

# Neutral placeholders — deliberately NOT a working identity.
#
# These used to hold the project owner's real name, phone and email so that a
# checkout without candidate.yaml reproduced his behavior byte-for-byte. That
# is exactly the wrong failure mode for anyone else: a second person running
# this repo without a candidate.yaml silently generated CVs carrying the
# owner's name and phone number and mailed them to real employers. The
# placeholders below are obviously broken instead of quietly wrong, and
# ``require_identity()`` stops document generation before one can reach a PDF.
DEFAULT_FULL_NAME = "UNCONFIGURED CANDIDATE"
DEFAULT_CV_FILENAME_PREFIX = "Candidate_CV"
DEFAULT_AKA = ""
DEFAULT_HEADLINE = "Software Developer"
DEFAULT_CONTACT = "set identity.contact in candidate/candidate.yaml"

# Identity fields that MUST come from candidate.yaml before any document is
# rendered. `identity.aka` and `identity.headline` are excluded on purpose:
# aka is optional by design (blank omits the subtitle) and headline has a
# generic, non-personal default that is merely bland, not wrong.
REQUIRED_IDENTITY_FIELDS = (
    "identity.full_name",
    "identity.contact",
    "identity.cv_filename_prefix",
)


class CandidateIdentityMissing(RuntimeError):
    """Raised when a document would be rendered without a configured identity."""


def _set_path(path) -> None:
    """Test helper: point the loader at a different file and drop the cache."""
    global _path_override
    _path_override = Path(path) if path else None
    _load_file.cache_clear()


def _resolve_path() -> Path:
    if _path_override is not None:
        return _path_override
    env_path = os.environ.get("CANDIDATE_YAML_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_PATH


@lru_cache(maxsize=32)
def _load_file(path: Path) -> dict:
    """Read one candidate.yaml, cached per path (missing-file warning fires
    once per path for the cache's lifetime)."""
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


def load(path: str | Path | None = None) -> dict:
    """Load and cache candidate.yaml as a dict. Returns {} if the file is
    absent — callers must supply a default via get() for every field.

    `path` selects a specific user's yaml explicitly (multi-user callers);
    omitted, the process-default resolution applies (env override / repo file).
    """
    resolved = Path(path) if path is not None else _resolve_path()
    return _load_file(resolved)


def get(dotpath: str, default=None, *, path: str | Path | None = None):
    """Read a nested key with dot notation, e.g. get("identity.full_name")."""
    node = load(path)
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


def missing_identity_fields(*, path: str | Path | None = None) -> list[str]:
    """Which REQUIRED_IDENTITY_FIELDS candidate.yaml does not supply.

    Empty list = safe to render documents. A blank/whitespace-only value
    counts as missing: an empty name on a CV is no better than an absent one.
    """
    missing = []
    for dotpath in REQUIRED_IDENTITY_FIELDS:
        value = get(dotpath, None, path=path)
        if value is None or not str(value).strip():
            missing.append(dotpath)
    return missing


def require_identity(*, path: str | Path | None = None) -> None:
    """Abort before rendering a document with an unconfigured identity.

    Called at the top of generate_docs.main(). An aborted generation is a
    normal retryable failure; a PDF carrying someone else's name is not
    recoverable once it has been sent, which is why this raises instead of
    warning.
    """
    missing = missing_identity_fields(path=path)
    if not missing:
        return
    resolved = _resolve_path() if path is None else Path(path)
    raise CandidateIdentityMissing(
        "candidate identity is not configured — refusing to generate documents.\n"
        f"  Missing: {', '.join(missing)}\n"
        f"  Expected in: {resolved}\n"
        "  Fix: copy candidate/candidate.yaml.example to candidate/candidate.yaml "
        "and fill in your own name, contact line and CV filename prefix "
        "(see docs/SETUP_NEW_USER.md)."
    )
