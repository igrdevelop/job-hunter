"""
hunter/apply_shared.py — shared helpers used by both apply_api and apply_cli.

Exported symbols used by apply_agent.py for backward compatibility:
    _already_processed, _body_banlist_hits, _opener_banlist_hits,
    ApplyError, APPLY_MANUAL_EXIT_CODE, PASTE_NO_URL_PLACEHOLDER
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import requests

from hunter.config import (
    APPLICATIONS_DIR,
    GENERATE_PL_RESUME,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    PROJECT_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_SEND_DOCS,
)

# ── Constants ─────────────────────────────────────────────────────────────────

PROMPTS_DIR = PROJECT_DIR / "prompts"

REQUIRED_JSON_KEYS: list[str] = [
    "company_name", "stack", "lang", "job_title",
    "resume_en", "cover_letter_en", "cover_letter_pl",
    "about_me_en", "about_me_pl",
]
if GENERATE_PL_RESUME:
    REQUIRED_JSON_KEYS.append("resume_pl")

# Exit code: JobLeads fetch blocked — MANUAL tracker row + stub job_posting.txt written
APPLY_MANUAL_EXIT_CODE = 44

# Exit code: fetch hit a transient rate limit (HTTP 429). The caller should retry
# later WITHOUT escalating the permanent fail counter — the offer is likely fine.
APPLY_RATE_LIMITED_EXIT_CODE = 45

# Placeholder URL used when user pastes job text into Telegram without any link.
PASTE_NO_URL_PLACEHOLDER = "paste://no-url"


def is_rate_limit_error(exc: Exception) -> bool:
    """True if an exception represents an HTTP 429 / rate-limit response.

    Checks a requests/cloudscraper-style ``exc.response.status_code`` first, then
    falls back to scanning the message for a 429 / "too many requests" signal.
    """
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg

# Shown after React-only auto-skip.
_REACT_SKIP_FORCE_HINT = (
    "\n\n📌 <b>Need docs anyway?</b> In Telegram:\n"
    "• <code>/force</code> and the same URL (🔗 above), or\n"
    "• <code>/force</code> followed by the full job posting text (same as paste flow).\n"
    "This enables <code>--force</code> (bypasses React-only filter); for JobLeads "
    "<code>job_posting.txt</code> will be used if already filled in."
)


# ── Pre-LLM text-based stack screening ───────────────────────────────────────

# Minimum number of React mentions (no Angular present) to auto-skip pre-LLM.
_REACT_SKIP_MIN_MENTIONS: int = 3

# BE-required signal patterns: language/framework + hard-requirement qualifier.
# Fires only when a clear "required/must/mandatory" is combined with a BE marker,
# AND no frontend framework (Angular / React / Vue) is mentioned in the posting.
_BE_REQUIRED_LANG_RE = re.compile(
    r"\b(?:python|django|flask|fastapi|ruby|rails|php|laravel|symfony|golang|go\s+lang"
    r"|java(?!script)|spring\s+boot|\.net\s+core|c\s*#)\b",
    re.IGNORECASE,
)
_BE_REQUIRED_QUALIFIER_RE = re.compile(
    r"\b(?:required|mandatory|essential|must\s+have|must[-\s]have|must\s+know"
    r"|you\s+(?:will\s+)?(?:need|must)|we\s+require|minimum\s+requirement)\b",
    re.IGNORECASE,
)
_FE_FRAMEWORK_RE = re.compile(
    r"\b(?:angular|react(?:\.?js)?|vue(?:\.?js)?|next\.?js|nuxt(?:\.?js)?)\b",
    re.IGNORECASE,
)


def is_react_only_job_text(text: str) -> bool:
    """Return True if job text is clearly React-only before calling the LLM.

    Conservative heuristic — only fires when:
    1. The word "angular" does NOT appear anywhere in the text, AND
    2. "react" appears at least _REACT_SKIP_MIN_MENTIONS (3) times.

    Skipping early saves the LLM call; Step 4.5 in apply_api.py remains as a
    fallback for edge cases (e.g. Angular mentioned once, React dominates).
    """
    t = text.lower()
    if "angular" in t:
        return False
    return len(re.findall(r"\breact\b", t)) >= _REACT_SKIP_MIN_MENTIONS


def is_backend_only_job_text(text: str) -> bool:
    """Return True if job text explicitly requires a backend language/framework
    AND mentions no frontend framework at all — saving the LLM call.

    Very conservative: requires BOTH a hard-requirement qualifier AND a BE
    language signal, with zero FE framework mentions.  False-positive risk is
    kept low by requiring the absence of all FE framework names.
    """
    if _FE_FRAMEWORK_RE.search(text):
        # Any Angular / React / Vue / Next / Nuxt mention → let LLM decide
        return False
    if not _BE_REQUIRED_LANG_RE.search(text):
        return False
    return bool(_BE_REQUIRED_QUALIFIER_RE.search(text))


# ── Exceptions ────────────────────────────────────────────────────────────────

class ApplyError(RuntimeError):
    """Raised when an apply attempt fails and fallback should be tried."""


# ── Tracker dedup ─────────────────────────────────────────────────────────────

def _already_processed(url: str, skip_dedup: bool = False) -> bool:
    """Check tracker.xlsx before calling LLM.

    Returns True if:
    - a successful entry exists (ATS = real score), OR
    - a React-skip entry exists (ATS=SKIP, Sent='—') — permanently blocked.
    FAIL and plain SKIP rows do NOT block, so those jobs can be retried.
    Skipped entirely when skip_dedup=True or URL is the paste placeholder.
    """
    if skip_dedup:
        return False
    if not url or url == PASTE_NO_URL_PLACEHOLDER:
        return False
    try:
        from hunter.services.tracker_service import should_skip_url
        return should_skip_url(url)
    except Exception:
        return False


# ── Telegram helpers ──────────────────────────────────────────────────────────

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


# Telegram Bot API: max document size 50MB
_TELEGRAM_DOC_MAX_BYTES = 50 * 1024 * 1024
_TELEGRAM_SEND_DOC_TIMEOUT = 120


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
                print(f"[apply_agent] Skipping Telegram doc (over 50MB): {p.name}")
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
        notify(f"⚠️ <b>Some files were not sent to Telegram</b>\n{short}{more}")
    elif sent:
        print(f"[apply_agent] Sent {sent} file(s) to Telegram")


# ── Cover letter review loop ──────────────────────────────────────────────────

_REVIEW_SYSTEM = (
    "You are a professional recruiter reviewing cover letters for a senior Angular candidate. "
    "Accept classic business-letter phrasing (I am writing to…, thank you, I look forward to…). "
    "Still penalise generic resume-site tone, missing posting specifics, and weak metrics. "
    "Respond ONLY with a JSON object, no other text."
)

_BANNED_OPENER_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\s*the best\s+\w[\w\s-]*?\bI know\b", re.IGNORECASE),
    re.compile(r"^\s*great\s+\w[\w\s-]*?\bdon['‘’]t just\b", re.IGNORECASE),
    re.compile(r"\bis what I bring to\b", re.IGNORECASE),
    re.compile(r"\bis exactly what\s+.{1,80}?(?:requires|needs|is looking for|is after)\b", re.IGNORECASE),
    re.compile(
        r"\bexactly the challenges you['‘’]?re\s+(?:facing|tackling|solving)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*I['‘’]ve had the opportunity to\b", re.IGNORECASE),
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

_BANNED_BODY_PHRASES: tuple[re.Pattern, ...] = (
    re.compile(r"\baligns?\s+seamlessly\b", re.IGNORECASE),
    re.compile(r"\baligns?\s+(?:perfectly\s+)?with\s+my\s+background\b", re.IGNORECASE),
    re.compile(r"\baligns?\s+perfectly\s+with\b", re.IGNORECASE),
    re.compile(r"\bstandards\s+of\s+excellence\b", re.IGNORECASE),
    re.compile(r"\btechnical\s+acumen\b", re.IGNORECASE),
    re.compile(r"\besteemed\s+team\b", re.IGNORECASE),
    re.compile(r"\bharnessing\s+both\b", re.IGNORECASE),
    re.compile(r"I['‘’]ve had the opportunity to closely follow\b", re.IGNORECASE),
    re.compile(r"\bhad the opportunity to closely follow\b", re.IGNORECASE),
    re.compile(r"\bperfect\s+(?:fit|match)\b", re.IGNORECASE),
    re.compile(r"\bideal\s+match\b", re.IGNORECASE),
    re.compile(r"\bexactly\s+what\s+I['‘’]?m\s+looking\s+for\b", re.IGNORECASE),
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

# Gate 8 — language mixing detection
# Polish diacritics / common Polish function words that never appear in English IT text
_PL_IN_EN_RE = re.compile(
    r"[ąęóśźżćńł]"
    r"|\b(się|jest|nie|przez|oraz|który|która|które|tego|jak|czy|przy|dla|już"
    r"|jestem|moje|mojej|moich|swoim|swoją|swoje|gdzie|będę|będzie|chciałbym"
    r"|chciałabym|doświadczenie|specjalizuję|zajmuję|pracowałem|pracowałam"
    r"|zbudowałem|przeprowadziłem|posiadam|poszukuję|szukam)\b",
    re.IGNORECASE,
)
# English sentence starters that don't belong inside a Polish letter
_EN_IN_PL_RE = re.compile(
    r"\b(I am writing|I would like|I have been|As a Senior|I look forward"
    r"|I bring|I have worked|In my previous|Dear Hiring|With over)\b",
    re.IGNORECASE,
)

_METRIC_RE = re.compile(
    r"\b\d+\s*%"
    r"|\b\d{3,}\b"
    r"|\b\d+\s*(?:x\b|\+\b)"
    r"|\b\d+\+?\s*(?:people|developers?|engineers?|banks?|apps?|applications?|"
    r"clients?|members?|months?|weeks?|hours?|microservices?|services?|projects?|"
    r"repos?|repositories?|teams?|companies|countries)\b",
    re.IGNORECASE,
)


def _opener_banlist_hits(letter: str) -> list[str]:
    """Return list of banned patterns matched in the letter's opener (first sentence)."""
    if not letter:
        return []
    head = letter.strip()[:250]
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


