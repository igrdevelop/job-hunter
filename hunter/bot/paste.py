"""
bot/paste.py — Paste-mode detection helpers.

_looks_like_paste(text) → True when the message looks like a pasted job posting.
_extract_url(text)       → first http(s) URL in text, or ''.

Note: _handle_paste() lives in bot/apply_runner.py (depends on apply_runner logic).
"""

import re

# Any message longer than this is treated as a pasted job posting (if it's not a bare URL).
# 200 chars catches compact JD summaries (~250 chars) without reacting to casual chat.
_PASTE_TEXT_MIN_LEN = 200

_URL_RE = re.compile(r"https?://\S+")


def _looks_like_paste(text: str) -> bool:
    """True when the user likely pasted a full job posting (with or without a URL)."""
    stripped = text.strip()
    if len(stripped) < _PASTE_TEXT_MIN_LEN:
        return False
    urls = _URL_RE.findall(stripped)
    if urls:
        non_url_len = len(_URL_RE.sub("", stripped).strip())
        return non_url_len >= _PASTE_TEXT_MIN_LEN
    return True


def _extract_url(text: str) -> str:
    """Return the first http(s) URL found in text, or ''."""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(").,;") if m else ""
