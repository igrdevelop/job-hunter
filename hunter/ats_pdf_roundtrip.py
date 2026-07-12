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
import re
from html import escape as html_escape
from pathlib import Path
from typing import Optional

from hunter import ats_checker
from hunter.pdf_text import extract_pdf_text

logger = logging.getLogger(__name__)

# Trigger the NBSP self-heal pass when the PDF score is this many percentage
# points below the JSON score. 5pp catches real rendering damage without
# triggering on every "performance optimization" split.
HEAL_DELTA_PP = 5.0

# A space-separating character that LibreOffice cannot break a line at.
# Used by nbsp_patch_missing_keywords to keep multi-word ATS keywords
# (e.g. "performance optimization") together in the rendered PDF.
NBSP = " "


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


def _en_cv_pdf_text(folder: Path, log_prefix: str) -> tuple[Optional[Path], str]:
    """Locate the EN CV PDF in `folder` and extract its text.

    Shared by run_pdf_roundtrip and run_llm_verdict so the locate+extract
    logic can't diverge. Returns (pdf_path, text); pdf_path is None when no
    PDF was found, text is "" when extraction produced nothing (both cases
    are logged with the caller's prefix).
    """
    pdf_path = find_en_cv_pdf(folder)
    if pdf_path is None:
        logger.info("[%s] no EN CV PDF found in %s — skipping", log_prefix, folder)
        return None, ""
    pdf_text = extract_pdf_text(pdf_path)
    if not pdf_text.strip():
        logger.info("[%s] PDF text extraction empty for %s — skipping", log_prefix, pdf_path.name)
        return pdf_path, ""
    return pdf_path, pdf_text


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

    pdf_path, pdf_text = _en_cv_pdf_text(folder, "ats_pdf")
    if pdf_path is None or not pdf_text.strip():
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


def run_llm_verdict(folder: Path, job_text: str) -> Optional[dict]:
    """Final independent ATS verdict: one cheap-LLM call over the rendered PDF.

    Uses the judge configuration (JUDGE_MODEL / JUDGE_PROVIDER / JUDGE_API_KEY —
    a cheap model that did NOT write the resume) to score the text extracted
    from the delivered EN CV PDF against the job posting. This is the only LLM
    scoring pass in the pipeline: the rewrite loop is purely deterministic.

    Informational only — returns None on any failure (disabled, no key, no
    PDF, empty extraction, LLM error) and must never block delivery.
    """
    from hunter import config

    if not getattr(config, "ATS_VERDICT_ENABLED", True):
        return None
    if not job_text.strip():
        return None
    if not config.JUDGE_API_KEY:
        logger.info("[ats_verdict] no judge API key — skipping verdict")
        return None

    pdf_path, pdf_text = _en_cv_pdf_text(folder, "ats_verdict")
    if pdf_path is None or not pdf_text.strip():
        return None

    verdict = ats_checker.llm_verdict(
        job_text=job_text,
        resume_text=pdf_text,
        provider=config.JUDGE_PROVIDER,
        model=config.JUDGE_MODEL,
        api_key=config.JUDGE_API_KEY,
    )
    if verdict is not None:
        verdict["pdf_file"] = pdf_path.name
    return verdict


def format_verdict(verdict: dict) -> str:
    """Telegram summary for the independent PDF verdict: the score plus the
    judge's own gap_report (trimmed), so the owner sees WHY the number isn't
    higher — not just the number. HTML-escaped because the bot sends
    notifications with parse_mode=HTML."""
    score = verdict.get("score", "?")
    text = f"ATS verdict (independent, PDF): {score}%"
    gap = format_gap_report(verdict)
    if gap:
        text += f"\n{gap}"
    return text


def format_gap_report(verdict: dict, max_chars: int = 350) -> str:
    """The verdict's gap_report as one trimmed, HTML-escaped Telegram line
    (empty string when the verdict carries none)."""
    gap = str(verdict.get("gap_report") or "").strip()
    if not gap:
        return ""
    if len(gap) > max_chars:
        gap = gap[: max_chars - 1].rstrip() + "…"
    return f"📋 <i>{html_escape(gap)}</i>"


def format_summary(pdf_check: dict) -> str:
    """One-line summary for the Telegram notification.

    No warn flag — by the time this renders we've either self-healed any
    significant Δ (apply_api / apply_cli run NBSP patch + regen) or
    accepted the residual. Bothering the user with a number they can't
    act on adds noise, not signal.
    """
    score = pdf_check.get("score", "?")
    delta = pdf_check.get("delta_from_json")
    if delta is None:
        return f"PDF ATS: {score}%"
    sign = "+" if delta >= 0 else ""
    return f"PDF ATS: {score}% ({sign}{delta} vs JSON)"


def nbsp_patch_missing_keywords(content: dict, missing_keywords: list[str]) -> int:
    """Replace internal whitespace with NBSP for multi-word keywords missing in PDF.

    Mutates `content["resume_en"]` in place. Returns the count of patches
    applied (so the caller can decide whether a regen is worth it). Only
    multi-word keywords (≥ 1 internal whitespace) are touched — splitting at
    a wrap is the documented cause of these PDF-side losses, and NBSP is the
    cheapest fix that doesn't alter visible spacing or wording.

    Single-word missing keywords (e.g. "express", "jasmine") are NOT patched:
    if they were truly absent from the JSON resume they'd be missing from
    every render, and the answer is a content rewrite (handled by the
    earlier _ats_check_loop), not a render-layer trick.
    """
    if not missing_keywords:
        return 0

    resume = content.get("resume_en")
    if not isinstance(resume, dict):
        return 0

    # Build list of (multi_word_kw, nbsp_version) to substitute throughout.
    subs: list[tuple[str, str]] = []
    for kw in missing_keywords:
        if not isinstance(kw, str):
            continue
        stripped = kw.strip()
        if not stripped or " " not in stripped:
            continue
        nbsp_version = re.sub(r"\s+", NBSP, stripped)
        subs.append((stripped, nbsp_version))
    if not subs:
        return 0

    patches = 0

    def walk(node):
        nonlocal patches
        if isinstance(node, dict):
            for k, v in list(node.items()):
                node[k] = walk(v)
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, str):
            new_s = node
            for kw, nbsp_kw in subs:
                # Case-insensitive replacement, preserving the original
                # casing in the resume — the ATS regex is case-insensitive
                # anyway and we don't want to flip "Performance Optimization"
                # to lowercase mid-sentence.
                pattern = re.compile(re.escape(kw), re.IGNORECASE)
                if pattern.search(new_s):

                    def _repl(m, nbsp_kw=nbsp_kw):
                        return re.sub(r"\s+", NBSP, m.group(0))

                    new_s_after = pattern.sub(_repl, new_s)
                    if new_s_after != new_s:
                        patches += 1
                        new_s = new_s_after
            return new_s
        return node

    content["resume_en"] = walk(resume)
    return patches
