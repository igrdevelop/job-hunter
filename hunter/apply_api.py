"""
hunter/apply_api.py — API pipeline for apply_agent.

Public entry point:
    main_api(url, paste_text="", *, skip_dedup, full_mode,
             jobleads_company, jobleads_title) -> None

No module-level state: all flags are explicit parameters, making the
function safe to call as an import from the Telegram bot or tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hunter.config import (
    GENERATE_DOCS_PATH,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    PROJECT_DIR,
)
from hunter.apply_shared import (
    APPLY_MANUAL_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    PROMPTS_DIR,
    _REACT_SKIP_FORCE_HINT,
    _already_processed,
    _ats_check_loop,
    _cover_letter_review,
    _handle_jobleads_fetch_blocked,
    compute_output_folder,
    is_backend_only_job_text,
    is_react_only_job_text,
    notify,
    send_telegram_documents,
    validate_content,
)
from hunter.services.apply_service import build_generate_docs_cmd

_BASE_CV_FILES = {
    "angular": "base_cv_angular.md",
    "react": "base_cv_react.md",
    "javascript": "base_cv_react.md",
    "fullstack_angular_nest": "base_cv_fullstack_angular_nest.md",
    "fullstack_react_next": "base_cv_fullstack_react_next.md",
    "ai": "base_cv_ai.md",
}

_AI_KEYWORDS = {
    "llm", "ai engineer", "ai developer", "ml engineer", "machine learning",
    "prompt engineer", "langchain", "openai", "anthropic", "llm integration",
    "ai-first", "ai first", "agentic", "copilot", "cursor ide",
}


def _detect_stack_hint(job_text: str) -> str:
    """Return a stack key for _BASE_CV_FILES based on job text keywords."""
    text = job_text.lower()
    # AI-first signals: explicit AI/LLM engineering role keywords
    if any(kw in text for kw in _AI_KEYWORDS):
        return "ai"
    # Fullstack: NestJS/Next.js signals
    has_nest = "nestjs" in text or "nest.js" in text
    has_next = "next.js" in text or "nextjs" in text
    has_react = "react" in text
    has_angular = "angular" in text
    # Next.js always implies React track (Next.js IS a React framework)
    if has_next:
        return "fullstack_react_next"
    # NestJS alone: route by frontend framework
    if has_nest:
        if has_react and not has_angular:
            return "fullstack_react_next"
        return "fullstack_angular_nest"
    if has_angular:
        return "angular"
    if has_react:
        return "react"
    return "javascript"


def _load_base_cv(stack_hint: str) -> str:
    """Return base CV markdown for the given stack, or '' if none available."""
    key = stack_hint.lower()
    filename = _BASE_CV_FILES.get(key)
    if not filename:
        return ""
    path = PROMPTS_DIR / filename
    if not path.exists():
        print(f"[apply_agent] Warning: base CV not found at {path}")
        return ""
    return path.read_text(encoding="utf-8")


def main_api(
    url: str,
    paste_text: str = "",
    *,
    skip_dedup: bool = False,
    full_mode: bool = False,
    jobleads_company: str = "",
    jobleads_title: str = "",
) -> None:
    """API pipeline: fetch job text → LLM → content.json → generate_docs.

    Parameters
    ----------
    url:               Job URL (or PASTE_NO_URL_PLACEHOLDER for paste-only flow).
    paste_text:        Pre-fetched text from Telegram paste flow (skips HTTP fetch).
    skip_dedup:        When True, bypass tracker dedup and force aggressive tailoring.
    full_mode:         When True, generate full file set (DOCX + PDF, PL CV, About_Me).
    jobleads_company:  Company hint from hunt listing (used in MANUAL fallback).
    jobleads_title:    Title hint from hunt listing (used in MANUAL fallback).
    """
    url_display = url if url and url != PASTE_NO_URL_PLACEHOLDER else "(pasted text, no URL)"
    print(f"\n[apply_agent] API mode | URL: {url_display}\n")

    if _already_processed(url, skip_dedup=skip_dedup):
        try:
            from hunter.tracker import lookup_url
            rows = lookup_url(url)
            detail = ""
            if rows:
                r = rows[0]
                detail = (
                    f"\n\nRow {r['row']}: <b>{r['company']}</b> — {r['title']}"
                    f"\nATS: {r['ats']}  Sent: {r['sent']}"
                    + (f"\nFolder: <code>{r['folder']}</code>" if r.get("folder") else "")
                )
        except Exception:
            detail = ""
        notify(f"ℹ️ <b>Already in tracker — skipped</b>\n🔗 {url}{detail}")
        print(f"[apply_agent] SKIP — already in tracker: {url}")
        return

    # Step 1 — Get job text: either use pasted text or fetch
    if paste_text:
        job_text = paste_text
        print(f"[apply_agent] Step 1: Using pasted text ({len(job_text)} chars, no fetch)")
    else:
        print("[apply_agent] Step 1: Fetching job posting...")
        try:
            from hunter.sources import fetch_job_text
            job_text = fetch_job_text(url)
            print(f"[apply_agent] Fetched {len(job_text)} chars of job text")
        except Exception as e:
            if "jobleads.com" in url.lower():
                _handle_jobleads_fetch_blocked(
                    url, str(e), company=jobleads_company, title=jobleads_title
                )
            notify(f"❌ <b>Failed to fetch job posting</b>\nURL: {url}\n\n<pre>{str(e)[:400]}</pre>")
            print(f"[apply_agent] FETCH ERROR: {e}")
            sys.exit(1)

    # Step 1.5a — Abort if fetched text is too short
    from hunter.validation import is_job_text_too_short
    if is_job_text_too_short(job_text):
        from hunter.validation import MIN_JOB_TEXT_LEN
        notify(
            f"⚠️ <b>Job text too short — skipped</b>\n"
            f"Got {len((job_text or '').strip())} chars (min {MIN_JOB_TEXT_LEN}).\n🔗 {url}"
        )
        print(f"[apply_agent] ABORT — job text too short ({len((job_text or '').strip())} chars): {url}")
        sys.exit(0)

    # Step 1.5b — Check for expired offer
    from hunter.expired_check import is_job_expired
    if is_job_expired(job_text):
        notify(f"⏭ <b>Expired — skipped</b>\n🔗 {url}")
        print(f"[apply_agent] EXPIRED — offer no longer active: {url}")
        try:
            from hunter.tracker import add_expired
            add_expired(url)
        except Exception as e:
            print(f"[apply_agent] Warning: could not write EXPIRED to tracker: {e}")
        return

    # Step 1.5c — Pre-LLM React-only text check (saves LLM call for obvious React jobs)
    # Skip only when skip_dedup is False (force mode bypasses all stack filters).
    if not skip_dedup and is_react_only_job_text(job_text):
        notify(
            f"⏭ <b>Skipped — React-only (pre-LLM text scan)</b>\n"
            f"🔗 {url}"
            f"{_REACT_SKIP_FORCE_HINT}"
        )
        print(f"[apply_agent] SKIP (pre-LLM) — React-only job text: {url}")
        try:
            from hunter.tracker import add_react_skipped
            add_react_skipped({"stack": "React (pre-LLM)", "company_name": "", "job_title": ""}, url)
        except Exception as e:
            print(f"[apply_agent] Warning: could not write React-skip to tracker: {e}")
        return

    # Step 1.5d — Pre-LLM backend-only text check (no FE framework + explicit BE required)
    if not skip_dedup and is_backend_only_job_text(job_text):
        notify(
            f"⏭ <b>Skipped — Backend-only (pre-LLM text scan)</b>\n"
            f"🔗 {url}"
            f"{_REACT_SKIP_FORCE_HINT}"
        )
        print(f"[apply_agent] SKIP (pre-LLM) — backend-only job text: {url}")
        return

    # Step 2 — Read system prompt (instructions + candidate profile)
    prompt_path = PROMPTS_DIR / "generation_rules.md"
    profile_path = PROMPTS_DIR / "candidate_profile.md"
    if not prompt_path.exists():
        print(f"[apply_agent] ERROR: {prompt_path} not found")
        sys.exit(1)
    instructions = prompt_path.read_text(encoding="utf-8")
    if profile_path.exists():
        profile = profile_path.read_text(encoding="utf-8")
        system_prompt = profile + "\n\n---\n\n" + instructions
    else:
        print(f"[apply_agent] WARNING: {profile_path} not found, using generation_rules.md only")
        system_prompt = instructions

    # Step 2.5 — Load base CV for detected stack (injected into user message)
    stack_hint = _detect_stack_hint(job_text)
    base_cv = _load_base_cv(stack_hint)
    if base_cv:
        print(f"[apply_agent] Step 2.5: Loaded base CV for stack '{stack_hint}'")
    else:
        print(f"[apply_agent] Step 2.5: No base CV for stack '{stack_hint or 'unknown'}' — generating from scratch")

    # Step 3 — Call LLM
    print(f"[apply_agent] Step 3: Calling {LLM_PROVIDER}/{LLM_MODEL}...")
    try:
        from llm_client import call_llm, LLMError
        url_hint = (
            url
            if url and url != PASTE_NO_URL_PLACEHOLDER
            else "(none — text pasted directly by user)"
        )
        user_message = f"Here is the job posting to analyze:\n\n{job_text}\n\nOriginal URL: {url_hint}"
        if base_cv:
            user_message += f"\n\n---\n\n## Base CV — {stack_hint} Track (use as starting point for bullets)\n\n{base_cv}"

        if skip_dedup:
            user_message += (
                "\n\n**FORCE MODE — aggressive tailoring required:**\n"
                "1. Add every technology mentioned in this job posting (React, AI/ML tools, "
                "specific frameworks, etc.) organically into the resume. Put them in the "
                "Skills section and, where it fits naturally, into bullet points in the work "
                "experience (e.g. 'used React to build...', 'integrated AI tooling for...'). "
                "Do NOT invent facts — weave in technologies the candidate plausibly touched "
                "given their background and experience level.\n"
                "2. Target ATS score of 95% or higher. Mirror keywords from the job description "
                "throughout the resume summary, skills list, and experience bullet points.\n"
                "3. Do NOT skip or refuse this job for any stack-related reason — generate "
                "full documents regardless of the technology mix."
            )

        content = call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
        )
    except LLMError as e:
        error_type = "rate_limit" if "rate" in str(e).lower() else "llm_error"
        notify(
            f"❌ <b>LLM failed ({error_type})</b>\n"
            f"URL: {url}\n"
            f"Model: {LLM_MODEL}\n\n"
            f"<pre>{str(e)[:500]}</pre>"
        )
        print(f"[apply_agent] LLM ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        notify(f"❌ <b>Unexpected error in LLM call</b>\n\n<pre>{str(e)[:500]}</pre>")
        print(f"[apply_agent] UNEXPECTED ERROR: {e}")
        sys.exit(1)

    # Step 4 — Validate JSON
    print("[apply_agent] Step 3: Validating LLM output...")
    errors = validate_content(content)
    if errors:
        print(f"[apply_agent] Validation errors: {errors}")
        notify(
            f"⚠️ <b>LLM output validation issues</b>\n"
            f"URL: {url}\n\n"
            + "\n".join(f"• {e}" for e in errors[:10])
        )

    # Step 4.4 — ATS boost pass (force mode only): if score < 95%, do a second LLM pass
    if skip_dedup:
        from hunter.tracker import _parse_ats_score
        _raw_ats = str(content.get("ats_score", "") or "")
        _, _ats_num = _parse_ats_score(_raw_ats)
        if _ats_num is not None and _ats_num < 95:
            print(f"[apply_agent] Force ATS boost: score={_ats_num}% < 95%, running second pass...")
            try:
                _boost_msg = (
                    f"The resume currently scores {_ats_num}% ATS against the job posting. "
                    "Rewrite 'resume_en' to reach 95%+:\n"
                    "- Add more matching keywords from the job description to the summary, "
                    "skills, and experience bullet points.\n"
                    "- Strengthen alignment with the required and preferred qualifications.\n"
                    "- Update the resume summary to reflect the exact job title and key requirements.\n"
                    "Return the same JSON schema with updated fields "
                    "('resume_en', 'resume_pl', 'ats_score', 'stack', 'to_learn'). "
                    "You may also update other fields if needed.\n\n"
                    f"Job posting:\n{job_text}\n\n"
                    f"Current resume content (JSON):\n{json.dumps(content, ensure_ascii=False)}"
                )
                from llm_client import call_llm
                _boosted = call_llm(
                    system_prompt=system_prompt,
                    user_message=_boost_msg,
                    provider=LLM_PROVIDER,
                    model=LLM_MODEL,
                    api_key=LLM_API_KEY,
                )
                for _key in ("resume_en", "resume_pl", "ats_score", "stack", "to_learn"):
                    if _boosted.get(_key):
                        content[_key] = _boosted[_key]
                _, _new_ats = _parse_ats_score(str(content.get("ats_score", "") or ""))
                print(f"[apply_agent] ATS after boost: {content.get('ats_score')} ({_new_ats}%)")
            except Exception as _boost_err:
                print(f"[apply_agent] ATS boost failed (using first pass): {_boost_err}")

    # Step 4.5 — Skip React-only jobs
    stack = (content.get("stack") or "").lower()
    if "react" in stack and "angular" not in stack and not skip_dedup:
        notify(
            f"⏭ <b>Skipped — React-only stack</b>\n"
            f"🔗 {url}\n"
            f"Stack: {content.get('stack', '?')}"
            f"{_REACT_SKIP_FORCE_HINT}"
        )
        print(f"[apply_agent] SKIP — React-only stack: {content.get('stack')}")
        try:
            from hunter.tracker import add_react_skipped
            add_react_skipped(content, url)
        except Exception as e:
            print(f"[apply_agent] Warning: could not write React-skip to tracker: {e}")
        return

    # Step 4.6 — Independent ATS check + rewrite loop for resume (target ≥ 95%)
    print("[apply_agent] Step 4.6: Running independent ATS check on resume...")
    content = _ats_check_loop(content, job_text)

    # Step 4.7 — Sanitize resume
    print("[apply_agent] Step 4.7: Sanitizing resume content...")
    try:
        from hunter.resume_sanitizer import sanitize_content
        content = sanitize_content(content)
    except Exception as _san_err:
        print(f"[apply_agent] Warning: resume sanitizer failed (continuing): {_san_err}")

    # Step 4.8 — Single-pass cover letter review
    print("[apply_agent] Step 4.8: Reviewing cover letter...")
    content = _cover_letter_review(content)

    # Step 5 — Compute output folder and finalize JSON
    company = content.get("company_name", "Unknown")

    from hunter.validation import is_bogus_company
    if is_bogus_company(company):
        notify(
            f"⚠️ <b>Bogus company name — skipped</b>\n"
            f"LLM returned: <code>{company}</code>\n🔗 {url}"
        )
        print(f"[apply_agent] ABORT — bogus company name {company!r}: {url}")
        sys.exit(0)

    output_folder = compute_output_folder(company)
    output_folder.mkdir(parents=True, exist_ok=True)

    content["output_folder"] = str(output_folder).replace("\\", "/")
    content["apply_url"] = "" if url == PASTE_NO_URL_PLACEHOLDER else url
    if "ats_score" not in content:
        content["ats_score"] = ""

    # Step 6 — Write content.json + job_posting.txt
    content_path = output_folder / "content.json"
    content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[apply_agent] Wrote {content_path}")

    job_posting_path = output_folder / "job_posting.txt"
    try:
        url_line = (
            f"URL: {url}\n\n"
            if url and url != PASTE_NO_URL_PLACEHOLDER
            else "URL: (none — pasted by user)\n\n"
        )
        job_posting_path.write_text(url_line + job_text, encoding="utf-8")
        print(f"[apply_agent] Saved job posting -> {job_posting_path.name}")
    except Exception as e:
        print(f"[apply_agent] Warning: could not save job_posting.txt: {e}")

    # Step 7 — Run generate_docs.py
    gen_cmd = build_generate_docs_cmd(
        generate_docs_script=GENERATE_DOCS_PATH,
        content_json_path=content_path,
        use_full=full_mode,
        force=skip_dedup,
        python_executable=sys.executable,
    )
    mode_label = "FULL" if full_mode else "SHORT"
    print(f"[apply_agent] Step 4: Generating docs ({mode_label})...")
    gen_ok = True
    try:
        result = subprocess.run(
            gen_cmd,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        gen_ok = result.returncode == 0
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[generate_docs] STDERR:", result.stderr, file=sys.stderr)
    except subprocess.TimeoutExpired:
        gen_ok = False
        print("[apply_agent] generate_docs.py timed out (120s)")

    # Step 8 — Notify success
    created_files = list(output_folder.glob("*.docx")) + list(output_folder.glob("*.pdf"))
    if created_files:
        file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
        ats = content.get("ats_score", "?")
        issues_note = ""
        if not gen_ok:
            issues_note = (
                "\n\n⚠️ <code>generate_docs.py</code> reported a problem "
                "(often <code>tracker.xlsx</code> locked, timeout, or PDF step). "
                "Files listed above are on disk; fix and re-run that script if needed."
            )
        notify(
            f"✅ <b>Docs ready!</b>\n\n"
            f"📁 <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>\n\n"
            f"{file_names}\n\n"
            f"ATS: {ats}% | Stack: {content.get('stack', '?')}\n"
            f"Via: API ({LLM_MODEL})\n"
            f"Review and send when ready."
            f"{issues_note}"
        )
        send_telegram_documents(created_files)
        print(
            f"\n[apply_agent] Done! Folder: "
            f"Applications/{output_folder.parent.name}/{output_folder.name}/ "
            f"({len(created_files)} files)"
        )
    else:
        notify(
            f"⚠️ <b>content.json OK but no docs generated</b>\n"
            f"📁 <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>\n"
            f"Run manually: python generate_docs.py \"{content_path}\""
        )
        print(f"\n[apply_agent] WARNING: No .docx/.pdf files found, but content.json is saved.")
        sys.exit(1)
