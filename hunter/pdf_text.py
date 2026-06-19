"""Best-effort PDF → plain-text extraction.

Used by ats_pdf_roundtrip to score the *rendered* PDF (not just the JSON we
fed to generate_docs) against the job posting — so we measure what a real
ATS parser would see, not what the LLM produced.

pypdf was chosen over pdfminer.six because it's already pure-python with no
native deps and survives most LibreOffice-generated PDFs fine. If a stronger
extractor is ever needed, swap this single module — callers depend only on
extract_pdf_text(path) returning a string (empty on any failure).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("[pdf_text] pypdf not installed — PDF roundtrip disabled")
        return ""

    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        logger.warning("[pdf_text] failed to read %s: %s", pdf_path.name, e)
        return ""