def _detect_language_mixing(letter: str, expected_lang: str) -> list[str]:
    """Gate 8 — detect language mixing in the cover letter.

    Returns a list of violation descriptions (empty = clean).
    IT anglicisms (Angular, TypeScript, NgRx, etc.) are always allowed in PL letters.
    """
    if not letter:
        return []
    lang = (expected_lang or "EN").upper()
    hits: list[str] = []
    if lang == "EN":
        # Strip known IT terms before checking for Polish diacritics/words
        it_terms = re.compile(
            r"\b(Angular|React|TypeScript|JavaScript|NgRx|RxJS|Nx|SonarQube"
            r"|Node\.?js|Jenkins|Webpack|Docker|GitHub|GitLab|CI/CD|SCSS|Bootstrap"
            r"|AG\s*Grid|Signals|Agile|Scrum|SAFe|REST|API|JSON|HTML|CSS|WCAG"
            r"|Cypress|Jest|Jasmine|Playwright|Next\.?js|NestJS|Redux)\b",
            re.IGNORECASE,
        )
        cleaned = it_terms.sub("", letter)
        if _PL_IN_EN_RE.search(cleaned):
            sample = _PL_IN_EN_RE.search(cleaned)
            hits.append(
                f"Gate 8 — Polish words/diacritics found in EN letter "
                f"(e.g. '{sample.group()[:30]}')"
            )
    elif lang == "PL":
        if _EN_IN_PL_RE.search(letter):
            sample = _EN_IN_PL_RE.search(letter)
            hits.append(
                f"Gate 8 — English sentence patterns found in PL letter "
                f"(e.g. '{sample.group()[:40]}')"
            )
    return hits


