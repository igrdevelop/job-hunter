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
    TELEGRAM_SEND_DOCS,
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


# Telegram Bot API: max document size 50MB (https://core.telegram.org/bots/api#senddocument)
_TELEGRAM_DOC_MAX_BYTES = 50 * 1024 * 1024
_TELEGRAM_SEND_DOC_TIMEOUT = 120

# Shown after React-only auto-skip — /force already sets --force (bypasses this filter).
_REACT_SKIP_FORCE_HINT = (
    "\n\n📌 <b>Нужны документы всё равно?</b> В Telegram:\n"
    "• <code>/force</code> и тот же URL (строка 🔗 выше), или\n"
    "• <code>/force</code> и сразу под ним полный текст вакансии (как при обычной вставке).\n"
    "Так включается <code>--force</code> (без React-only); для JobLeads подтянется "
    "<code>job_posting.txt</code>, если ты его уже заполнял."
)


def send_telegram_documents(paths: list[Path]) -> None:
    """Send generated files to Telegram as documents (separate from notify text)."""
    if not TELEGRAM_SEND_DOCS or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not paths:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    failed: list[str] = []
    sent = 0
    for p in sorted(paths, key=lambda x: x.name):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
            if size > _TELEGRAM_DOC_MAX_BYTES:
                print(
                    f"[apply_agent] Skipping Telegram doc (over 50MB): {p.name}",
                )
                failed.append(f"{p.name} (over 50MB cap)")
                continue
            with p.open("rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": TELEGRAM_CHAT_ID},
                    files={"document": (p.name, f, "application/octet-stream")},
                    timeout=_TELEGRAM_SEND_DOC_TIMEOUT,
                )
            data = r.json() if r.content else {}
            if r.status_code != 200 or not data.get("ok"):
                desc = data.get("description", r.text[:200])
                print(f"[apply_agent] sendDocument failed for {p.name}: {desc}")
                failed.append(p.name)
            else:
                sent += 1
        except Exception as e:
            print(f"[apply_agent] sendDocument error for {p.name}: {e}")
            failed.append(p.name)
    if failed:
        short = "\n".join(f"  • {x}" for x in failed[:15])
        more = f"\n  … +{len(failed) - 15} more" if len(failed) > 15 else ""
        notify(
            f"⚠️ <b>Some files were not sent to Telegram</b>\n{short}{more}",
        )
    elif sent:
        print(f"[apply_agent] Sent {sent} file(s) to Telegram")


# ── Cover letter review loop ─────────────────────────────────────────────────

_REVIEW_SYSTEM = (
    "You are a professional recruiter reviewing cover letters for a senior Angular candidate. "
    "Accept classic business-letter phrasing (I am writing to…, thank you, I look forward to…). "
    "Still penalise generic resume-site tone, missing posting specifics, and weak metrics. "
    "Respond ONLY with a JSON object, no other text."
)


_BANNED_OPENER_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\s*the best\s+\w[\w\s-]*?\bI know\b", re.IGNORECASE),
    re.compile(r"^\s*great\s+\w[\w\s-]*?\bdon['\u2018\u2019]t just\b", re.IGNORECASE),
    re.compile(r"\bis what I bring to\b", re.IGNORECASE),
    re.compile(r"\bis exactly what\s+.{1,80}?(?:requires|needs|is looking for|is after)\b", re.IGNORECASE),
    re.compile(
        r"\bexactly the challenges you['\u2018\u2019]?re\s+(?:facing|tackling|solving)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*I['\u2018\u2019]ve had the opportunity to\b", re.IGNORECASE),
    re.compile(r"^\s*I had the opportunity to\b", re.IGNORECASE),
    re.compile(r"^\s*I am passionate about\b", re.IGNORECASE),
    re.compile(r"^\s*As a (?:lifelong|passionate|dedicated|seasoned|highly[- ]skilled)\b", re.IGNORECASE),
    re.compile(r"^\s*Engineering teams\s+succeed\b", re.IGNORECASE),
    re.compile(
        r"^\s*Working with\s+\w.{0,60}for the past\s+\w.{0,40}I (?:have seen|learned|observed|know)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*Having\s+\w.{0,30}for\s+\d+\s+years?\b", re.IGNORECASE),
)

