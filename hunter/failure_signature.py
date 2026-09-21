"""Stable signature for an apply-failure error text (docs/APPLY_FAILURE_QUEUES_PLAN.md).

Two failures caused by the same defect should get the same signature, even
when their raw text differs in every vacancy-specific detail (URL, UUID,
numbers, quoted fragments, file paths). The signature is what the M0 tool
(`tools/fail_signatures.py`) groups by, and what the M2 classifier will count
recurrences of. Both import this ONE function, so the calibration measured in
M0 is exactly what M2 runs on.

Pure, deterministic, stdlib only, never raises.

Line choice: `logs/apply_failures.jsonl`'s `error` field is sometimes the
head of apply_agent's STDOUT (the worker path falls back to stdout when
stderr is empty), so the first line is often just "[apply_agent] Step 1 ...".
The signature therefore uses a line carrying an error keyword (the most
frequent one, ties going to the last; see `signature()`). A text
with no such line gets `UNINFORMATIVE`; M0 reports how common that is,
because it limits what the log can tell us.
"""

from __future__ import annotations

import hashlib
import re

UNINFORMATIVE = "(no error line in logged text)"
EMPTY = "(empty error text)"

_MAX_SIGNATURE_CHARS = 160

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
# A quoted fragment may span newlines: the 2026-09-10 incident's stderr quoted
# a UUID + "\n\nJob" as one "deny rule". Length-capped so a stray unbalanced
# quote can't swallow the rest of the text.
_QUOTED_RE = re.compile(r"\"[^\"]{0,300}\"|'[^'\n]{0,300}'")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+){2,}[\\/]?")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SPACE_RE = re.compile(r"[ \t]+")

_ERROR_LINE_RE = re.compile(
    # `\w+(?:error|exception)` covers CamelCase names (ModuleNotFoundError),
    # which a plain `\berror\b` never matches.
    r"\b(\w*(?:error|exception)s?|traceback|fail(?:ed|ure|s)?|denied|deny|permission"
    r"|not found|no such|invalid|refused|timed out|timeout|unauthori[sz]ed|forbidden"
    r"|abort(?:ed|ing)?|cannot|can't|unable|unrecognized|expired|killed)\b",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Replace vacancy-specific details with placeholders, line by line."""
    out = _URL_RE.sub("<url>", text)
    out = _QUOTED_RE.sub('"<q>"', out)
    out = _UUID_RE.sub("<id>", out)
    out = _HEX_RE.sub("<id>", out)
    out = _PATH_RE.sub("<path>", out)
    out = _NUM_RE.sub("<n>", out)
    lines = [_SPACE_RE.sub(" ", line).strip() for line in out.splitlines()]
    return "\n".join(line for line in lines if line)


def signature(text: str | None) -> str:
    """One-line signature of an error text; see the module docstring."""
    if not text or not text.strip():
        return EMPTY
    lines = normalize_text(text).splitlines()
    error_lines = [line for line in lines if _ERROR_LINE_RE.search(line)]
    if not error_lines:
        return UNINFORMATIVE
    # Most frequent error line wins, ties go to the LAST one. Frequency first
    # because the logged text is truncated at a fixed length (head or tail,
    # depending on the caller), so its first or last line is often a partial
    # copy of a line repeated above. Picking the last line blindly would give
    # one defect a different signature per vacancy.
    counts: dict[str, int] = {}
    last_pos: dict[str, int] = {}
    for pos, line in enumerate(error_lines):
        counts[line] = counts.get(line, 0) + 1
        last_pos[line] = pos
    best = max(counts, key=lambda line: (counts[line], last_pos[line]))
    return best[:_MAX_SIGNATURE_CHARS]


def signature_id(sig: str) -> str:
    """Short stable id for a signature, for referencing it in reports/commands."""
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — not security