def _review_cover_letter(letter: str, expected_lang: str = "EN") -> tuple[str, int]:
    """Review cover letter against quality gates; rewrite if any gate fails.

    Gate 1: word count (_CL_WORD_MIN–_CL_WORD_MAX)
    Gate 2: ≥2 numeric metrics in body (excluding "10+ years")
    Gate 3: opener not in banned patterns
    Gate 4: no banned body phrases
    Gate 5: unfamiliar tech uses only safe verbs (checked by LLM)
    Gate 6: CTA — no banned fluff phrases in final paragraph
    Gate 7: 3–5 body paragraphs after Dear …
    Gate 8: no language mixing (EN letter must not contain Polish words/diacritics;
            PL letter must not contain English sentence patterns)

    Returns (rewritten_or_original, score_1_to_10). Score > 6 = acceptable.
    Skips if no API key available.
    """
    if not LLM_API_KEY:
        return letter, 10

    opener_hits = _opener_banlist_hits(letter)
    body_hits = _body_banlist_hits(letter)
    cta_hits = _cta_banlist_hits(letter)
    lang_hits = _detect_language_mixing(letter, expected_lang)
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
    if lang_hits:
        forced_fails.extend(lang_hits)

    for msg in forced_fails:
        print(f"[apply_agent] Pre-check FAIL: {msg}")

    gate6_line = f"FAIL — {cta_hits[0][:60]}" if cta_hits else "PASS"
    gate8_line = f"FAIL — {lang_hits[0][:80]}" if lang_hits else f"PASS (expected: {expected_lang})"

    gate_summary = (
        f"Gate 1 (word count {_CL_WORD_MIN}-{_CL_WORD_MAX}): "
        f"{'PASS' if _CL_WORD_MIN <= wc <= _CL_WORD_MAX else f'FAIL ({wc} words)'}\n"
        f"Gate 2 (≥2 numeric metrics): {'PASS' if metric_count >= 2 else f'FAIL ({metric_count} found)'}\n"
        f"Gate 3 (opener ban): {'FAIL — ' + opener_hits[0][:60] if opener_hits else 'PASS'}\n"
        f"Gate 4 (body banned phrases): {'FAIL — ' + body_hits[0][:60] if body_hits else 'PASS'}\n"
        f"Gate 6 (CTA): {gate6_line}\n"
        f"Gate 7 (body paragraphs {_CL_BODY_PARA_MIN}-{_CL_BODY_PARA_MAX}): "
        f"{'PASS' if _CL_BODY_PARA_MIN <= body_paras <= _CL_BODY_PARA_MAX else f'FAIL ({body_paras})'}\n"
        f"Gate 8 (language consistency — expected {expected_lang}): {gate8_line}"
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
        f"Gate 8 (language consistency — expected language: {expected_lang}): "
        "The letter MUST be written entirely in one language. "
        "For EN: no Polish words, no Polish diacritics (ą ę ó ś ź ż ć ń ł), no Polish grammar. "
        "For PL: no English sentence-level constructs (full English sentences are forbidden; "
        "IT anglicisms like Angular, TypeScript, NgRx, CI/CD are allowed). "
        "Language mixing → Gate 8 FAIL.\n\n"
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


# ── Language enforce-gate ─────────────────────────────────────────────────────
# After generation + ATS rewrites, English fields can still contain Polish keywords
# (the ATS loop mirrors a Polish posting's keywords verbatim into resume_en). This
# gate detects contamination (hunter.lang_guard), repairs it by *translating* from
# the clean opposite-language counterpart, and — if strong contamination survives —
# signals the caller to BLOCK delivery rather than ship a broken document.

_RESUME_TRANSLATE_SYS = (
    "You are a professional bilingual (Polish/English) resume translator. "
    "You translate resume content between Polish and English. "
    "Respond ONLY with a valid JSON object — no markdown, no commentary."
)


def _expected_role_count(content: dict) -> int:
    """Best estimate of how many experience entries a resume must keep."""
    counts = []
    for k in ("resume_en", "resume_pl"):
        r = content.get(k)
        if isinstance(r, dict) and isinstance(r.get("experience"), list):
            counts.append(len(r["experience"]))
    return max(counts) if counts else 0


def _translate_resume(source_resume: dict, target_lang: str, *, expected_roles: int) -> dict | None:
    """Translate a resume dict into `target_lang` ('EN'/'PL'). Returns dict or None.

    Pure translation: keeps company names, periods, titles, tech names, numbers and
    array structure identical; only natural-language values are translated. Guards
    against role drop — returns None if the translation loses experience entries.
    """
    if not LLM_API_KEY or not isinstance(source_resume, dict):
        return None
    lang_name = "English" if target_lang.upper() == "EN" else "Polish"
    try:
        from llm_client import call_llm
        result = call_llm(
            system_prompt=_RESUME_TRANSLATE_SYS,
            user_message=(
                f"Translate this resume JSON into {lang_name}. STRICT RULES:\n"
                f"- Output MUST be entirely in {lang_name}. Translate EVERY foreign word, "
                "including skill keywords, to its standard professional equivalent "
                "(e.g. 'responsywne interfejsy' -> 'responsive interfaces', "
                "'testy jednostkowe' -> 'unit tests', 'doświadczenie' -> 'experience').\n"
                "- Do NOT keep any source-language word and do NOT add parenthetical "
                "glosses like 'X (Y)'. Standard IT anglicisms (Angular, TypeScript, "
                "frontend, backend, code review, CI/CD, deployment) stay as-is.\n"
                f"- Keep company, period, title, subtitle, numbers, metrics, versions and "
                "tech names IDENTICAL. Translate only natural-language text.\n"
                f"- Return ALL {expected_roles} experience entries in the SAME order. "
                "Never drop, merge, summarise or reorder an entry.\n"
                "- Return the SAME JSON keys/structure as the input.\n\n"
                'Respond with JSON only: {"resume": <translated resume object>}\n\n'
                f"Resume to translate:\n{json.dumps(source_resume, ensure_ascii=False)}"
            ),
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=4000,
        )
        out = result.get("resume") if isinstance(result, dict) else None
        if not isinstance(out, dict):
            # Some models return the resume object directly without the wrapper.
            out = result if isinstance(result, dict) and result.get("experience") else None
        if not isinstance(out, dict):
            return None
        exp = out.get("experience")
        if expected_roles and (not isinstance(exp, list) or len(exp) < expected_roles):
            print(
                f"[apply_agent] lang-gate: translation dropped roles "
                f"({len(exp) if isinstance(exp, list) else 0} < {expected_roles}) — rejecting"
            )
            return None
        return out
    except Exception as e:
        print(f"[apply_agent] lang-gate resume translation error: {e}")
        return None


def _translate_plain(text: str, target_lang: str, kind: str) -> str:
    """Translate a cover letter / about-me string into target_lang. '' on failure."""
    if not LLM_API_KEY or not isinstance(text, str) or not text.strip():
        return ""
    lang_name = "English" if target_lang.upper() == "EN" else "Polish"
    try:
        from llm_client import call_llm
        result = call_llm(
            system_prompt="You are a professional translator. Respond ONLY with JSON.",
            user_message=(
                f"Rewrite this {kind} in natural, professional {lang_name}. "
                f"Output MUST be entirely in {lang_name} — translate every foreign word "
                "to its standard professional equivalent, INCLUDING any quoted text "
                "(translate the words inside quotation marks too; do not preserve a "
                "foreign-language quote verbatim). Keep standard IT anglicisms. "
                "Do NOT add parenthetical glosses. Keep the same structure, facts, "
                "metrics and tone; avoid word-for-word calques. The result must contain "
                f"zero non-{lang_name} words other than proper nouns and tech names.\n\n"
                'Respond with JSON only: {"text": "<translated text>"}\n\n'
                f"Text:\n{text}"
            ),
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=2000,
        )
        out = result.get("text", "") if isinstance(result, dict) else ""
        return out if isinstance(out, str) and len(out) > 30 else ""
    except Exception as e:
        print(f"[apply_agent] lang-gate {kind} translation error: {e}")
        return ""


def _is_unit_clean(scan: dict, unit_prefix: str, side: str) -> bool:
    """True if no contamination paths for `unit_prefix` on the given side.

    side='en' → check Polish-in-English maps; side='pl' → English-in-Polish map.
    """
    if side == "en":
        buckets = (scan.get("en_strong", {}), scan.get("en_soft", {}))
    else:
        buckets = (scan.get("pl_english", {}),)
    for bucket in buckets:
        if any(p == unit_prefix or p.startswith(unit_prefix + ".") for p in bucket):
            return False
    return True


def enforce_language_separation(content: dict, posting_lang: str = "EN") -> tuple[dict, bool, list[str]]:
    """Enforce-gate: each `_en` field must be clean English, each `_pl` field clean Polish.

    Repair strategy (language routing): when a contaminated field has a CLEAN
    opposite-language counterpart, regenerate it by translating the clean one — far
    more reliable than patching, with no re-fabrication or ATS keyword re-stuffing.
    Falls back to in-place cleanup translation when both sides are dirty.

    Returns (content, blocked, report). `blocked=True` means strong Polish survived
    in an English field after repair — the caller must NOT ship the documents.
    """
    from hunter.lang_guard import scan_content, has_blocking_contamination, needs_repair

    report: list[str] = []
    scan = scan_content(content)
    if not needs_repair(scan):
        return content, False, report

    contaminated = sorted(
        set(scan.get("en_strong", {})) | set(scan.get("en_soft", {})) | set(scan.get("pl_english", {}))
    )
    report.append(f"contamination in {len(contaminated)} field(s): {', '.join(contaminated[:8])}")
    expected_roles = _expected_role_count(content)

    # Units: (en_key, pl_key, is_resume)
    units = [
        ("resume_en", "resume_pl", True),
        ("cover_letter_en", "cover_letter_pl", False),
        ("about_me_en", "about_me_pl", False),
    ]

    def _retranslate(src_obj, target_lang, is_resume, kind="text"):
        if is_resume:
            return _translate_resume(src_obj, target_lang, expected_roles=expected_roles)
        return _translate_plain(src_obj, target_lang, kind)

    # Round 0 — repair each contaminated field by translating the CLEAN
    # opposite-language counterpart (most reliable: no re-fabrication).
    for en_key, pl_key, is_resume in units:
        en_dirty = not _is_unit_clean(scan, en_key, "en")
        pl_dirty = not _is_unit_clean(scan, pl_key, "pl")

        kind = "cover letter" if "letter" in en_key else ("about-me text" if "about" in en_key else "text")
        if en_dirty and content.get(pl_key) and not pl_dirty:
            fixed = _retranslate(content[pl_key], "EN", is_resume, kind)
            if fixed:
                content[en_key] = fixed
                report.append(f"{en_key}: re-translated from clean {pl_key}")
        if pl_dirty and content.get(en_key) and not en_dirty:
            fixed = _retranslate(content[en_key], "PL", is_resume, kind)
            if fixed:
                content[pl_key] = fixed
                report.append(f"{pl_key}: re-translated from clean {en_key}")

    # Rounds 1-2 — for any field still carrying STRONG Polish (no clean counterpart,
    # or the counterpart-translation left residue), clean it IN PLACE. Translation
    # is imperfect on the first try, so retry before giving up.
    en_keys = {u[0]: u[2] for u in units}
    for _round in range(2):
        final_scan = scan_content(content)
        if not has_blocking_contamination(final_scan):
            break
        # en_strong is keyed by field PATH; collapse to distinct UNITS so a resume
        # with several contaminated fields is re-translated once, not once per field.
        dirty_units = dict.fromkeys(
            k.split(".")[0] for k in final_scan.get("en_strong", {})
        )
        for unit_key in dirty_units:
            is_resume = en_keys.get(unit_key, False)
            src = content.get(unit_key)
            if not src:
                continue
            kind = "cover letter" if "letter" in unit_key else ("about-me text" if "about" in unit_key else "text")
            fixed = _retranslate(src, "EN", is_resume, kind)
            if fixed and fixed != src:
                content[unit_key] = fixed
                report.append(f"{unit_key}: cleaned in place (round {_round + 1})")

    # Final verdict: block only if STRONG Polish still survives in an English field.
    final_scan = scan_content(content)
    blocked = has_blocking_contamination(final_scan)
    if blocked:
        survivors = final_scan.get("en_strong", {})
        detail = "; ".join(
            f"{p}: {', '.join(frags[:4])}" for p, frags in list(survivors.items())[:5]
        )
        report.append(f"BLOCKED — strong Polish survived → {detail}")
    return content, blocked, report


_ATS_THRESHOLD = 95.0
_ATS_MAX_ROUNDS = 2   # honest rounds; after this: soft → aggressive → final check

# Regulatory / compliance terms that job postings list as the EMPLOYER's own
# credentials ("we work in accordance with DORA, RODO"). The ATS keyword extractor
# picks them up as job keywords, and the aggressive rewrite would inject them into
# the candidate's Skills as if they were personal expertise — a fabrication. These
# are stripped from the ATS "missing keywords" so the rewrite never adds them.
# (Mirrors the RED LINE in prompts/generation_rules.md.)
_ATS_KEYWORD_BLOCKLIST = frozenset({
    "dora", "rodo", "gdpr", "iso", "iso 27001", "iso27001", "soc2", "soc 2",
    "hipaa", "pci", "pci-dss", "pci dss",
})


def _filter_self_description_keywords(keywords: list[str]) -> list[str]:
    """Drop employer-credential / regulatory terms that must not be claimed as
    the candidate's own skills (see _ATS_KEYWORD_BLOCKLIST)."""
    return [k for k in keywords if k.strip().lower() not in _ATS_KEYWORD_BLOCKLIST]


# Word-boundary matcher for regulatory/compliance terms that an employer lists as
# its own credentials. Used to scrub fabricated claims the LLM may still write into
# the summary / skills / about-me despite the generation_rules.md RED LINE.
_COMPLIANCE_CLAIM_RE = re.compile(
    r"\b(?:DORA|RODO|GDPR|ISO(?:\s?\d{4,5})?|SOC\s?2|HIPAA|PCI(?:[-\s]?DSS)?)\b",
    re.IGNORECASE,
)

# Removes a connector + compliance phrase embedded in a bullet/stack_line, e.g.
# " with DORA compliance", " following ISO standards", " and GDPR compliance".
_COMPLIANCE_CLAUSE_RE = re.compile(
    r"\s*(?:[,;]|\b(?:with|following|and|including|under|per|ensuring|maintaining"
    r"|adhering to|in line with|compliant with|aligned with)\b)\s+[^,.;]*?"
    r"\b(?:DORA|RODO|GDPR|ISO(?:\s?\d{4,5})?|SOC\s?2|HIPAA|PCI(?:[-\s]?DSS)?)\b"
    r"(?:\s+(?:compliance|standards?|adherence|certification|requirements?))?",
    re.IGNORECASE,
)


def _scrub_compliance_clause(text: str) -> str:
    """Remove embedded compliance clauses from a bullet/stack_line while keeping
    the rest of the sentence intact. Loops until stable to catch chained clauses
    ('following ISO standards and DORA compliance')."""
    if not isinstance(text, str):
        return text
    prev = None
    cur = text
    while prev != cur and _COMPLIANCE_CLAIM_RE.search(cur):
        prev = cur
        cur = _COMPLIANCE_CLAUSE_RE.sub("", cur, count=1)
    # Tidy leftovers: double spaces, dangling connectors/punctuation before end.
    cur = re.sub(r"\s{2,}", " ", cur)
    cur = re.sub(r"\s+(?:and|with|following|including)\s*$", "", cur, flags=re.IGNORECASE)
    cur = re.sub(r"\s*[,;]\s*$", "", cur)
    return cur.strip()


def _strip_compliance_claims(content: dict) -> tuple[dict, list[str]]:
    """Remove fabricated regulatory/compliance claims (DORA, RODO, GDPR, ISO,
    SOC2, HIPAA, PCI) from summary / skills / about-me text. These come from the
    employer's self-description and must never be claimed as the candidate's own
    expertise. Returns (content, list_of_fixes)."""
    fixes: list[str] = []

    def _scrub_sentences(text: str, label: str) -> str:
        if not isinstance(text, str) or not _COMPLIANCE_CLAIM_RE.search(text):
            return text
        parts = re.split(r"(?<=[.!?])\s+", text)
        kept = [s for s in parts if not _COMPLIANCE_CLAIM_RE.search(s)]
        new = " ".join(kept).strip()
        if new != text:
            fixes.append(f"[{label}] removed compliance-claim sentence(s)")
        return new

    def _scrub_skills(skills: object, label: str) -> object:
        if isinstance(skills, dict):
            for cat, val in list(skills.items()):
                if isinstance(val, str) and _COMPLIANCE_CLAIM_RE.search(val):
                    items = [i for i in val.split(",") if not _COMPLIANCE_CLAIM_RE.search(i)]
                    new = ", ".join(s.strip() for s in items if s.strip())
                    if new != val:
                        skills[cat] = new
                        fixes.append(f"[{label}] removed compliance terms from skills.{cat}")
                elif isinstance(val, list) and any(_COMPLIANCE_CLAIM_RE.search(str(i)) for i in val):
                    skills[cat] = [i for i in val if not _COMPLIANCE_CLAIM_RE.search(str(i))]
                    fixes.append(f"[{label}] removed compliance terms from skills.{cat}")
        return skills

    def _scrub_experience(exp: object, label: str) -> None:
        if not isinstance(exp, list):
            return
        for role in exp:
            if not isinstance(role, dict):
                continue
            bullets = role.get("bullets")
            if isinstance(bullets, list):
                new_bullets = []
                for b in bullets:
                    nb = _scrub_compliance_clause(b) if isinstance(b, str) else b
                    if isinstance(b, str) and nb != b:
                        fixes.append(f"[{label}] scrubbed compliance clause from a bullet")
                    # Drop a bullet that was ONLY a compliance claim (now empty)
                    if isinstance(nb, str) and not nb.strip():
                        continue
                    new_bullets.append(nb)
                role["bullets"] = new_bullets
            for fld in ("stack_line", "subtitle"):
                if isinstance(role.get(fld), str) and _COMPLIANCE_CLAIM_RE.search(role[fld]):
                    new = _scrub_compliance_clause(role[fld])
                    if new != role[fld]:
                        role[fld] = new
                        fixes.append(f"[{label}] scrubbed compliance from {fld}")

    for rk, lang in (("resume_en", "EN"), ("resume_pl", "PL")):
        r = content.get(rk)
        if isinstance(r, dict):
            if "summary" in r:
                r["summary"] = _scrub_sentences(r["summary"], f"{lang} summary")
            if "skills" in r:
                r["skills"] = _scrub_skills(r["skills"], lang)
            _scrub_experience(r.get("experience"), lang)
            # Courses: comma-separated; drop any item naming a compliance framework.
            if isinstance(r.get("courses"), str) and _COMPLIANCE_CLAIM_RE.search(r["courses"]):
                items = [i for i in r["courses"].split(",") if not _COMPLIANCE_CLAIM_RE.search(i)]
                new = ", ".join(s.strip() for s in items if s.strip())
                if new != r["courses"]:
                    r["courses"] = new
                    fixes.append(f"[{lang}] removed compliance item from courses")
    for ak, lang in (("about_me_en", "EN"), ("about_me_pl", "PL")):
        if ak in content:
            content[ak] = _scrub_sentences(content[ak], f"{lang} about_me")

    return content, fixes

_ATS_REWRITE_PROMPT = """\
The resume scored {score:.1f}% on an independent ATS check (target: {threshold}%).

Missing keywords that must be added:
{missing}

Specific recommendations:
{recs}

Gap analysis:
{gap}

Rewrite 'resume_en' to reach {threshold}%+:
- Add ALL missing keywords naturally into the Skills section and relevant experience bullets.
- resume_en MUST stay entirely in English. If a job-posting keyword is in another
  language (e.g. Polish), add its standard ENGLISH equivalent — never the foreign
  word, and never a parenthetical gloss like "X (Y)".
- Do NOT invent facts — integrate keywords into real experience the candidate has.
- Keep the same JSON schema; return ALL fields unchanged except the ones you improve.

Job posting (for keyword reference):
{job_text}

Current resume JSON:
{content_json}"""

_ATS_SOFT_PROMPT = """\
The resume scored {score:.1f}% after {rounds} honest rewrites (target: {threshold}%).
It is still below threshold. Apply a smarter keyword strategy:

Missing keywords:
{missing}

Rules for this pass:
- Add every missing keyword to the Skills section directly — no disclaimers needed.
- Where a missing term is a synonym or close variant of something already in the resume,
  add it as an alternative phrasing (e.g. "REST / RESTful APIs", "CI/CD / GitHub Actions").
- Rephrase existing bullet points to use the exact wording from the job description
  (e.g. if JD says "cross-functional teams", replace "multi-team collaboration").
- Keep resume_en entirely in English: translate any non-English keyword to its
  English equivalent; never paste foreign words or "X (Y)" glosses.
- You may expand the Skills section with adjacent technologies the candidate has
  encountered in projects, even briefly.
- Keep all factual claims truthful; do not add years of experience for new terms.
- Return the same JSON schema with improved resume_en (and resume_pl if present).

Job posting:
{job_text}

Current resume JSON:
{content_json}"""

_ATS_AGGRESSIVE_PROMPT = """\
The resume scored {score:.1f}% after {rounds} rewrites (target: {threshold}%).
Last resort: keyword injection pass.

Missing keywords:
{missing}

Rules:
- Insert ALL missing keywords from the list directly into the Skills section.
- No caveats, no "familiar with" — just list them as skills.
- Also rewrite any bullet point that can naturally absorb a missing term.
- resume_en MUST be entirely in English: use the English equivalent of any
  non-English keyword; never paste foreign words or "X (Y)" glosses.
- Return the same JSON schema with improved resume_en (and resume_pl if present).

Job posting:
{job_text}

Current resume JSON:
{content_json}"""


def _ats_check_loop(content: dict, job_text: str) -> dict:
    """Run independent ATS check; rewrite resume up to 4 times if score < 95%.

    Round 1-2: honest rewrite ("do NOT invent facts").
    Round 3:   soft-liar pass — synonyms, adjacent tech, exact JD phrasing.
    Round 4:   aggressive pass — inject all missing keywords into Skills directly.
    Round 5:   final check only; if still failing → warn and proceed.
    """
    from hunter import ats_checker

    _TOTAL_ROUNDS = 5  # rewrite rounds before final check

    resume_en = content.get("resume_en", "")
    if not resume_en:
        print("[apply_agent] ATS check skipped — no resume_en in content")
        return content

    if isinstance(resume_en, dict):
        resume_text_for_ats = json.dumps(resume_en, ensure_ascii=False)
    else:
        resume_text_for_ats = str(resume_en)

    # Snapshot the full experience arrays before any rewrite. The ATS rewrite
    # passes send a truncated resume to the LLM (content_json is capped), so the
    # model can silently return fewer roles. Dropping a role violates a hard
    # RED LINE, so we restore the original experience whenever a boost shrinks it.
    import copy

    def _exp_of(r: object) -> list:
        return r.get("experience") if isinstance(r, dict) and isinstance(r.get("experience"), list) else []

    _orig_exp_en = copy.deepcopy(_exp_of(content.get("resume_en")))
    _orig_exp_pl = copy.deepcopy(_exp_of(content.get("resume_pl")))

    # Job text shown to the rewrite passes, with employer self-description /
    # regulatory terms removed so the LLM can't lift DORA/RODO/ISO from the posting
    # and inject them into the candidate's bullets. The ATS *checker* above still
    # gets the full, unmodified job_text.
    _rewrite_job_text = _COMPLIANCE_CLAIM_RE.sub("", job_text)[:3000]

    for attempt in range(1, _TOTAL_ROUNDS + 2):
        run_llm = attempt == 1 and bool(LLM_API_KEY)
        result = ats_checker.check(
            job_text=job_text,
            resume_text=resume_text_for_ats,
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            run_llm_review=run_llm,
        )
        print(f"[apply_agent] ATS check (attempt {attempt}):\n{result.summary()}")
        content["ats_check"] = result.to_dict()

        if result.passed(_ATS_THRESHOLD):
            break

        if attempt > _TOTAL_ROUNDS:
            break

        _missing_kw = _filter_self_description_keywords(result.missing_keywords)
        missing_str = "\n".join(f"  - {k}" for k in _missing_kw[:20]) or "  (none identified)"
        recs_str = "\n".join(f"  - {r}" for r in result.recommendations) or "  (none)"
        # The ATS check only scores the English resume, so only resume_en is sent
        # for rewriting (resume_pl is untouched here). The cap must comfortably fit
        # a full 7-role resume (~7k chars) so the LLM never sees a truncated
        # experience array and silently drops roles — the old 4000 cap cut the
        # array mid-way and caused exactly that. The role-preservation guard below
        # is the hard backstop; this just stops triggering it in the first place.
        content_json_str = json.dumps(
            {k: content[k] for k in ("resume_en", "stack", "ats_score") if k in content},
            ensure_ascii=False,
        )[:16000]

        if attempt <= _ATS_MAX_ROUNDS:
            mode = "honest"
            rewrite_msg = _ATS_REWRITE_PROMPT.format(
                score=result.score,
                threshold=_ATS_THRESHOLD,
                missing=missing_str,
                recs=recs_str,
                gap=result.llm_gap_report or "N/A",
                job_text=_rewrite_job_text,
                content_json=content_json_str,
            )
        elif attempt == _ATS_MAX_ROUNDS + 1:
            mode = "soft"
            rewrite_msg = _ATS_SOFT_PROMPT.format(
                score=result.score,
                threshold=_ATS_THRESHOLD,
                rounds=attempt - 1,
                missing=missing_str,
                job_text=_rewrite_job_text,
                content_json=content_json_str,
            )
        else:
            mode = "aggressive"
            rewrite_msg = _ATS_AGGRESSIVE_PROMPT.format(
                score=result.score,
                threshold=_ATS_THRESHOLD,
                rounds=attempt - 1,
                missing=missing_str,
                job_text=_rewrite_job_text,
                content_json=content_json_str,
            )

        try:
            from llm_client import call_llm
            print(f"[apply_agent] ATS rewrite attempt {attempt}/{_TOTAL_ROUNDS} ({mode} mode)...")
            boosted = call_llm(
                system_prompt=(
                    "You are rewriting a resume to pass ATS screening. "
                    "Return the same JSON schema with improved fields."
                ),
                user_message=rewrite_msg,
                provider=LLM_PROVIDER,
                model=LLM_MODEL,
                api_key=LLM_API_KEY,
            )
            for key in ("resume_en", "resume_pl",
                        "ats_score", "stack", "to_learn", "skills"):
                if boosted.get(key):
                    content[key] = boosted[key]
            # Guard: the rewrite must never drop roles (truncated input can make
            # the LLM return a shorter experience array). Restore the originals.
            for _key, _orig_exp in (("resume_en", _orig_exp_en), ("resume_pl", _orig_exp_pl)):
                _r = content.get(_key)
                if isinstance(_r, dict) and _orig_exp and len(_exp_of(_r)) < len(_orig_exp):
                    print(
                        f"[apply_agent] ATS rewrite dropped roles in {_key} "
                        f"({len(_exp_of(_r))} < {len(_orig_exp)}) — restoring full experience"
                    )
                    _r["experience"] = copy.deepcopy(_orig_exp)
            resume_en = content.get("resume_en", resume_en)
            if isinstance(resume_en, dict):
                resume_text_for_ats = json.dumps(resume_en, ensure_ascii=False)
            else:
                resume_text_for_ats = str(resume_en)
        except Exception as e:
            print(f"[apply_agent] ATS rewrite failed: {e}")
            break

    return content


def _cover_letter_review(content: dict) -> dict:
    """Review cover_letter_en once; rewrite if quality gates fail. Accept result as-is.

    Language unity: each field is reviewed against its own language.
    _en field → Gate 8 expects English (no Polish mixing).
    _pl field is not reviewed here but is generated by _translate_cover_letter_pl.
    """
    letter = content.get("cover_letter_en", "")
    if not letter:
        return content

    # Language unity: the _en field must be in English
    original_en = letter
    new_letter, score = _review_cover_letter(letter, expected_lang="EN")
    print(f"[apply_agent] Cover letter review: score={score}/10")

    content["cover_letter_en"] = new_letter
    if new_letter != original_en:
        notify(f"✍️ Cover letter rewritten after review (score was {score}/10)")
        pl = _translate_cover_letter_pl(new_letter)
        if pl:
            content["cover_letter_pl"] = pl

    return content


def _cover_letter_review_loop(content: dict, max_rounds: int = 3) -> dict:
    """Deprecated: use _cover_letter_review. Kept for backward compat."""
    return _cover_letter_review(content)


# ── Output folder logic ───────────────────────────────────────────────────────

def compute_output_folder(company_name: str) -> Path:
    """Compute Applications/{date}/{Company} with _2, _3 suffixes if needed."""
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


# ── JobLeads MANUAL flow ──────────────────────────────────────────────────────

def _handle_jobleads_fetch_blocked(
    url: str, err: str, company: str = "", title: str = ""
) -> None:
    """Stub job_posting.txt + MANUAL tracker row; Telegram instructs user; process exits 44."""
    from hunter.tracker import (
        add_manual_jobleads_pending,
        has_manual_pending,
        lookup_url,
        manual_jobleads_job_posting_path,
    )
    from hunter.sources.jobleads import JOBLEADS_PASTE_MARKER

    if has_manual_pending(url):
        jp = manual_jobleads_job_posting_path(url)
        hint = f"\nFile: <code>{jp}</code>" if jp else ""
        notify(
            "📋 <b>JobLeads — MANUAL row already exists</b>\n"
            "Paste the job text into <code>job_posting.txt</code> (below the marker) and run apply "
            "again with the same URL.\n"
            f"🔗 {url}{hint}\n"
            "<i>Dedup: row already in tracker.xlsx</i>"
        )
        print(f"[apply_agent] MANUAL_PENDING (existing) exit={APPLY_MANUAL_EXIT_CODE}")
        sys.exit(APPLY_MANUAL_EXIT_CODE)

    if lookup_url(url):
        notify(
            "📋 <b>JobLeads — URL already in tracker.xlsx</b> (dedup).\n"
            f"🔗 {url}\n"
            "If the row has status FAIL and you want MANUAL mode — delete that row in Excel and retry."
        )
        print(f"[apply_agent] MANUAL_PENDING (URL already tracked) exit={APPLY_MANUAL_EXIT_CODE}")
        sys.exit(APPLY_MANUAL_EXIT_CODE)

    company_folder = _sanitize_folder_company(company or "Unknown")
    title = (title or "Unknown").strip() or "Unknown"
    output_folder = compute_output_folder(company_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    stub = output_folder / "job_posting.txt"
    stub.write_text(
        f"URL: {url}\n\n"
        f"Company (from listing): {company or '—'}\n"
        f"Title (from listing): {title or '—'}\n\n"
        "JobLeads blocks automatic download (Cloudflare).\n"
        "Open the job in your browser, copy the full posting, and paste it below the marker line.\n\n"
        f"{JOBLEADS_PASTE_MARKER}\n\n",
        encoding="utf-8",
    )

    written = add_manual_jobleads_pending(
        url=url,
        company=company or "Unknown",
        title=title,
        folder_abs=output_folder,
    )
    folder_display = str(output_folder).replace("\\", "/")
    notify(
        "📋 <b>JobLeads — manual description required</b>\n\n"
        "Page blocked by Cloudflare. Row added to <b>tracker.xlsx</b> "
        "(ATS = <code>MANUAL</code>), folder created:\n"
        f"📁 <code>{folder_display}/</code>\n\n"
        "1. Open <code>job_posting.txt</code> in that folder\n"
        "2. Paste the full job posting <b>below</b> the marker line\n"
        "3. Save the file and run apply again <b>with the same URL</b>\n\n"
        f"🔗 {url}\n\n"
        f"<pre>{(err or '')[:280]}</pre>"
        + ("" if written else "\n\n<i>Tracker row not added (rare conflict).</i>"),
    )
    print(f"[apply_agent] MANUAL_PENDING exit={APPLY_MANUAL_EXIT_CODE} tracker_row={written}")
    sys.exit(APPLY_MANUAL_EXIT_CODE)


# ── Content validation ────────────────────────────────────────────────────────

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
        if isinstance(resume.get("experience"), list) and len(resume["experience"]) < 7:
            errors.append(f"resume_en.experience has only {len(resume['experience'])} jobs (expected 7 — ALL roles required)")
    else:
        errors.append("resume_en is not a dict")

    return errors