# Resume-site / vibe-padding junk. Note: "excited to" and "thrilled to" are banned as empty filler.
_BANNED_BODY_PHRASES: tuple[re.Pattern, ...] = (
    re.compile(r"\baligns?\s+seamlessly\b", re.IGNORECASE),
    re.compile(r"\baligns?\s+(?:perfectly\s+)?with\s+my\s+background\b", re.IGNORECASE),
    re.compile(r"\baligns?\s+perfectly\s+with\b", re.IGNORECASE),
    re.compile(r"\bstandards\s+of\s+excellence\b", re.IGNORECASE),
    re.compile(r"\btechnical\s+acumen\b", re.IGNORECASE),
    re.compile(r"\besteemed\s+team\b", re.IGNORECASE),
    re.compile(r"\bharnessing\s+both\b", re.IGNORECASE),
    re.compile(r"I['\u2018\u2019]ve had the opportunity to closely follow\b", re.IGNORECASE),
    re.compile(r"\bhad the opportunity to closely follow\b", re.IGNORECASE),
    re.compile(r"\bperfect\s+(?:fit|match)\b", re.IGNORECASE),
    re.compile(r"\bideal\s+match\b", re.IGNORECASE),
    re.compile(r"\bexactly\s+what\s+I['\u2018\u2019]?m\s+looking\s+for\b", re.IGNORECASE),
    re.compile(r"\bpassionate\s+about\b", re.IGNORECASE),
    re.compile(r"\bthrilled\s+to\b", re.IGNORECASE),
    re.compile(r"\bexcited\s+to\b", re.IGNORECASE),
    re.compile(r"\bproven\s+track\s+record\b", re.IGNORECASE),
    re.compile(r"\bcomfortable\s+owning\b", re.IGNORECASE),
    re.compile(r"\bcomfortable\s+with\b", re.IGNORECASE),
    re.compile(r"\bseamlessly\b", re.IGNORECASE),
    re.compile(r"\bsynergy\b", re.IGNORECASE),
    re.compile(r"\bleverage\b", re.IGNORECASE),
)

_BANNED_CTA_PHRASES: tuple[re.Pattern, ...] = (
    re.compile(r"I would welcome the opportunity to contribute", re.IGNORECASE),
    re.compile(r"Please find my CV attached", re.IGNORECASE),
    re.compile(r"Feel free to reach out", re.IGNORECASE),
    re.compile(r"I look forward to hearing from you", re.IGNORECASE),
    re.compile(r"Thank you for considering my application", re.IGNORECASE),
)

_CL_WORD_MIN, _CL_WORD_MAX = 220, 280
_CL_BODY_PARA_MIN, _CL_BODY_PARA_MAX = 3, 5

