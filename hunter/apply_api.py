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
    PROJECT_DIR,
)
from hunter.llm_profiles import get_active as _get_llm_profile
from hunter.apply_shared import (
    APPLY_RATE_LIMITED_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    PROMPTS_DIR,
    _REACT_SKIP_FORCE_HINT,
    _already_processed,
    _ats_check_loop,
    _handle_jobleads_fetch_blocked,
    is_transient_fetch_error,
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
) -> Path | None:
    """API pipeline: fetch job text → LLM → content.json → generate_docs.

    Returns the output folder on success (so the caller can run the dual-apply
    shadow), or None when the job was skipped / deduped / expired.

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

    # Begin per-vacancy LLM cost accounting. Every call_llm fired from inside
    # this push/pop pair (generation, ATS-loop rewrites, CL self-review,
    # claim-judge + repair, judge model calls) appends one usage record to
    # `_usage_log`; the apply_shared helpers don't need to know about it.
    # Manual push/pop instead of `with` because main_api has half a dozen
    # early returns + sys.exit paths and wrapping the whole body in a `with`
    # would be a huge whitespace diff. The finally block guarantees pop
    # regardless of how the function exits.
    from llm_client import pop_usage_log, push_usage_log
    _usage_log = push_usage_log()
    try:
        return _run_main_api(
            url=url,
            paste_text=paste_text,
            skip_dedup=skip_dedup,
            full_mode=full_mode,
            jobleads_company=jobleads_company,
            jobleads_title=jobleads_title,
            _usage_log=_usage_log,
        )
    finally:
        pop_usage_log()


def _run_main_api(
    *,
    url: str,
    paste_text: str,
    skip_dedup: bool,
    full_mode: bool,
    jobleads_company: str,
    jobleads_title: str,
    _usage_log: list,
) -> Path | None:
    """Inner body of main_api, split out so push_usage_log() / pop_usage_log()
    can wrap it without forcing a 500-line `with` block. See main_api for arg
    semantics. `_usage_log` is the live accounting frame populated as the
    pipeline runs LLM calls; the notify step reads it to price the run.
    """
    # Resolve the active LLM profile once for the whole pipeline. Every call_llm
    # below uses _llm_prof.provider/.model/.api_key so a /llm switch takes effect
    # on the next vacancy without a bot restart.
    _llm_prof = _get_llm_profile()

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
            if is_transient_fetch_error(e, url):
                # Transient anti-bot block (429 anywhere, or 403/Cloudflare on a
                # known anti-bot host like pracuj/LinkedIn) — signal the caller to
                # retry later WITHOUT escalating the permanent fail counter, so it
                # never becomes a "gave up" dead row. The offer itself is fine.
                print(f"[apply_agent] FETCH BLOCKED (transient, retry later): {e}")
                sys.exit(APPLY_RATE_LIMITED_EXIT_CODE)
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

    # Step 1.5e — Manual-apply "warn but allow" screen. A pasted URL bypasses the
    # hunt-time filter, so re-run the body-level gates against the fetched text and
    # warn (without aborting) if the posting would normally have been filtered out.
    # Hunt/AUTO jobs already passed these gates, so this only fires on manual input.
    try:
        from hunter.filters import screen_job_text
        screen_reason = screen_job_text(job_text)
        if screen_reason:
            notify(
                f"⚠️ <b>Heads-up — this posting would normally be filtered</b>\n"
                f"Reason: {screen_reason}\n"
                f"🔗 {url}\n\n"
                f"Generating documents anyway (manual override)…"
            )
            print(f"[apply_agent] WARN (manual screen) — {screen_reason}: {url}")
    except Exception as e:  # noqa: BLE001 — best-effort, never block apply
        print(f"[apply_agent] Warning: manual screen failed: {e}")

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
    print(f"[apply_agent] Step 3: Calling {_llm_prof.provider}/{_llm_prof.model}...")
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
            provider=_llm_prof.provider,
            model=_llm_prof.model,
            api_key=_llm_prof.api_key,
        )
    except LLMError as e:
        error_type = "rate_limit" if "rate" in str(e).lower() else "llm_error"
        notify(
            f"❌ <b>LLM failed ({error_type})</b>\n"
            f"URL: {url}\n"
            f"Model: {_llm_prof.model}\n\n"
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
        # Repair pass: structural errors (e.g. a dropped role) make the resume
        # unusable, so ask the LLM once to return a complete, fixed JSON rather
        # than silently generating a broken PDF.
        try:
            _repair_msg = (
                "The JSON you returned has structural problems that make the resume "
                "invalid. Fix ALL of the issues below and return the COMPLETE JSON "
                "again (same schema, every field), not just the changed parts:\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\n\nCRITICAL: resume_en.experience MUST contain ALL 7 roles in this "
                "exact order: Alten Poland, Fairmarkit, Venture Labs, SII, Altoros, "
                "SolbegSoft, Staronka. Never drop a role to fit 2 pages — compress "
                "older roles to 1-2 bullets instead. Keep company, period, title, "
                "subtitle verbatim per the rules.\n\n"
                f"Previous JSON to fix:\n{json.dumps(content, ensure_ascii=False)}"
            )
            from llm_client import call_llm as _repair_call_llm
            _repaired = _repair_call_llm(
                system_prompt=system_prompt,
                user_message=_repair_msg,
                provider=_llm_prof.provider,
                model=_llm_prof.model,
                api_key=_llm_prof.api_key,
            )
            _repaired_errors = validate_content(_repaired)
            if len(_repaired_errors) < len(errors):
                print(
                    f"[apply_agent] Repair pass improved validation: "
                    f"{len(errors)} -> {len(_repaired_errors)} errors"
                )
                content = _repaired
                errors = _repaired_errors
            else:
                print("[apply_agent] Repair pass did not improve output; keeping first pass")
        except Exception as _repair_err:
            print(f"[apply_agent] Repair pass failed (using first pass): {_repair_err}")

    if errors:
        notify(
            f"⚠️ <b>LLM output validation issues (after repair)</b>\n"
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
                    provider=_llm_prof.provider,
                    model=_llm_prof.model,
                    api_key=_llm_prof.api_key,
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

    # Strip fabricated regulatory/compliance claims (DORA/RODO/GDPR/ISO/...) that
    # belong to the employer's self-description, not the candidate's expertise.
    try:
        from hunter.apply_shared import _strip_compliance_claims
        content, _compliance_fixes = _strip_compliance_claims(content)
        for _fix in _compliance_fixes:
            print(f"[apply_agent] compliance-scrub: {_fix}")
    except Exception as _cc_err:
        print(f"[apply_agent] Warning: compliance scrub failed (continuing): {_cc_err}")

    # Strip fabricated client-prestige claims ("Fortune 500 clients", "top-tier")
    # the LLM invents despite the RED LINE, and collapse "term / synonym" gloss
    # pairs the ATS rewrite leaves in the skills section.
    try:
        from hunter.apply_shared import _dedup_skill_glosses, _strip_prestige_claims
        content, _prestige_fixes = _strip_prestige_claims(content, job_text)
        for _fix in _prestige_fixes:
            print(f"[apply_agent] prestige-scrub: {_fix}")
        content, _gloss_fixes = _dedup_skill_glosses(content)
        for _fix in _gloss_fixes:
            print(f"[apply_agent] gloss-dedup: {_fix}")
    except Exception as _ps_err:
        print(f"[apply_agent] Warning: prestige/gloss scrub failed (continuing): {_ps_err}")

    # Step 4.72 — Claim judge: a second cheap model verifies every generated
    # claim against the candidate profile + job posting and returns a structured
    # violations list. Runs after the deterministic scrubs (first echelon) and
    # BEFORE the language gate (a repair could introduce language drift; the gate
    # stays the last word). Best-effort — never fatal. See docs/CV_JUDGE_PLAN.md.
    judge_report = None
    from hunter.config import JUDGE_ENABLED, JUDGE_MODE
    if JUDGE_ENABLED:
        print("[apply_agent] Step 4.72: Claim judge verifying content...")
        try:
            from hunter.claim_judge import run_judge_stage
            _outcome = run_judge_stage(
                content, job_text, base_cv, enabled=True, mode=JUDGE_MODE
            )
            content = _outcome.content
            judge_report = _outcome.report
            for _v in judge_report.actionable:
                print(f"[apply_agent] judge: [{_v.severity}] {_v.field}: {_v.reason}")
            for _fix in _outcome.fixes:
                print(f"[apply_agent] judge-repair: {_fix}")
            if JUDGE_MODE in ("warn", "block") and judge_report.actionable:
                notify(judge_report.telegram_summary(url))
            if _outcome.blocked:
                notify(
                    f"⛔ <b>Blocked — fabricated claim survived repair</b>\n"
                    f"🔗 {url}\n"
                    + "\n".join(f"• {v.field}: {v.reason[:100]}" for v in _outcome.survivors[:3])
                )
                print(f"[apply_agent] ABORT — claim judge blocked delivery: {url}")
                sys.exit(0)
        except SystemExit:
            raise
        except Exception as _judge_err:
            print(f"[apply_agent] Warning: claim judge failed (continuing): {_judge_err}")

    # Step 4.75 — Language enforce-gate: each _en field must be clean English and
    # each _pl field clean Polish. Polish postings cause the ATS loop to inject
    # Polish keywords into resume_en; here we repair by translating from the clean
    # opposite-language counterpart, and BLOCK delivery if strong Polish survives.
    from hunter.lang_guard import detect_posting_language
    posting_lang = detect_posting_language(job_text)
    print(f"[apply_agent] Step 4.75: Language gate (posting language: {posting_lang})...")
    try:
        from hunter.apply_shared import enforce_language_separation
        content, _lang_blocked, _lang_report = enforce_language_separation(content)
        for _line in _lang_report:
            print(f"[apply_agent] lang-gate: {_line}")
        if _lang_blocked:
            notify(
                f"⛔ <b>Blocked — Polish leaked into the English CV</b>\n"
                f"🔗 {url}\n"
                f"The English resume still contained Polish after an automatic "
                f"translation pass, so no document was generated (a broken CV was "
                f"NOT sent). Re-run /force to retry, or apply manually.\n\n"
                + "\n".join(f"• {l}" for l in _lang_report[-3:])
            )
            print(f"[apply_agent] ABORT — language gate blocked delivery: {url}")
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as _lang_err:
        print(f"[apply_agent] Warning: language gate failed (continuing): {_lang_err}")

    # Step 4.8 — Content QA sanity check
    print("[apply_agent] Step 4.9: Running content QA checks...")
    try:
        from hunter.content_qa import run_qa
        qa = run_qa(content)
        print(qa.summary())
        if not qa.passed:
            notify(qa.telegram_summary(url))
    except Exception as _qa_err:
        print(f"[apply_agent] Warning: QA check failed (continuing): {_qa_err}")

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
    # Deterministic posting language drives which CV is delivered as primary
    # (PL posting → also render the Polish CV in short mode; see generate_docs).
    content["primary_lang"] = posting_lang
    if "ats_score" not in content:
        content["ats_score"] = ""

    # Step 6 — Write content.json + job_posting.txt
    content_path = output_folder / "content.json"

    def _persist_content() -> None:
        """Serialize the live `content` dict to content.json. Single write
        path for Steps 6/6.5/7.5/7.7 so the serialization can't diverge."""
        content_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    _persist_content()
    print(f"[apply_agent] Wrote {content_path}")

    # Audit trail for tuning the judge prompt: persist findings whenever any.
    if judge_report is not None and judge_report.violations:
        try:
            (output_folder / "judge_report.json").write_text(
                json.dumps(judge_report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[apply_agent] Warning: could not write judge_report.json: {e}")

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

    # Step 6.5 — Price the full LLM run and persist on content.json BEFORE
    # generate_docs runs.
    #
    # All LLM work is done by this point (generation, ATS-loop rewrites, CL
    # self-review, judge + repair). generate_docs.py is a child Python
    # subprocess that imports hunter.services.tracker_service and calls
    # add_applied — which reads cost off content.json. If we priced AFTER
    # the subprocess, add_applied would land cost_usd=NULL in tracker.db
    # and the downstream Sheet mirror (mirror_cost_cell_sync) would no-op
    # because it reads cost_usd from the DB. Result: every row's M column
    # stays empty and the whole "cost in the table" feature silently
    # collapses. (Verified by /code-review on PR #104.)
    #
    # Best-effort throughout: any pricing error logs + falls back to None
    # so the apply flow completes regardless. cost_dict is read again
    # later for the Telegram summary so an unparseable usage log doesn't
    # both skip the DB cost AND make the message misleading.
    cost_dict: dict | None = None
    try:
        from hunter.llm_cost import price_usage as _price_usage
        cost_dict = _price_usage(_usage_log)
        content["cost"] = cost_dict
        try:
            _persist_content()
        except Exception as _save_err:
            print(f"[apply_agent] Warning: could not persist cost to content.json: {_save_err}")
    except Exception as e:
        print(f"[apply_agent] Warning: cost pricing failed (continuing): {e}")

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

    # Step 7.5 — PDF roundtrip + NBSP self-heal.
    #
    # First pass: extract text from the rendered EN CV PDF and re-score it
    # against the job posting. This catches keywords that python-docx →
    # LibreOffice → PDF drops at render time (multi-word phrases split across
    # a line wrap, lost bullets, table reordering) that the JSON ATS score
    # can't see.
    #
    # If the PDF score is ≥ HEAL_DELTA_PP below the JSON score, the loss is
    # almost certainly a multi-word keyword breaking on a wrap ("performance
    # optimization" → "performance\noptimization"). Patch each affected
    # phrase with NBSP in content.json, regenerate the docs, re-score once.
    # The user never sees a warn flag — either we self-healed it, or one
    # extra pass wasn't enough and we accept the residual rather than
    # spamming Telegram with an unactionable number.
    #
    # Best-effort throughout: any failure logs + continues. Heuristic only —
    # no LLM call, no extra API spend.
    pdf_summary = ""
    if gen_ok:
        try:
            from hunter.ats_pdf_roundtrip import (
                HEAL_DELTA_PP,
                format_summary,
                nbsp_patch_missing_keywords,
                run_pdf_roundtrip,
            )

            pdf_check = run_pdf_roundtrip(
                folder=output_folder,
                job_text=job_text,
                json_ats_score=content.get("ats_score"),
            )

            delta = pdf_check.get("delta_from_json") if pdf_check else None
            if pdf_check and delta is not None and delta <= -HEAL_DELTA_PP:
                missing = pdf_check.get("missing_keywords") or []
                patches = nbsp_patch_missing_keywords(content, missing)
                if patches:
                    print(
                        f"[apply_agent] PDF Δ={delta:+.1f}pp — "
                        f"patched {patches} multi-word keyword(s) with NBSP, regenerating"
                    )
                    _persist_content()
                    try:
                        subprocess.run(
                            gen_cmd,
                            cwd=str(PROJECT_DIR),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=120,
                        )
                        pdf_check_2 = run_pdf_roundtrip(
                            folder=output_folder,
                            job_text=job_text,
                            json_ats_score=content.get("ats_score"),
                        )
                        if pdf_check_2 is not None:
                            pdf_check = pdf_check_2
                    except subprocess.TimeoutExpired:
                        print("[apply_agent] self-heal regen timed out (120s) — keeping original PDF")

            if pdf_check is not None:
                content["ats_check_pdf"] = pdf_check
                _persist_content()
                pdf_summary = " | " + format_summary(pdf_check)
                print(f"[apply_agent] {format_summary(pdf_check)}")
        except Exception as e:
            print(f"[apply_agent] Warning: PDF roundtrip failed (continuing): {e}")

    # Step 7.7 — Final independent ATS verdict: one cheap-LLM (judge model) call
    # over the text extracted from the delivered EN CV PDF. This is the number
    # the user sees in Telegram — an assessor that did not write the resume,
    # scoring exactly what a real ATS parses. Informational, never blocks.
    verdict = None
    if gen_ok:
        try:
            from hunter.ats_pdf_roundtrip import format_verdict, run_llm_verdict
            verdict = run_llm_verdict(folder=output_folder, job_text=job_text)
            if verdict is not None:
                # Step 7.7b — Verdict refine loop: if the independent verdict is
                # below target, rewrite resume_en (honest, then stretch) against
                # its own feedback, re-render, and re-verdict — keeping only
                # strict improvements. See docs/VERDICT_REFINE_PLAN.md.
                from hunter.config import ATS_VERDICT_MAX_REFINES, ATS_VERDICT_TARGET
                if (
                    float(verdict.get("score") or 0) < ATS_VERDICT_TARGET
                    and ATS_VERDICT_MAX_REFINES > 0
                ):
                    print(
                        f"[apply_agent] Verdict {verdict.get('score')}% < target "
                        f"{ATS_VERDICT_TARGET}% — running refine loop "
                        f"(max {ATS_VERDICT_MAX_REFINES} round(s))..."
                    )
                    from hunter.verdict_refine import refine_loop

                    def _regen_for_refine(_folder: Path) -> None:
                        subprocess.run(
                            gen_cmd,
                            cwd=str(PROJECT_DIR),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=120,
                        )

                    content, verdict = refine_loop(
                        content, job_text, base_cv, output_folder, verdict,
                        regenerate_docs=_regen_for_refine,
                        target=ATS_VERDICT_TARGET,
                        max_rounds=ATS_VERDICT_MAX_REFINES,
                    )
                content["ats_verdict"] = verdict
                print(f"[apply_agent] {format_verdict(verdict)}")
                # Stamp the score on the tracker row (created by generate_docs
                # in Step 7, so it already exists). The Sheets column-N cell is
                # mirrored later by the bot process (gsheets_sync.mirror_new_row
                # reads ats_verdict from the DB). Paste flow has no URL to match
                # a row by — skip the stamp there.
                if url and url != PASTE_NO_URL_PLACEHOLDER:
                    try:
                        from hunter.tracker import set_ats_verdict
                        set_ats_verdict(url, float(verdict["score"]))
                    except Exception as _tr_err:
                        print(f"[apply_agent] Warning: verdict tracker stamp failed: {_tr_err}")
                # Re-price so content.json + the Telegram summary include the
                # verdict call. The tracker row (written by generate_docs in
                # Step 7, before this call) keeps the pre-verdict figure — the
                # delta is one Haiku call (~$0.02), acceptable drift.
                try:
                    from hunter.llm_cost import price_usage as _price_usage2
                    cost_dict = _price_usage2(_usage_log)
                    content["cost"] = cost_dict
                except Exception as _cost_err:
                    print(f"[apply_agent] Warning: verdict re-pricing failed: {_cost_err}")
                _persist_content()
        except Exception as e:
            print(f"[apply_agent] Warning: ATS verdict failed (continuing): {e}")

    # Step 8 — Notify success (cost was already priced + persisted in Step 6.5).
    created_files = list(output_folder.glob("*.docx")) + list(output_folder.glob("*.pdf"))
    if created_files:
        file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
        # Only the independent verdict is user-facing (owner request — the
        # generator's own self-score was noisy: "self-scored myself 96%").
        # It stays on content.json for diagnostics, just not in Telegram.
        if verdict is not None:
            ats_line = f"ATS: {verdict.get('score')}% (independent, PDF)"
        else:
            ats_line = f"ATS: {content.get('ats_score', '?')}%"
        cost_line = ""
        if cost_dict is not None:
            from hunter.llm_cost import format_summary as _cost_summary
            cost_line = f"\n{_cost_summary(cost_dict)}"
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
            f"{ats_line}{pdf_summary} | Stack: {content.get('stack', '?')}\n"
            f"Via: API ({_llm_prof.model}){cost_line}\n"
            f"Review and send when ready."
            f"{issues_note}"
        )
        send_telegram_documents(created_files)
        print(
            f"\n[apply_agent] Done! Folder: "
            f"Applications/{output_folder.parent.name}/{output_folder.name}/ "
            f"({len(created_files)} files)"
        )
        # Success: hand the folder back so apply_agent.main() can run the
        # dual-apply shadow comparison (if enabled) off the saved job_posting.txt.
        return output_folder
    else:
        notify(
            f"⚠️ <b>content.json OK but no docs generated</b>\n"
            f"📁 <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>\n"
            f"Run manually: python generate_docs.py \"{content_path}\""
        )
        print("\n[apply_agent] WARNING: No .docx/.pdf files found, but content.json is saved.")
        sys.exit(1)
