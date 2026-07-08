"""Parse LinkedIn content-search `document.body.innerText` into post blocks.

Pure string logic — no Playwright import, no browser required (M1). Shape is
per docs/LINKEDIN_POSTS_SOURCE_PLAN.md §4.6 round 2 live-probe finding:

  - Posts are separated by "Feed post" marker lines.
  - The author name is the next non-empty line after the marker.
  - Header noise ("• 3rd+ …", a title/subtitle line, timestamp, "• Follow" /
    "• Connect" button text) sits between the author line and the post body;
    drop everything up to and including the Follow/Connect line.
  - The body runs until the next "Feed post" marker (or end of text).

Real post permalinks (owner discovery 2026-07-08, re-verified live against a
current session — an earlier probe had found none reachable, which is no
longer accurate): SOME posts (not all — appears tied to how LinkedIn renders
that particular post, e.g. shares) wrap their body text in a real, working
`<a href="https://www.linkedin.com/feed/update/urn:li:share:...">` — no extra
click needed, it's already in the DOM. `browser._EXTRACT_JS` emits a
`LI_PERMALINK::<url>` marker line right where that anchor sits in the
document-order text stream; this parser extracts it into `ParsedPost.
permalink` (best-effort, `None` when the post has no such link) and strips
the marker line out of the body text.
"""

from __future__ import annotations

from dataclasses import dataclass

_FEED_POST_MARKER = "feed post"
_HEADER_END_MARKERS = ("follow", "connect")
# Header noise is short (author subtitle + timestamp lines) — bail out instead
# of silently eating the whole body if no Follow/Connect line ever appears.
_MAX_HEADER_LINES = 10

_PERMALINK_MARKER_PREFIX = "LI_PERMALINK::"


@dataclass
class ParsedPost:
    author: str
    body: str
    permalink: str | None = None


def _split_lines(inner_text: str) -> list[str]:
    return inner_text.replace("\r\n", "\n").split("\n")


def _find_markers(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if line.strip().lower() == _FEED_POST_MARKER]


def _next_non_empty(lines: list[str], start: int, end: int) -> tuple[str, int] | None:
    """First non-blank line in lines[start:end]; returns (text, index) or None."""
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped:
            return stripped, i
    return None


def parse_posts(inner_text: str) -> list[ParsedPost]:
    """Split a captured innerText blob into (author, body) post blocks.

    Skips any "Feed post" block where an author line can't be found (malformed
    capture) rather than raising — the caller runs on live scraped text.
    """
    if not inner_text:
        return []
    lines = _split_lines(inner_text)
    markers = _find_markers(lines)
    if not markers:
        return []

    posts: list[ParsedPost] = []
    for idx, marker_pos in enumerate(markers):
        block_end = markers[idx + 1] if idx + 1 < len(markers) else len(lines)
        author_match = _next_non_empty(lines, marker_pos + 1, block_end)
        if author_match is None:
            continue
        author, author_idx = author_match

        # Look for the Follow/Connect header-end line within a short window
        # after the author line; drop everything up to and including it.
        header_scan_end = min(author_idx + 1 + _MAX_HEADER_LINES, block_end)
        body_start = author_idx + 1
        for i in range(author_idx + 1, header_scan_end):
            if lines[i].strip().lower() in _HEADER_END_MARKERS:
                body_start = i + 1
                break

        body_lines = [ln for ln in lines[body_start:block_end]]

        # Pull out any LI_PERMALINK:: marker line(s) — first one wins, and the
        # marker itself is never part of the actual post text.
        permalink: str | None = None
        filtered_body_lines: list[str] = []
        for ln in body_lines:
            stripped = ln.strip()
            if stripped.startswith(_PERMALINK_MARKER_PREFIX):
                if permalink is None:
                    permalink = stripped[len(_PERMALINK_MARKER_PREFIX):].strip()
                continue
            filtered_body_lines.append(ln)
        body_lines = filtered_body_lines

        # Trim leading/trailing blank lines but keep internal structure.
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        posts.append(ParsedPost(author=author, body=body, permalink=permalink))
    return posts
