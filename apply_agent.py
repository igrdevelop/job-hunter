#!/usr/bin/env python3
"""
apply_agent.py — Autonomous apply agent.

Auth priority:
  1. CLI first (Claude Pro subscription) — if `claude` CLI is installed & logged in
  2. API fallback — uses LLM_API_KEY / ANTHROPIC_API_KEY from .env
  Override: --cli forces CLI-only; APPLY_USE_CLI=true in .env does the same.

Doc generation:
  Default (short mode): PDF-only, EN CV only (no PL CV, no .txt files)
  --full flag: all files (DOCX + PDF, PL CV, About_Me .txt files)

Usage:
  python apply_agent.py "https://justjoin.it/job-offer/company-role-city-tech"
  python apply_agent.py "https://nofluffjobs.com/job/some-slug"
  python apply_agent.py --cli "https://..."      # force CLI mode
  python apply_agent.py --full "https://..."     # generate all file types
  python apply_agent.py --force "https://..."    # skip tracker dedup
  python apply_agent.py --paste-file posting.txt            # no URL, use pasted text
  python apply_agent.py --paste-file posting.txt "https://..."  # URL + pasted text
"""

import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from hunter.config import (
    APPLY_USE_CLI,
    APPLICATIONS_DIR,
    CLI_MAX_RETRIES,
    CLI_RETRY_DELAY,
    GENERATE_DOCS_PATH,
    GENERATE_PL_RESUME,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    PROJECT_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from hunter.services.apply_service import build_generate_docs_cmd

# ── Config ────────────────────────────────────────────────────────────────────
PROMPTS_DIR = PROJECT_DIR / "prompts"
GENERATE_DOCS_SCRIPT = GENERATE_DOCS_PATH

REQUIRED_JSON_KEYS = [
    "company_name", "stack", "lang", "job_title",
    "resume_en", "cover_letter_en", "cover_letter_pl",
    "about_me_en", "about_me_pl",
]
if GENERATE_PL_RESUME:
    REQUIRED_JSON_KEYS.append("resume_pl")

_SKIP_DEDUP = False
_FULL_MODE = False

# Optional context from hunter when URL is jobleads.com (see apply_service subprocess argv)
_APPLY_META_COMPANY = ""
_APPLY_META_TITLE = ""

# Exit code: JobLeads fetch blocked — MANUAL tracker row + stub job_posting.txt written
APPLY_MANUAL_EXIT_CODE = 44

# Placeholder URL used when user pastes job text into Telegram without any link.
# Kept non-empty so tracker dedup / hyperlink code doesn't choke on blanks.
PASTE_NO_URL_PLACEHOLDER = "paste://no-url"


# ── Tracker dedup check (avoid wasting LLM tokens) ──────────────────────────

def _already_processed(url: str) -> bool:
    """Check tracker.xlsx before calling LLM.

    Returns True if:
    - a successful entry exists (ATS = real score), OR
    - a React-skip entry exists (ATS=SKIP, Sent='—') — permanently blocked.
    FAIL and plain SKIP rows do NOT block, so those jobs can be retried.
    Skipped entirely when --force flag is used or URL is the paste placeholder.
    """
    if _SKIP_DEDUP:
        return False
    if not url or url == PASTE_NO_URL_PLACEHOLDER:
        return False
    try:
        from hunter.services.tracker_service import should_skip_url
        return should_skip_url(url)
    except Exception:
        return False


# ── Telegram helper ───────────────────────────────────────────────────────────

def notify(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[apply_agent] Telegram error: {e}")


# ── Cover letter review loop ─────────────────────────────────────────────────

_REVIEW_SYSTEM = (
    "You are a professional recruiter reviewing cover letters. "
    "Respond ONLY with a JSON object, no other text."
)


_BANNED_OPENER_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\s*the best\s+\w[\w\s-]*?\bI know\b", re.IGNORECASE),
    re.compile(r"^\s*great\s+\w[\w\s-]*?\bdon['’]t just\b", re.IGNORECASE),
    re.compile(r"\bis what I bring to\b", re.IGNORECASE),
    re.compile(r"\bis exactly what\s+.{1,80}?(?:requires|needs|is looking for|is after)\b", re.IGNORECASE),
    re.compile(r"\bexactly the challenges you['’]?re\s+(?:facing|tackling|solving)\b", re.IGNORECASE),
    re.compile(r"^\s*I am writing to\b", re.IGNORECASE),
    re.compile(r"^\s*I am excited to\b", re.IGNORECASE),
    re.compile(r"^\s*I am passionate about\b", re.IGNORECASE),
    re.compile(r"^\s*As a (?:lifelong|passionate|dedicated|seasoned|highly[- ]skilled)\b", re.IGNORECASE),
    re.compile(r"^\s*Engineering teams\s+succeed\b", re.IGNORECASE),
)


def _opener_banlist_hits(letter: str) -> list[str]:
    """Return list of banned patterns matched in the letter's opener (first sentence)."""
    if not letter:
        return []
    # Consider first ~250 chars — safely covers any reasonable opening sentence.
    head = letter.strip()[:250]
    # Take the first sentence for strict "^" checks; keep full head for mid-sentence patterns.
    first_sentence = re.split(r"[.!?]\s", head, maxsplit=1)[0]
    hits: list[str] = []
    for pat in _BANNED_OPENER_PATTERNS:
        target = first_sentence if pat.pattern.startswith(r"^") else head
        if pat.search(target):
            hits.append(pat.pattern)
    return hits


def _review_cover_letter(letter: str) -> tuple[str, int]:
    """Send cover letter to LLM for AI-language review.

    Returns (rewritten_or_original, score_1_to_10).
    Score > 6 = acceptable. Score ≤ 6 = rewrites.
    If the opener matches a banned pattern, the score is capped at 4 to force a rewrite.
    Skips if no API key available.
    """
    if not LLM_API_KEY:
        return letter, 10

    banlist_hits = _opener_banlist_hits(letter)
    banlist_note = ""
    if banlist_hits:
        banlist_note = (
            "\n\nCRITICAL: the opening sentence of this letter matches one or more BANNED "
            "patterns listed in the system prompt (thought-leadership lectures, "
            "'is what I bring to', 'is exactly what X needs', 'caught my attention' without "
            "a specific posting detail, etc). Score MUST be <= 4 and you MUST rewrite the "
            "opener following the Concrete-fact-about-THEM shape (reference one specific "
            "thing from the job posting or company and tie one of the candidate's facts to "
            "it in the same sentence)."
        )

    user_msg = (
        "Review this cover letter for AI-generated language patterns.\n"
        "Score it 1-10 where:\n"
        "  1-4: clearly AI-written (generic openers, formulaic, buzzwords)\n"
        "  5-6: borderline — some generic phrases, some specific\n"
        "  7-10: natural, human, specific to the job\n\n"
        "Penalise: sentences that could apply to ANY job, repetitive rhythm, "
        "opener starting with 'I', generic buzzwords, opener that would survive "
        "a company-name swap.\n\n"
        "If score ≤ 6, provide a rewritten version that removes all generic phrases "
        "and makes every sentence concrete and specific to this job/company. "
        "Keep paragraphs 2-4 intact unless they obviously contradict or repeat the new opener."
        f"{banlist_note}\n\n"
        'Respond with JSON only: {"score": <int 1-10>, "rewrite": <rewritten string or null if score > 6>}\n\n'
        f"Cover letter:\n{letter}"
    )

    try:
        from llm_client import call_llm
        result = call_llm(
            system_prompt=_REVIEW_SYSTEM,
            user_message=user_msg,
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=2000,
        )
        score = int(result.get("score", 10))
        rewrite = result.get("rewrite")
        if banlist_hits:
            score = min(score, 4)
            print(f"[apply_agent] Opener banlist hits: {banlist_hits} — forcing score={score}")
        if score <= 6 and isinstance(rewrite, str) and len(rewrite) > 50:
            return rewrite.strip(), score
        return letter, score
    except Exception as e:
        print(f"[apply_agent] Cover letter review error: {e}")
        return letter, 10


def _translate_cover_letter_pl(letter_en: str) -> str:
    """Re-translate rewritten EN cover letter to Polish."""
    if not LLM_API_KEY:
        return ""
    try:
        from llm_client import call_llm
        result = call_llm(
            system_prompt="You are a professional translator. Respond ONLY with JSON.",
            user_message=(
                "Translate this cover letter to natural, professional Polish. "
                "Preserve tone, structure and all specific details. "
                'Respond with JSON only: {"cover_letter_pl": "<translation>"}\n\n'
                f"Letter:\n{letter_en}"
            ),
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=2000,
        )
        pl = result.get("cover_letter_pl", "")
        return pl if isinstance(pl, str) and len(pl) > 50 else ""
    except Exception as e:
        print(f"[apply_agent] PL re-translation error: {e}")
        return ""


def _cover_letter_review_loop(content: dict, max_rounds: int = 3) -> dict:
    """Review and optionally rewrite cover_letter_en up to max_rounds times.

    Updates cover_letter_pl if EN was changed.
    """
    letter = content.get("cover_letter_en", "")
    if not letter:
        return content

    original_en = letter
    final_score = 10

    for attempt in range(1, max_rounds + 1):
        new_letter, score = _review_cover_letter(letter)
        final_score = score
        print(f"[apply_agent] Cover letter review round {attempt}/{max_rounds}: score={score}")
        letter = new_letter
        if score > 6:
            break

    content["cover_letter_en"] = letter
    if letter != original_en:
        print(f"[apply_agent] Cover letter rewritten (final score={final_score}), updating PL translation…")
        notify(f"✍️ Cover letter rewritten after review (score was {final_score}/10)")
        pl = _translate_cover_letter_pl(letter)
        if pl:
            content["cover_letter_pl"] = pl

    return content


# ── Output folder logic ──────────────────────────────────────────────────────

def compute_output_folder(company_name: str) -> Path:
    """Compute Applications/{date}/{Company} with _2, _3 suffixes on company name if needed."""
    today = date.today().strftime("%Y-%m-%d")
    date_dir = APPLICATIONS_DIR / today
    base = date_dir / company_name
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = date_dir / f"{company_name}_{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_folder_company(name: str) -> str:
    """Safe folder segment from company name (Windows / macOS)."""
    s = _INVALID_FOLDER_CHARS.sub("_", (name or "").strip())
    s = s.strip("._ ")[:120] or "Unknown"
    return s


def _handle_jobleads_fetch_blocked(url: str, err: str) -> None:
    """Stub job_posting.txt + MANUAL tracker row; Telegram instructs user; process exits 44."""
    from hunter.tracker import (
        add_manual_jobleads_pending,
        has_manual_pending,
        lookup_url,
        manual_jobleads_job_posting_path,
    )
    from job_fetch.jobleads import JOBLEADS_PASTE_MARKER

    if has_manual_pending(url):
        jp = manual_jobleads_job_posting_path(url)
        hint = f"\nФайл: <code>{jp}</code>" if jp else ""
        notify(
            "📋 <b>JobLeads — запись MANUAL уже есть</b>\n"
            "Вставь текст вакансии в <code>job_posting.txt</code> (ниже маркера) и снова запусти apply "
            "с той же ссылкой.\n"
            f"🔗 {url}{hint}\n"
            "<i>Dedup: строка уже в tracker.xlsx</i>"
        )
        print(f"[apply_agent] MANUAL_PENDING (existing) exit={APPLY_MANUAL_EXIT_CODE}")
        sys.exit(APPLY_MANUAL_EXIT_CODE)

    if lookup_url(url):
        notify(
            "📋 <b>JobLeads — URL уже в tracker.xlsx</b> (дедуп).\n"
            f"🔗 {url}\n"
            "Если там статус FAIL и хочешь MANUAL-режим — удали эту строку в Excel и повтори."
        )
        print(f"[apply_agent] MANUAL_PENDING (URL already tracked) exit={APPLY_MANUAL_EXIT_CODE}")
        sys.exit(APPLY_MANUAL_EXIT_CODE)

    company_folder = _sanitize_folder_company(_APPLY_META_COMPANY or "Unknown")
    title = (_APPLY_META_TITLE or "Unknown").strip() or "Unknown"
    output_folder = compute_output_folder(company_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    stub = output_folder / "job_posting.txt"
    stub.write_text(
        f"URL: {url}\n\n"
        f"Company (from listing): {_APPLY_META_COMPANY or '—'}\n"
        f"Title (from listing): {_APPLY_META_TITLE or '—'}\n\n"
        "JobLeads blocks automatic download (Cloudflare).\n"
        "Open the job in your browser, copy the full posting, and paste it below the marker line.\n\n"
        f"{JOBLEADS_PASTE_MARKER}\n\n",
        encoding="utf-8",
    )

    written = add_manual_jobleads_pending(
        url=url,
        company=_APPLY_META_COMPANY or "Unknown",
        title=title,
        folder_abs=output_folder,
    )
    folder_display = str(output_folder).replace("\\", "/")
    notify(
        "📋 <b>JobLeads — нужно вручную дописать описание</b>\n\n"
        "Страница недоступна для бота (Cloudflare). Создана строка в <b>tracker.xlsx</b> "
        "(ATS = <code>MANUAL</code>) и папка:\n"
        f"📁 <code>{folder_display}/</code>\n\n"
        "1. Открой <code>job_posting.txt</code> в этой папке\n"
        "2. Вставь полный текст вакансии <b>под</b> строкой-маркером\n"
        "3. Сохрани файл и снова запусти apply <b>с той же ссылкой</b>\n\n"
        f"🔗 {url}\n\n"
        f"<pre>{(err or '')[:280]}</pre>"
        + ("" if written else "\n\n<i>Строка tracker не добавлена (редкий конфликт).</i>"),
    )
    print(f"[apply_agent] MANUAL_PENDING exit={APPLY_MANUAL_EXIT_CODE} tracker_row={written}")
    sys.exit(APPLY_MANUAL_EXIT_CODE)


def validate_content(data: dict) -> list[str]:
    """Return list of missing/invalid fields."""
    errors = []
    for key in REQUIRED_JSON_KEYS:
        if key not in data or data[key] is None:
            errors.append(f"Missing field: {key}")

    resume = data.get("resume_en")
    if isinstance(resume, dict):
        for sub in ("summary", "skills", "experience", "education"):
            if sub not in resume:
                errors.append(f"resume_en missing: {sub}")
        if isinstance(resume.get("experience"), list) and len(resume["experience"]) < 3:
            errors.append(f"resume_en.experience has only {len(resume['experience'])} jobs (expected 6)")
    else:
        errors.append("resume_en is not a dict")

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# API MODE — fetch job → LLM → content.json → generate_docs
# ══════════════════════════════════════════════════════════════════════════════

def main_api(url: str, paste_text: str = "") -> None:
    url_display = url if url and url != PASTE_NO_URL_PLACEHOLDER else "(pasted text, no URL)"
    print(f"\n[apply_agent] API mode | URL: {url_display}\n")

    if _already_processed(url):
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

    # Step 1 — Get job text: either use pasted text (Telegram paste flow) or fetch
    if paste_text:
        job_text = paste_text
        print(f"[apply_agent] Step 1: Using pasted text ({len(job_text)} chars, no fetch)")
    else:
        print("[apply_agent] Step 1: Fetching job posting...")
        try:
            from job_fetch import fetch_job_text
            job_text = fetch_job_text(url)
            print(f"[apply_agent] Fetched {len(job_text)} chars of job text")
        except Exception as e:
            if "jobleads.com" in url.lower():
                _handle_jobleads_fetch_blocked(url, str(e))
            notify(f"❌ <b>Failed to fetch job posting</b>\nURL: {url}\n\n<pre>{str(e)[:400]}</pre>")
            print(f"[apply_agent] FETCH ERROR: {e}")
            sys.exit(1)

    # Step 2 — Read system prompt (instructions + candidate profile)
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

    # Step 3 — Call LLM
    print(f"[apply_agent] Step 2: Calling {LLM_PROVIDER}/{LLM_MODEL}...")
    try:
        from llm_client import call_llm, LLMError
        url_hint = (
            url
            if url and url != PASTE_NO_URL_PLACEHOLDER
            else "(none — text pasted directly by user)"
        )
        content = call_llm(
            system_prompt=system_prompt,
            user_message=f"Here is the job posting to analyze:\n\n{job_text}\n\nOriginal URL: {url_hint}",
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
        # Continue anyway — partial content is better than nothing

    # Step 4.5 — Skip React-only jobs (no Angular mentioned in stack)
    stack = (content.get("stack") or "").lower()
    if "react" in stack and "angular" not in stack:
        notify(
            f"⏭ <b>Skipped — React-only stack</b>\n"
            f"🔗 {url}\n"
            f"Stack: {content.get('stack', '?')}"
        )
        print(f"[apply_agent] SKIP — React-only stack: {content.get('stack')}")
        try:
            from hunter.tracker import add_react_skipped
            add_react_skipped(content, url)
        except Exception as e:
            print(f"[apply_agent] Warning: could not write React-skip to tracker: {e}")
        return

    # Step 4.6 — Review and optionally rewrite cover letter (up to 3 rounds)
    print("[apply_agent] Step 4.6: Reviewing cover letter for AI-language patterns...")
    content = _cover_letter_review_loop(content)

    # Step 5 — Compute output folder and finalize JSON
    company = content.get("company_name", "Unknown")
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
        job_posting_path.write_text(
            url_line + job_text,
            encoding="utf-8",
        )
        print(f"[apply_agent] Saved job posting -> {job_posting_path.name}")
    except Exception as e:
        print(f"[apply_agent] Warning: could not save job_posting.txt: {e}")

    # Step 7 — Run generate_docs.py
    use_full = _FULL_MODE
    gen_cmd = build_generate_docs_cmd(
        generate_docs_script=GENERATE_DOCS_SCRIPT,
        content_json_path=content_path,
        use_full=use_full,
        force=_SKIP_DEDUP,
        python_executable=sys.executable,
    )
    mode_label = "FULL" if use_full else "SHORT"
    print(f"[apply_agent] Step 4: Generating docs ({mode_label})...")
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
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[generate_docs] STDERR:", result.stderr, file=sys.stderr)
        if result.returncode != 0:
            notify(
                f"⚠️ <b>generate_docs.py failed</b>\n"
                f"content.json written OK, but doc generation had issues.\n"
                f"Folder: <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>"
            )
    except subprocess.TimeoutExpired:
        print("[apply_agent] generate_docs.py timed out (120s)")

    # Step 8 — Notify success
    created_files = list(output_folder.glob("*.docx")) + list(output_folder.glob("*.pdf"))
    if created_files:
        file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
        ats = content.get("ats_score", "?")
        notify(
            f"✅ <b>Docs ready!</b>\n\n"
            f"📁 <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>\n\n"
            f"{file_names}\n\n"
            f"ATS: {ats}% | Stack: {content.get('stack', '?')}\n"
            f"Via: API ({LLM_MODEL})\n"
            f"Review and send when ready."
        )
        print(f"\n[apply_agent] Done! Folder: Applications/{output_folder.parent.name}/{output_folder.name}/ ({len(created_files)} files)")
    else:
        notify(
            f"⚠️ <b>content.json OK but no docs generated</b>\n"
            f"📁 <code>Applications/{output_folder.parent.name}/{output_folder.name}/</code>\n"
            f"Run manually: python generate_docs.py \"{content_path}\""
        )
        print(f"\n[apply_agent] WARNING: No .docx/.pdf files found, but content.json is saved.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# CLI MODE — original Claude Code CLI approach (fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _get_existing_folders() -> set[str]:
    """Return relative paths of all known application folders.

    New structure:  Applications/{date}/{Company}  → stored as "{date}/{Company}"
    Legacy flat:    Applications/{Company}_{date}   → stored as "{Company}_{date}"
    """
    if not APPLICATIONS_DIR.exists():
        return set()
    result: set[str] = set()
    for item in APPLICATIONS_DIR.iterdir():
        if not item.is_dir():
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}$", item.name):
            # Date subfolder (new structure)
            for sub in item.iterdir():
                if sub.is_dir():
                    result.add(f"{item.name}/{sub.name}")
        else:
            # Legacy flat folder
            result.add(item.name)
    return result


def _find_new_folder(before: set[str], timeout: int = 300) -> str | None:
    """Detect a newly created application folder.

    Searches today's date subfolder first (new structure), then falls back to
    scanning the flat Applications/ directory (legacy / CLI created outside date dir).
    Returns a relative path like "2026-04-14/CompanyName" for new structure, or
    a plain folder name for legacy.
    """
    today = date.today().strftime("%Y-%m-%d")
    date_dir = APPLICATIONS_DIR / today
    run_start = time.time()
    deadline = run_start + max(timeout, 0)
    while True:
        # New structure: check today's date subfolder
        if date_dir.exists():
            for folder in date_dir.iterdir():
                if not folder.is_dir():
                    continue
                rel = f"{today}/{folder.name}"
                if rel not in before:
                    return rel
                if folder.stat().st_mtime >= run_start - 5:
                    return rel
        # Legacy fallback: check Applications/ directly
        if APPLICATIONS_DIR.exists():
            for folder in APPLICATIONS_DIR.iterdir():
                if not folder.is_dir():
                    continue
                if re.match(r"^\d{4}-\d{2}-\d{2}$", folder.name):
                    continue  # Skip date dirs already handled above
                if folder.name not in before:
                    return folder.name
                if folder.stat().st_mtime >= run_start - 5:
                    return folder.name
        if time.time() >= deadline:
            break
        time.sleep(5)
    return None


def main_cli(url: str) -> None:
    print(f"\n[apply_agent] CLI mode | URL: {url}\n")

    if _already_processed(url):
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

    folders_before = _get_existing_folders()

    # Pre-fetch job text via JSON API so Claude doesn't have to WebFetch
    apply_input = url
    job_text: str | None = None
    try:
        from job_fetch import fetch_job_text
        job_text = fetch_job_text(url)
        if job_text and len(job_text) > 100:
            apply_input = f"URL: {url}\n\n{job_text}"
            print(f"[apply_agent] Pre-fetched {len(job_text)} chars via JSON API")
    except Exception as e:
        print(f"[apply_agent] Pre-fetch failed ({e}), passing raw URL to Claude")

    cmd = ["claude", "-p", "--dangerously-skip-permissions", f"/apply {apply_input}"]
    print(f"[apply_agent] Running claude CLI...\n")

    result = None
    new_folder_timeout = None

    for attempt in range(1, CLI_MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_DIR),
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            new_folder_on_timeout = _find_new_folder(folders_before, timeout=0)
            if new_folder_on_timeout:
                print(f"\n[apply_agent] Claude timed out but folder created: {new_folder_on_timeout}")
                result = None
                new_folder_timeout = new_folder_on_timeout
                break
            else:
                notify(f"⏱ <b>apply_agent timeout (10 min)</b>\nURL: {url}")
                print(f"\n[apply_agent] Timeout — no folder created.")
                raise ApplyError("CLI timeout — no folder created")

        if result.returncode == 0:
            break

        output = (result.stderr or result.stdout or "")
        is_overloaded = "overloaded" in output.lower() or "529" in output

        if is_overloaded and attempt < CLI_MAX_RETRIES:
            wait = CLI_RETRY_DELAY * attempt
            print(f"[apply_agent] Claude overloaded (529), retry {attempt}/{CLI_MAX_RETRIES} in {wait}s...")
            notify(f"⚠️ Claude overloaded (529), retry {attempt}/{CLI_MAX_RETRIES} in {wait}s...")
            time.sleep(wait)
            continue

        # Permanent failure — not overloaded, or last attempt
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[apply_agent] STDERR:", result.stderr, file=sys.stderr)
        error_detail = (result.stderr or result.stdout or "no output")[:800]
        notify(
            f"❌ <b>apply_agent CLI failed</b>\n"
            f"URL: {url}\n"
            f"Exit code: {result.returncode}"
            + (f" (attempt {attempt}/{CLI_MAX_RETRIES})" if attempt > 1 else "")
            + f"\n\n<pre>{error_detail}</pre>"
        )
        print(f"\n[apply_agent] claude exited with code {result.returncode}")
        raise ApplyError(f"CLI exited with code {result.returncode}")

    if result is not None and result.returncode == 0:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[apply_agent] STDERR:", result.stderr, file=sys.stderr)

    new_folder = new_folder_timeout or _find_new_folder(folders_before, timeout=30)

    if new_folder:
        folder_path = APPLICATIONS_DIR / new_folder

        # Save raw job posting text (free — no LLM required)
        if job_text:
            try:
                job_posting_path = folder_path / "job_posting.txt"
                job_posting_path.write_text(f"URL: {url}\n\n{job_text}", encoding="utf-8")
                print(f"[apply_agent] Saved job posting -> {job_posting_path.name}")
            except Exception as e:
                print(f"[apply_agent] Warning: could not save job_posting.txt: {e}")

        # Check React-only stack from content.json written by Claude
        content_json_path = folder_path / "content.json"
        if content_json_path.exists():
            try:
                _cli_content = json.loads(content_json_path.read_text(encoding="utf-8"))

                # React-only skip
                _cli_stack = (_cli_content.get("stack") or "").lower()
                if "react" in _cli_stack and "angular" not in _cli_stack:
                    notify(
                        f"⏭ <b>Skipped — React-only stack</b>\n"
                        f"🔗 {url}\n"
                        f"Stack: {_cli_content.get('stack', '?')}"
                    )
                    print(f"[apply_agent] SKIP — React-only stack: {_cli_content.get('stack')}")
                    try:
                        from hunter.tracker import add_react_skipped
                        add_react_skipped(_cli_content, url)
                    except Exception as e:
                        print(f"[apply_agent] Warning: could not write React-skip to tracker: {e}")
                    return

                # Cover letter review — rewrite if too AI-sounding, then regenerate docs
                print("[apply_agent] Cover letter review (CLI mode)...")
                _cli_content_reviewed = _cover_letter_review_loop(_cli_content)
                if _cli_content_reviewed.get("cover_letter_en") != _cli_content.get("cover_letter_en"):
                    content_json_path.write_text(
                        json.dumps(_cli_content_reviewed, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print("[apply_agent] Regenerating docs with rewritten cover letter...")
                    gen_cmd = build_generate_docs_cmd(
                        generate_docs_script=GENERATE_DOCS_SCRIPT,
                        content_json_path=content_json_path,
                        use_full=_FULL_MODE,
                        force=_SKIP_DEDUP,
                        python_executable=sys.executable,
                    )
                    subprocess.run(gen_cmd, cwd=str(PROJECT_DIR), check=False)

            except Exception as e:
                print(f"[apply_agent] CLI post-processing error: {e}")

        created_files = list(folder_path.glob("*.docx")) + list(folder_path.glob("*.pdf"))
        if created_files:
            file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
            notify(
                f"✅ <b>Docs ready!</b>\n\n"
                f"📁 <code>Applications/{new_folder}/</code>\n\n"
                f"{file_names}\n\n"
                f"Via: CLI (Pro subscription)\n"
                f"Review and send when ready."
            )
            print(f"\n[apply_agent] Done! Folder: Applications/{new_folder}/ ({len(created_files)} files)")
        else:
            notify(
                f"⚠️ <b>Folder created but no docs found!</b>\n"
                f"📁 <code>Applications/{new_folder}/</code>\n"
                f"Check the folder for partial output."
            )
            print(f"\n[apply_agent] WARNING: Folder created but no .docx/.pdf files found.")
            raise ApplyError("Folder created but no docs found")
    else:
        stdout_preview = (result.stdout or "").strip()[:600] if result else ""
        notify(
            f"❌ <b>CLI exited 0 but no folder created</b>\n"
            f"🔗 {url}\n\n"
            + (f"Claude output:\n<pre>{stdout_preview}</pre>" if stdout_preview else "No CLI output captured.")
        )
        print(f"\n[apply_agent] FAIL: claude exited 0 but no new folder was created.")
        raise ApplyError("No output folder created")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def _is_cli_available() -> bool:
    """Check if Claude CLI is installed and logged in (Pro subscription)."""
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        if r.returncode != 0:
            return False
        output = (r.stdout + r.stderr).lower()
        if "not logged in" in output or "unauthorized" in output:
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


class ApplyError(RuntimeError):
    """Raised when an apply attempt fails and fallback should be tried."""


def main(
    url: str,
    force_cli: bool = False,
    force: bool = False,
    full: bool = False,
    paste_text: str = "",
) -> None:
    if force:
        global _SKIP_DEDUP
        _SKIP_DEDUP = True
    if full:
        global _FULL_MODE
        _FULL_MODE = True

    # Pasted text skips fetch, so there is nothing for CLI mode (claude -p /apply) to help with.
    # CLI mode also can't write structured content.json reliably for the pasted branch — force API.
    if paste_text:
        if not LLM_API_KEY:
            print("[apply_agent] ERROR: --paste-file requires LLM_API_KEY (CLI mode not supported).")
            sys.exit(1)
        main_api(url or PASTE_NO_URL_PLACEHOLDER, paste_text=paste_text)
        return

    if force_cli or APPLY_USE_CLI:
        main_cli(url)
        return

    cli_ok = _is_cli_available()
    if cli_ok:
        print("[apply_agent] Claude CLI detected (Pro subscription) — trying CLI first")
        try:
            main_cli(url)
            return
        except (ApplyError, SystemExit) as e:
            if LLM_API_KEY:
                print(f"[apply_agent] CLI failed ({e}), falling back to API mode")
                notify(f"🔄 CLI failed — retrying via API\n🔗 {url}")
            else:
                print(f"[apply_agent] CLI failed and no API key available")
                sys.exit(1)

    if LLM_API_KEY:
        main_api(url)
    else:
        print("[apply_agent] ERROR: No Claude CLI login and no LLM_API_KEY set. Cannot proceed.")
        sys.exit(1)


def parse_apply_cli_argv(argv: list[str]) -> tuple[str, bool, bool, bool, str, str, str]:
    """Parse argv (including script name) → url, force_cli, force, full, company, title, paste_file."""
    args = argv[1:]
    force_cli = "--cli" in args
    force = "--force" in args
    full = "--full" in args
    company, title, paste_file = "", "", ""
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--company" and i + 1 < len(args):
            company = args[i + 1]
            i += 2
            continue
        if a == "--title" and i + 1 < len(args):
            title = args[i + 1]
            i += 2
            continue
        if a == "--paste-file" and i + 1 < len(args):
            paste_file = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        pos.append(a)
        i += 1
    url = pos[0] if pos else ""
    return url, force_cli, force, full, company, title, paste_file


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python apply_agent.py <job_url> [--cli] [--force] [--full] "
            "[--company NAME] [--title TITLE] [--paste-file PATH]",
        )
        sys.exit(1)

    url, force_cli, force, full, co, ti, paste_file = parse_apply_cli_argv(sys.argv)

    paste_text = ""
    if paste_file:
        try:
            paste_text = Path(paste_file).read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"[apply_agent] ERROR: failed to read paste file {paste_file}: {e}")
            sys.exit(1)
        if not paste_text:
            print(f"[apply_agent] ERROR: paste file {paste_file} is empty.")
            sys.exit(1)

    if not url and not paste_text:
        print(
            "Usage: python apply_agent.py <job_url> [--cli] [--force] [--full] "
            "[--paste-file PATH] ...",
        )
        sys.exit(1)

    _APPLY_META_COMPANY = co
    _APPLY_META_TITLE = ti
    main(url, force_cli=force_cli, force=force, full=full, paste_text=paste_text)