_METRIC_RE = re.compile(
    r"\b\d+\s*%"                                          # percentages
    r"|\b\d{3,}\b"                                        # 3+ digit numbers (300, 1000)
    r"|\b\d+\s*(?:x\b|\+\b)"                             # multipliers / "10x", "50+"
    r"|\b\d+\+?\s*(?:people|developers?|engineers?|banks?|apps?|applications?|"
    r"clients?|members?|months?|weeks?|hours?|microservices?|services?|projects?|"
    r"repos?|repositories?|teams?|companies|countries)\b",
    re.IGNORECASE,
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


def _body_banlist_hits(letter: str) -> list[str]:
    return [pat.pattern for pat in _BANNED_BODY_PHRASES if pat.search(letter)]


def _last_paragraph_text(letter: str) -> str:
    """Final paragraph block (split on blank lines) for CTA checks."""
    text = (letter or "").strip()
    if not text:
        return ""
    chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if chunks:
        return chunks[-1]
    return text


def _count_body_paragraphs(letter: str) -> int:
    """Body paragraphs after salutation (Dear ...)."""
    text = (letter or "").strip()
    if not text:
        return 0
    chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(chunks) >= 2:
        if chunks[0].lower().startswith("dear "):
            return len(chunks) - 1
        return len(chunks)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return 0
    start = 1 if lines[0].lower().startswith("dear ") else 0
    return max(0, len(lines) - start)


def _cta_banlist_hits(letter: str) -> list[str]:
    last_para = _last_paragraph_text(letter)
    return [pat.pattern for pat in _BANNED_CTA_PHRASES if pat.search(last_para)]


def _count_words(text: str) -> int:
    return len(text.split()) if text else 0


def _count_metrics(letter: str) -> int:
    cleaned = re.sub(r"\b10\+\s*years?\b", "", letter, flags=re.IGNORECASE)
    return len(_METRIC_RE.findall(cleaned))


def _review_cover_letter(letter: str) -> tuple[str, int]:
    """Review cover letter against quality gates; rewrite if any gate fails.

    Gate 1: word count (_CL_WORD_MIN–_CL_WORD_MAX)
    Gate 2: ≥2 numeric metrics in body (excluding "10+ years")
    Gate 3: opener not in banned patterns
    Gate 4: no banned body phrases
    Gate 5: unfamiliar tech uses only safe verbs (checked by LLM)
    Gate 6: CTA — no banned fluff phrases in final paragraph
    Gate 7: 3–5 body paragraphs after Dear …

    Returns (rewritten_or_original, score_1_to_10). Score > 6 = acceptable.
    Skips if no API key available.
    """
    if not LLM_API_KEY:
        return letter, 10

    opener_hits = _opener_banlist_hits(letter)
    body_hits = _body_banlist_hits(letter)
    cta_hits = _cta_banlist_hits(letter)
    wc = _count_words(letter)
    metric_count = _count_metrics(letter)
    body_paras = _count_body_paragraphs(letter)

    forced_fails: list[str] = []
    if not (_CL_WORD_MIN <= wc <= _CL_WORD_MAX):
        forced_fails.append(f"Gate 1 — word count {wc} (target {_CL_WORD_MIN}-{_CL_WORD_MAX})")
    if metric_count < 2:
        forced_fails.append(f"Gate 2 — {metric_count} numeric metric(s) found (need ≥2)")
    if opener_hits:
        forced_fails.append(f"Gate 3 — banned opener: {opener_hits[0][:80]}")
    if body_hits:
        forced_fails.append(f"Gate 4 — banned body phrase: {body_hits[0][:80]}")
    if cta_hits:
        forced_fails.append(f"Gate 6 — banned CTA: {cta_hits[0][:80]}")
    if not (_CL_BODY_PARA_MIN <= body_paras <= _CL_BODY_PARA_MAX):
        forced_fails.append(
            f"Gate 7 — body paragraphs {body_paras} (target {_CL_BODY_PARA_MIN}-{_CL_BODY_PARA_MAX} after salutation)",
        )

    for msg in forced_fails:
        print(f"[apply_agent] Pre-check FAIL: {msg}")

    gate6_line = f"FAIL — {cta_hits[0][:60]}" if cta_hits else "PASS"

    gate_summary = (
        f"Gate 1 (word count {_CL_WORD_MIN}-{_CL_WORD_MAX}): "
        f"{'PASS' if _CL_WORD_MIN <= wc <= _CL_WORD_MAX else f'FAIL ({wc} words)'}\n"
        f"Gate 2 (≥2 numeric metrics): {'PASS' if metric_count >= 2 else f'FAIL ({metric_count} found)'}\n"
        f"Gate 3 (opener ban): {'FAIL — ' + opener_hits[0][:60] if opener_hits else 'PASS'}\n"
        f"Gate 4 (body banned phrases): {'FAIL — ' + body_hits[0][:60] if body_hits else 'PASS'}\n"
        f"Gate 6 (CTA): {gate6_line}\n"
        f"Gate 7 (body paragraphs {_CL_BODY_PARA_MIN}-{_CL_BODY_PARA_MAX}): "
        f"{'PASS' if _CL_BODY_PARA_MIN <= body_paras <= _CL_BODY_PARA_MAX else f'FAIL ({body_paras})'}"
    )

    critical_note = ""
    if forced_fails:
        critical_note = (
            "\n\nCRITICAL: pre-checks detected hard violations above. "
            "Score MUST be ≤ 4. Rewrite is mandatory, fixing ALL failing gates."
        )

    user_msg = (
        "Review this cover letter against the quality gates below.\n\n"
        f"Pre-check results:\n{gate_summary}\n\n"
        "Gate 5 (check yourself): any technology NOT in the core Angular frontend stack "
        "(Angular, TypeScript, RxJS, NgRx, Jest, Cypress, Jenkins, Webpack, Node.js, Git, "
        "SCSS, Bootstrap, AG Grid, Nx, Signals) must be introduced with SAFE verbs only: "
        "'familiar with', 'exposure to', 'adjacent to', 'ramping up on', 'transferable from'. "
        "DANGER verbs for unfamiliar tech ('spent N years on', 'led X', 'architected X', "
        "'built X from scratch', 'owned X') → Gate 5 FAIL.\n\n"
        "Score 1-10:\n"
        "  1-4: one or more gates fail\n"
        "  5-6: borderline\n"
        "  7-10: all gates pass, natural human voice, specific to this job\n\n"
        "Also penalise: opener that survives company-name swap, repetitive sentence rhythm, "
        "sentences that apply to any employer.\n\n"
        "If score ≤ 6, rewrite fixing ALL failing gates. Target a classic business letter: "
        "`Dear Hiring Manager,` then blank line, then 3-5 body paragraphs separated by blank lines. "
        "Intro may use standard phrases (I am writing to express…) but must still include a concrete "
        "detail from the job posting. Include ≥2 numeric metrics in the letter. "
        "Closing: 1 forward-looking sentence with a concrete anchor (time window, topic, timezone). "
        "BANNED CTAs: 'I look forward to hearing from you', 'Thank you for considering my application', "
        "'Please find my CV attached', 'Feel free to reach out', 'I would welcome the opportunity to contribute'. "
        "ALLOWED CTAs: 'I look forward to meeting you', 'I look forward to discussing [specific topic]'. "
        "No signature block in the letter text. "
        "Avoid resume-site filler: 'technical acumen', 'aligns seamlessly', 'aligns with my background', "
        "'aligns perfectly with', 'comfortable owning', 'comfortable with', 'proven track record', "
        "'leverage', 'synergy', 'excited to', 'passionate about', 'thrilled to'.\n"
        f"{critical_note}\n\n"
        'Respond JSON only: {"score": <int 1-10>, "fails": [<failing gate descriptions>], '
        '"rewrite": <rewritten letter string or null if score > 6>}\n\n'
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
        if forced_fails:
            score = min(score, 4)
            print(f"[apply_agent] Hard gate fails — capping score={score}, fails={forced_fails}")
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
                "Re-write this cover letter in natural, professional Polish — "
                "do NOT translate word-by-word. Use Polish idioms and collocations; "
                "avoid English calques (use 'zainteresowała mnie oferta' not "
                "'przyciągnęło mnie do oferty'; use 'chętnie omówię' not 'chętnie przyczynię się'). "
                "Keep the same structure (Dear Hiring Manager + 3-5 body paragraphs, \\n\\n between paragraphs), "
                "same specific facts and metrics, same tone. "
                'Respond with JSON only: {"cover_letter_pl": "<rewritten Polish text>"}\n\n'
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

    # Step 1.5 — Check for expired offer (skip before calling LLM)
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
        user_message = f"Here is the job posting to analyze:\n\n{job_text}\n\nOriginal URL: {url_hint}"

        # Force mode: instruct LLM to aggressively tailor the resume to the job posting
        if _SKIP_DEDUP:
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
        # Continue anyway — partial content is better than nothing

    # Step 4.4 — ATS boost pass (force mode only): if score < 95%, do a second LLM pass
    if _SKIP_DEDUP:
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

    # Step 4.5 — Skip React-only jobs (no Angular mentioned in stack)
    # Bypassed in force mode — user explicitly wants docs regardless of stack.
    stack = (content.get("stack") or "").lower()
    if "react" in stack and "angular" not in stack and not _SKIP_DEDUP:
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

    # Check for expired offer before spinning up Claude CLI
    if job_text:
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

                # React-only skip — bypassed in force mode
                _cli_stack = (_cli_content.get("stack") or "").lower()
                if "react" in _cli_stack and "angular" not in _cli_stack and not _SKIP_DEDUP:
                    notify(
                        f"⏭ <b>Skipped — React-only stack</b>\n"
                        f"🔗 {url}\n"
                        f"Stack: {_cli_content.get('stack', '?')}"
                        f"{_REACT_SKIP_FORCE_HINT}"
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
            send_telegram_documents(created_files)
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


def parse_apply_cli_argv(
    argv: list[str],
) -> tuple[str, bool, bool, bool, str, str, str, bool]:
    """Parse argv (including script name).

    Returns: url, force_cli, force, full, company, title, paste_file, notify_start
    """
    args = argv[1:]
    force_cli = "--cli" in args
    force = "--force" in args
    full = "--full" in args
    notify_start = "--notify-start" in args
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
    return url, force_cli, force, full, company, title, paste_file, notify_start


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python apply_agent.py <job_url> [--cli] [--force] [--full] "
            "[--company NAME] [--title TITLE] [--paste-file PATH] [--notify-start]",
        )
        sys.exit(1)

    url, force_cli, force, full, co, ti, paste_file, notify_start = parse_apply_cli_argv(sys.argv)

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

    # Send early Telegram notification so the user knows the subprocess is alive.
    # Only set when triggered manually from Telegram (not from auto-apply).
    if notify_start:
        label = url if url else "(pasted text)"
        notify(f"🔄 <b>Обрабатываю...</b>\n🔗 {label}\n\nFetching job text & calling LLM…")

    _APPLY_META_COMPANY = co
    _APPLY_META_TITLE = ti
    main(url, force_cli=force_cli, force=force, full=full, paste_text=paste_text)
