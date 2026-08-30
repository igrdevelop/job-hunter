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
