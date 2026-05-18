"""
hunter/apply_api.py — API-mode apply pipeline.

Fetches job text, calls the LLM, runs quality loops, generates docs.
"""

import json
import subprocess
import sys
from pathlib import Path

from hunter.apply_shared import (
    PASTE_NO_URL_PLACEHOLDER,
    PROMPTS_DIR,
    _ats_check_loop,
    _cover_letter_review_loop,
    _sanitize_folder_company,
    compute_output_folder,
    handle_jobleads_fetch_blocked,
    validate_content,
)
from hunter.config import (
    GENERATE_DOCS_PATH,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    PROJECT_DIR,
)
from hunter.notify import notify, send_telegram_documents
from hunter.services.apply_service import build_generate_docs_cmd

_GENERATE_DOCS_SCRIPT = GENERATE_DOCS_PATH


def main_api(
    url: str,
    paste_text: str = "",
    force: bool = False,
    full: bool = False,
    meta_company: str = "",
    meta_title: str = "",
) -> None:
    url_display = url if url and url != PASTE_NO_URL_PLACEHOLDER else "(pasted text, no URL)"
    print(f"\n[apply_agent] API mode | URL: {url_display}\n")

    # ── Dedup check ──────────────────────────────────────────────────────────
    if not force and url and url != PASTE_NO_URL_PLACEHOLDER:
        try:
            from hunter.services.tracker_service import should_skip_url
            if should_skip_url(url):
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
        except Exception:
            pass

    # ── Step 1: Get job text ─────────────────────────────────────────────────
    if paste_text:
        job_text = paste_text
        print(f"[apply_agent] Step 1: Using pasted text ({len(job_text)} chars, no fetch)")
    else:
        print("[apply_agent] Step 1: Fetching job posting...")
        try:
            from job_fetch import fetch_job_text  # type: ignore[import]
            job_text = fetch_job_text(url)
            print(f"[apply_agent] Fetched {len(job_text)} chars of job text")
        except Exception as e:
            if "jobleads.com" in url.lower():
                handle_jobleads_fetch_blocked(url, str(e), meta_company, meta_title)
            notify(f"❌ <b>Failed to fetch job posting</b>\nURL: {url}\n\n<pre>{str(e)[:400]}</pre>")
            print(f"[apply_agent] FETCH ERROR: {e}")
            sys.exit(1)

    # ── Step 1.5: Expired check ──────────────────────────────────────────────
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

    # ── Step 2: Read system prompt ───────────────────────────────────────────
    prompt_path = PROMPTS_DIR / "system_prompt.md"
    profile_path = PROMPTS_DIR / "candidate_profile.md"
    if not prompt_path.exists():
        print(f"[apply_agent] ERROR: {prompt_path} not found")
        sys.exit(1)
    instructions = prompt_path.read_text(encoding="utf-8")
    if profile_path.exists():
        profile = profile_path.read_text(encoding="utf-8")
        system_prompt = profile + "\n\n---\n\n" + instructions
    else:
        print(f"[apply_agent] WARNING: {profile_path} not found, using system_prompt.md only")
        system_prompt = instructions

    # ── Step 3: Call LLM ─────────────────────────────────────────────────────
    print(f"[apply_agent] Step 2: Calling {LLM_PROVIDER}/{LLM_MODEL}...")
    try:
        from llm_client import call_llm, LLMError  # type: ignore[import]
        url_hint = (
            url if url and url != PASTE_NO_URL_PLACEHOLDER
            else "(none — text pasted directly by user)"
        )
        user_message = f"Here is the job posting to analyze:\n\n{job_text}\n\nOriginal URL: {url_hint}"

        if force:
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

    # ── Step 4: Validate JSON ────────────────────────────────────────────────
    print("[apply_agent] Step 3: Validating LLM output...")
    errors = validate_content(content)
    if errors:
        print(f"[apply_agent] Validation errors: {errors}")
        notify(
            f"⚠️ <b>LLM output validation issues</b>\n"
            f"URL: {url}\n\n"
            + "\n".join(f"• {e}" for e in errors[:10])
        )

    # ── Step 4.4: Force ATS boost ────────────────────────────────────────────
    if force:
        from hunter.tracker import _parse_ats_score
        _raw_ats = str(content.get("ats_score", "") or "")
        _, _ats_num = _parse_ats_score(_raw_ats)
        if _ats_num is not None and _ats_num < 95:
            print(f"[apply_agent] Force ATS boost: score={_ats_num}% < 95%, running second pass...")
            try:
                _boost_msg = (
                    f"The resume currently scores {_ats_num}% ATS against the job posting. "
                    "Rewrite 'resume_en' and 'cover_letter_en' to reach 95%+:\n"
                    "- Add more matching keywords from the job description to the summary, "
                    "skills, and experience bullet points.\n"
                    "- Strengthen alignment with the required and preferred qualifications.\n"
                    "- Update the resume summary to reflect the exact job title and key requirements.\n"
                    "Return the same JSON schema with updated fields "
                    "('resume_en', 'cover_letter_en', 'cover_letter_pl', 'ats_score', 'stack', 'to_learn'). "
                    "You may also update other fields if needed.\n\n"
                    f"Job posting:\n{job_text}\n\n"
                    f"Current resume content (JSON):\n{json.dumps(content, ensure_ascii=False)}"
                )
                from llm_client import call_llm  # type: ignore[import]
                _boosted = call_llm(
                    system_prompt=system_prompt,
                    user_message=_boost_msg,
                    provider=LLM_PROVIDER,
                    model=LLM_MODEL,
                    api_key=LLM_API_KEY,
                )
                for _key in ("resume_en", "cover_letter_en", "cover_letter_pl", "ats_score", "stack", "to_learn"):
                    if _boosted.get(_key):
                        content[_key] = _boosted[_key]
                _, _new_ats = _parse_ats_score(str(content.get("ats_score", "") or ""))
                print(f"[apply_agent] ATS after boost: {content.get('ats_score')} ({_new_ats}%)")
            except Exception as _boost_err:
                print(f"[apply_agent] ATS boost failed (using first pass): {_boost_err}")

    # ── Step 4.5: React-only skip ────────────────────────────────────────────
    from hunter.apply_shared import REACT_SKIP_FORCE_HINT
    stack = (content.get("stack") or "").lower()
    _angular_in_raw = "angular" in job_text.lower()
    if "react" in stack and "angular" not in stack and not _angular_in_raw and not force:
        notify(
            f"⏭ <b>Skipped — React-only stack</b>\n"
            f"🔗 {url}\n"
            f"Stack: {content.get('stack', '?')}"
            f"{REACT_SKIP_FORCE_HINT}"
        )
        print(f"[apply_agent] SKIP — React-only stack: {content.get('stack')}")
        try:
            from hunter.tracker import add_react_skipped
            add_react_skipped(content, url)
        except Exception as e:
            print(f"[apply_agent] Warning: could not write React-skip to tracker: {e}")
        return

    # ── Step 4.6: Cover letter review ────────────────────────────────────────
    print("[apply_agent] Step 4.6: Reviewing cover letter for AI-language patterns...")
    content = _cover_letter_review_loop(content)

    # ── Step 4.7: Independent ATS check ──────────────────────────────────────
    print("[apply_agent] Step 4.7: Running independent ATS check...")
    content = _ats_check_loop(content, job_text)

    # ── Step 5: Compute output folder ────────────────────────────────────────
    company = content.get("company_name", "Unknown")
    output_folder = compute_output_folder(company)
    output_folder.mkdir(parents=True, exist_ok=True)

    content["output_folder"] = str(output_folder).replace("\\", "/")
    content["apply_url"] = "" if url == PASTE_NO_URL_PLACEHOLDER else url
    if "ats_score" not in content:
        content["ats_score"] = ""

    # ── Step 6: Write content.json + job_posting.txt ─────────────────────────
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

    # ── Step 7: Run generate_docs.py ─────────────────────────────────────────
    gen_cmd = build_generate_docs_cmd(
        generate_docs_script=_GENERATE_DOCS_SCRIPT,
        content_json_path=content_path,
        use_full=full,
        force=force,
        python_executable=sys.executable,
    )
    mode_label = "FULL" if full else "SHORT"
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

    # ── Step 8: Notify success ───────────────────────────────────────────────
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
            f"\n[apply_agent] Done! Folder: Applications/"
            f"{output_folder.parent.name}/{output_folder.name}/ ({len(created_files)} files)"
        )
    else:
        notify(
            f"⚠️ <b>content.json OK but no docs generated</b>\n"
            f"📁 <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>\n"
            f"Run manually: python generate_docs.py \"{content_path}\""
        )
        print(f"\n[apply_agent] WARNING: No .docx/.pdf files found, but content.json is saved.")
        sys.exit(1)
