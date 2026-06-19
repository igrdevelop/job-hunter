"""Post-generation ATS roundtrip: re-score the *rendered* PDF against the job.

What our existing ats_checker measures
--------------------------------------
ats_checker.check(job_text, resume_text) scores the LLM-generated JSON
(content["resume_en"], flattened). That's what the LLM produced — not what
the recruiter's ATS actually parses out of our PDF.

Why that matters
----------------
Real ATS pipelines (Workday, Taleo, iCIMS, Greenhouse, Lever) extract text
from the PDF binary first, then run keyword / NER / matching. Anything our
generate_docs.py pipeline (python-docx → LibreOffice → PDF) drops or rewords
during rendering — line-break hyphenation that splits "performance-\\noptim",
Unicode bullets that get eaten, two-column tables, headers/footers — is
invisible to the JSON-side score but very visible to the recruiter's ATS.

What this module does
---------------------
Right after generate_docs, extract text from the rendered EN CV PDF and run
the same ats_checker against it. Heuristic-only (no LLM review) — keyword
match + TF-IDF, cheap and offline. Returns a dict with the PDF score and
delta vs the JSON score. The caller stores it in content.json + surfaces it
in the Telegram notification.

Best-effort throughout: any failure logs and returns None — the apply
pipeline must never block delivery because the roundtrip couldn't read a PDF.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from hunter import ats_checker
from hunter.pdf_text import extract_pdf_text

logger = logging.getLogger(__name__)

# Warn in the Telegram message when the PDF score is this many percentage
# points below the JSON score. 5pp catches real rendering damage without
# screaming at every "performance optimization" split.
WARN_DELTA_PP = 5.0


def find_en_cv_pdf(folder: Path) -> Optional[Path]:
    """Locate the English CV PDF in an apply folder."""
    for pat in ("*CV*EN*.pdf", "*Resume*EN*.pdf", "*_EN.pdf"):
        for p in folder.glob(pat):
            if "cover" in p.name.lower():
                continue
            return p
    # Fallback: first non-cover, non-PL PDF
    for p in folder.glob("*.pdf"):
        n = p.name.lower()
        if "cover" not in n and "_pl" not in n:
            return p
    return None


def run_pdf_roundtrip(
    folder: Path,
    job_text: str,
    json_ats_score: Optional[float] = None,
) -> Optional[dict]:
    """Score the rendered EN CV PDF in `folder` against `job_text`.

    Returns a dict with keys mirroring ats_checker.ATSResult.to_dict() plus
    `delta_from_json` (PDF score − JSON score, in percentage points).
    Returns None if the PDF can't be located or read — the apply pipeline
    treats that as "no signal" and never blocks delivery on it.
    """
    if not job_text.strip():
        logger.info("[ats_pdf] empty job_text — skipping roundtrip")
        return None

    pdf_path = find_en_cv_pdf(folder)
    if pdf_path is None:
        logger.info("[ats_pdf] no EN CV PDF found in %s — skipping roundtrip", folder)
        return None

    pdf_text = extract_pdf_text(pdf_path)
    if not pdf_text.strip():
        logger.info("[ats_pdf] PDF text extraction empty for %s — skipping", pdf_path.name)
        return None

    # Heuristic-only — no LLM. The roundtrip's job is to measure what *we*
    # lost in rendering; an LLM second-opinion would just re-judge the same
    # JSON content and burn tokens.
    result = ats_checker.check(
        job_text=job_text,
        resume_text=pdf_text,
        run_llm_review=False,
    )
    out = result.to_dict()
    out["pdf_file"] = pdf_path.name
    out["pdf_text_chars"] = len(pdf_text)
    if json_ats_score is not None:
        out["delta_from_json"] = round(out["score"] - float(json_ats_score), 1)
    return out


def format_summary(pdf_check: dict) -> str:
    """One-line summary for the Telegram notification."""
    score = pdf_check.get("score", "?")
    delta = pdf_check.get("delta_from_json")
    if delta is None:
        return f"PDF ATS: {score}%"
    sign = "+" if delta >= 0 else ""
    flag = " ⚠️" if delta <= -WARN_DELTA_PP else ""
    return f"PDF ATS: {score}% ({sign}{delta} vs JSON){flag}"
