"""Shared text/location helpers for job sources.

Two cross-cutting bits were re-implemented in nearly every JSON/RSS source:

1. HTML-fragment → plain text (``strip_html``) — used to turn a posting's HTML
   description into a short prefilter context string or the full LLM job text.
2. Remote-location normalization (``REMOTE_ANY`` + ``ensure_remote_token``) — the
   remote-only boards must emit a location string that still carries a "remote"
   token so it survives the central location whitelist in ``hunter.filters``
   (``_matches_location`` rejects a non-empty location lacking remote/wroclaw).

Each source keeps its own ``_format_location`` wrapper (input shapes differ:
list vs str vs two-arg) and delegates the shared core to ``ensure_remote_token``.
"""

from __future__ import annotations

import re
from html import unescape

# ``[^>]+`` already spans newlines, so DOTALL is a harmless no-op here; kept for
# parity with the per-source patterns this replaced.
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)

# Location tokens that mean "no geographic restriction" — collapsed to plain
# "Remote" by sources that want to drop the synonym rather than keep it as a hint.
REMOTE_ANY = frozenset({"anywhere", "worldwide", "global", "anywhere in the world", "remote"})


def strip_html(html: str | None, max_len: int) -> str:
    """Strip HTML tags from a fragment, collapse whitespace, and truncate.

    Returns "" for empty or non-string input. Entities are unescaped.
    """
    if not isinstance(html, str) or not html:
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", html))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def ensure_remote_token(base: str | None, geo: str | None = None) -> str:
    """Return a location string guaranteed to contain a "remote" token.

    - empty ``base`` → ``"Remote"``
    - ``base`` already containing "remote" (case-insensitive) → unchanged
    - otherwise → ``f"{base} (Remote)"``

    An optional ``geo`` hint (already-formatted, e.g. "Poland, Germany") is
    appended as ``f"{base} — {geo}"`` when non-empty.
    """
    base = (base or "").strip()
    if not base:
        base = "Remote"
    elif "remote" not in base.lower():
        base = f"{base} (Remote)"
    if geo:
        geo = geo.strip()
        if geo:
            return f"{base} — {geo}"
    return base
