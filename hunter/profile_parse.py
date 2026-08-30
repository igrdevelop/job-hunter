"""hunter/profile_parse.py — resume text extraction into a structured Profile
(hunter/profile_schema.py).

docs/RESUME_PROFILE_STORE_PLAN.md M3. Two layers:

  1. `extract_resume_text(path)` — read raw text out of an uploaded .docx /
     .pdf / .txt / .md file. This step CAN fail (a corrupt file, an unknown
     extension) and raises `ProfileParseError` when it does — there is no
     text to hand the rest of the pipeline.
  2. `parse_resume_text(text, llm=...)` (added in a later step) — turn that
     text into a Profile. That step must NEVER hard-fail: a resume upload is
     an onboarding event, and a parse failure there must degrade to "put it
     all in leftovers", not break the flow.
"""

from __future__ import annotations

from pathlib import Path

from hunter import contact_extract, profile_schema

_TEXT_EXTENSIONS = {".txt", ".md"}


class ProfileParseError(RuntimeError):
    """Raised when a resume file cannot be turned into text at all — unknown
    extension, corrupt/unreadable file, or a file with no extractable text."""


def _extract_docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text.strip())
    return "\n".join(parts)


def _extract_pdf_text(path: Path) -> str:
    # Reuse the same extractor hunter/ats_pdf_roundtrip.py uses to read a
    # rendered CV PDF back — one PDF->text implementation for the whole repo.
    from hunter.pdf_text import extract_pdf_text

    return extract_pdf_text(path)


def extract_resume_text(path: Path) -> str:
    """Extract plain text from an uploaded resume file.

    Supports `.docx` (python-docx: paragraphs + table cells), `.pdf`
    (`hunter.pdf_text.extract_pdf_text`), and `.txt`/`.md` (read as-is).
    Any other extension, an unreadable file, or a file with no extractable
    text raises `ProfileParseError` with a message safe to show the user who
    uploaded it.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    try:
        if suffix == ".docx":
            text = _extract_docx_text(path)
        elif suffix == ".pdf":
            text = _extract_pdf_text(path)
        elif suffix in _TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            raise ProfileParseError(f"Unsupported resume file type: {suffix or '(none)'}")
    except ProfileParseError:
        raise
    except Exception as e:
        raise ProfileParseError(f"Could not read {path.name}: {e}") from e

    if not text.strip():
        raise ProfileParseError(f"No extractable text in {path.name}")
    return text


# ── Text -> Profile ─────────────────────────────────────────────────────────


def _contact_line(text: str) -> str:
    """Best-effort "phone | email" line, deterministic, $0.

    `contact_extract.extract_contacts` was built for finding a RECRUITER's
    contact inside a job posting, but its email/phone regexes are generic —
    over a resume's own header (no "recruiter"/"kontakt" label to match) it
    still finds the candidate's own email and, if present, attaches the
    first phone number to it. Good enough for a pre-fill the user reviews
    on the confirmation screen; the candidate's own name is deliberately
    NOT guessed from this (see parse_resume_text's docstring)."""
    contacts = contact_extract.extract_contacts(text)
    if not contacts:
        return ""
    parts = [p for p in (contacts[0].phone, contacts[0].email) if p]
    return " | ".join(parts)


def parse_resume_text(
    text: str,
    llm: object | None = None,
    *,
    source_upload_id: str = "",
) -> profile_schema.Profile:
    """Turn resume text into a structured Profile.

    Never hard-fails: any LLM call this eventually makes (a later step) is
    wrapped so a failure degrades to the same fallback used here — the whole
    input text as one leftover, plus a deterministic email/phone pre-fill.
    The candidate's own NAME is never guessed from free text; it is either
    supplied by an LLM parse the user confirms, or left for the user to type.
    """
    text = (text or "").strip()
    return _fallback_profile(text, source_upload_id=source_upload_id)


def _fallback_profile(text: str, *, source_upload_id: str = "") -> profile_schema.Profile:
    """The parse-never-fails branch: no structure extracted, just a contact
    pre-fill and the raw text preserved as a single leftover for the user to
    reassign by hand."""
    profile = profile_schema.Profile()
    if not text:
        return profile
    contact = _contact_line(text)
    if contact:
        profile.core.identity.contact = contact
    profile.leftovers = [profile_schema.Leftover(text=text, source_upload_id=source_upload_id)]
    return profile
