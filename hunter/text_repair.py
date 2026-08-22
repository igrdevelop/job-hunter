"""Repair the seam left behind when a span is cut out of generated text.

Every content-safety stage in the apply pipeline works by REMOVING a span from
an otherwise finished sentence: the claim judge drops a fabricated clause
(`claim_judge._drop_quote`), the prestige scrub drops "Fortune 500" phrases,
the compliance scrub drops "DORA compliance" tails. Each one used to tidy the
result with its own set of global regexes over the whole field, which produced
two classes of defect that reached the rendered PDF (owner report 2026-08-21,
Avive Solutions application):

* ``re.sub(r"\\s{2,}", " ", text)`` — ``\\s`` matches ``\\n``, so cutting ONE
  clause out of a cover letter collapsed every paragraph break in the entire
  letter into a single space.
* The junction between the surviving left and right sides was only patched for
  a few hard-coded pairs, leaving ``", - from a real-time..."``,
  ``"...decisions; across a team of 10+."`` and bullets starting lowercase.

The helpers here fix the junction where the cut actually happened — the caller
knows both sides of it — instead of guessing at it with regexes over untouched
prose. That is both safer (an untouched semicolon elsewhere in the field is
never rewritten) and more complete.
"""

from __future__ import annotations

import re

# Connectors that only ever introduced the removed span; when the cut leaves one
# stranded at an edge it has to go with it.
CONNECTORS = (
    "and",
    "or",
    "including",
    "serving",
    "with",
    "as well as",
    "plus",
    "oraz",
    "i",
    "dla",
    "wraz z",
)

_LEADING_CONNECTOR_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(c) for c in CONNECTORS) + r")\b\s*",
    re.IGNORECASE,
)
_TRAILING_CONNECTOR_RE = re.compile(
    r"\s+(?:" + "|".join(re.escape(c) for c in CONNECTORS) + r")\s*$",
    re.IGNORECASE,
)

# Lowercase-by-convention tokens that must survive the re-capitalisation pass.
_KEEP_LOWER = frozenset({"npm", "nginx", "webpack", "eslint", "px", "ng"})

_DASHES = "-–—"
# Same three dashes for use INSIDE a character class. The hyphen must be escaped:
# interpolating the raw string above would make ".-–" a range covering every
# ASCII letter, so the "strip a leading separator" pass ate the first letter of
# the surviving text ("enforced" -> "nforced").
_SEPARATOR_CLASS = r"[,;:.\-–—]"


def collapse_spaces(text: str) -> str:
    """Collapse runs of horizontal whitespace, preserving line structure.

    ``[^\\S\\n]`` is "whitespace that is not a newline" — the whole point of
    this helper over a plain ``\\s{2,}`` collapse, which silently destroys the
    paragraph breaks of a cover letter.
    """
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    text = re.sub(r"[^\S\n]+\n", "\n", text)  # trailing spaces before a break
    return re.sub(r"\n[^\S\n]+", "\n", text)  # indentation after a break


def _capitalize_first(text: str, *, original: str) -> str:
    """Restore a leading capital the cut removed, if the original had one."""
    stripped = text.lstrip()
    if not stripped or not stripped[0].isalpha() or not stripped[0].islower():
        return text
    orig = original.lstrip()
    if not orig or not orig[0].isalpha() or not orig[0].isupper():
        return text
    first_word = re.match(r"[A-Za-z]+", stripped)
    if first_word and first_word.group(0).lower() in _KEEP_LOWER:
        return text
    pad = text[: len(text) - len(stripped)]
    return pad + stripped[0].upper() + stripped[1:]


def repair_junction(left: str, right: str) -> str:
    """Rejoin the two sides of a cut so the seam reads as written prose.

    ``left``/``right`` are the text before and after the removed span, exactly
    as they were in the source. Only the seam is touched — punctuation and
    casing elsewhere in either side are left alone.
    """
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return _LEADING_CONNECTOR_RE.sub("", re.sub(rf"^{_SEPARATOR_CLASS}\s*", "", right))
    if not right:
        left = _TRAILING_CONNECTOR_RE.sub("", left)
        return re.sub(r"[,;:]$", "", left)

    # A connector that only introduced the removed span is now stranded.
    left = _TRAILING_CONNECTOR_RE.sub("", left)

    head = right[0]
    if head in ",;:.!?":
        # The right side carries its own separator — ours is a duplicate.
        return re.sub(r"[,;:]$", "", left) + right
    if head in _DASHES:
        # ", - from a real-time..." — the dash is the separator, drop the comma.
        return re.sub(r"[,;:]$", "", left) + " " + right
    if head.islower() and left.endswith((";", ":")):
        # A semicolon/colon promises an independent clause; what survived the
        # cut is a fragment ("...decisions; across a team of 10+").
        return left[:-1] + " " + right
    if head.isalpha() and head.islower() and left.endswith((".", "!", "?")):
        # The cut removed a sentence opener; the survivor now starts a sentence.
        return left + " " + right[0].upper() + right[1:]
    return left + " " + right


def cut_span(text: str, start: int, end: int) -> str:
    """Remove ``text[start:end]`` and repair the seam and the outer edges."""
    repaired = repair_junction(text[:start], text[end:])
    repaired = collapse_spaces(repaired)
    repaired = _capitalize_first(repaired, original=text)
    return repaired.strip()


def drop_sentences(text: str, predicate) -> str:
    """Drop whole sentences matching ``predicate``, keeping paragraph breaks.

    The naive ``" ".join(re.split(r"(?<=[.!?])\\s+", text))`` used before merged
    every paragraph of a cover letter into one block even when the dropped
    sentence sat in a single paragraph.
    """
    out: list[str] = []
    for block in re.split(r"(\n\s*\n)", text):
        if block.strip() == "" or re.fullmatch(r"\n\s*\n", block):
            out.append(block)
            continue
        kept = [s for s in re.split(r"(?<=[.!?])\s+", block) if not predicate(s)]
        out.append(" ".join(kept))
    joined = "".join(out)
    # A paragraph emptied by the drop leaves a triple break behind.
    joined = re.sub(r"\n{3,}", "\n\n", joined.strip())
    return collapse_spaces(joined)
