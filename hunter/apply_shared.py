"""
hunter/apply_shared.py — Shared helpers for the apply pipeline.

Contains:
  - Cover letter quality gates + review loop
  - Independent ATS check + rewrite loop
  - validate_content()
  - Output folder computation
  - JobLeads MANUAL flow handler
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

from hunter.config import (
    APPLICATIONS_DIR,
    GENERATE_PL_RESUME,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    PROJECT_DIR,
)

# ── Required JSON keys ───────────────────────────────────────────────────────

REQUIRED_JSON_KEYS = [
    "company_name", "stack", "lang", "job_title",
    "resume_en", "cover_letter_en", "cover_letter_pl",
    "about_me_en", "about_me_pl",
]
if GENERATE_PL_RESUME:
    REQUIRED_JSON_KEYS.append("resume_pl")

PROMPTS_DIR = PROJECT_DIR / "prompts"

# ── Cover letter review ──────────────────────────────────────────────────────

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

_METRIC_RE = re.compile(
    r"\b\d+\s*%"
    r"|\b\d{3,}\b"
    r"|\b\d+\s*(?:x\b|\+\b)"
    r"|\b\d+\+?\s*(?:people|developers?|engineers?|banks?|apps?|applications?|"
    r"clients?|members?|months?|weeks?|hours?|microservices?|services?|projects?|"
    r"repos?|repositories?|teams?|companies|countries)\b",
    re.IGNORECASE,
)

_ATS_THRESHOLD = 95.0
_ATS_MAX_ROUNDS = 2

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
- Mirror the exact phrasing from the job description where possible.
- Do NOT invent facts — integrate keywords into real experience the candidate has.
- Keep the same JSON schema; return ALL fields unchanged except the ones you improve.

Job posting (for keyword reference):
{job_text}

Current resume JSON:
{content_json}"""


def _opener_banlist_hits(letter: str) -> list[str]:
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
    text = (letter or "").strip()
    if not text:
        return ""
    chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if chunks:
        return chunks[-1]
    return text


def _count_body_paragraphs(letter: str) -> int:
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

    Returns (rewritten_or_original, score_1_to_10). Score > 6 = acceptable.
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
        from llm_client import call_llm  # type: ignore[import]
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
    if not LLM_API_KEY:
        return ""
    try:
        from llm_client import call_llm  # type: ignore[import]
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
    """Review and optionally rewrite cover_letter_en up to max_rounds times."""
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
        from hunter.notify import notify
        print(f"[apply_agent] Cover letter rewritten (final score={final_score}), updating PL translation…")
        notify(f"✍️ Cover letter rewritten after review (score was {final_score}/10)")
        pl = _translate_cover_letter_pl(letter)
        if pl:
            content["cover_letter_pl"] = pl

    return content


def _resume_to_text(resume_en) -> str:
    """Flatten resume_en dict to plain text for ATS keyword scoring."""
    if isinstance(resume_en, str):
        return resume_en
    if not isinstance(resume_en, dict):
        return str(resume_en)
    parts: list[str] = []
    if summary := resume_en.get("summary"):
        parts.append(str(summary))
    if skills := resume_en.get("skills"):
        if isinstance(skills, dict):
            parts.extend(str(v) for v in skills.values())
        else:
            parts.append(str(skills))
    for exp in resume_en.get("experience") or []:
        if isinstance(exp, dict):
            parts.append(exp.get("title", ""))
            parts.append(exp.get("stack_line", ""))
            parts.extend(exp.get("bullets") or [])
    if education := resume_en.get("education"):
        parts.append(str(education))
    if courses := resume_en.get("courses"):
        parts.append(str(courses))
    return "\n".join(p for p in parts if p)


def _ats_check_loop(content: dict, job_text: str) -> dict:
    """Run independent ATS check; rewrite resume if score < 95%."""
    from hunter import ats_checker  # type: ignore[import]

    resume_en = content.get("resume_en", "")
    if not resume_en:
        print("[apply_agent] ATS check skipped — no resume_en in content")
        return content

    for attempt in range(1, _ATS_MAX_ROUNDS + 2):
        run_llm = attempt == 1 and bool(LLM_API_KEY)
        result = ats_checker.check(
            job_text=job_text,
            resume_text=_resume_to_text(resume_en),
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            run_llm_review=run_llm,
        )
        print(f"[apply_agent] ATS check (attempt {attempt}):\n{result.summary()}")
        content["ats_check"] = result.to_dict()

        if result.passed(_ATS_THRESHOLD):
            from hunter.notify import notify
            notify(f"✅ ATS check passed: {result.score:.1f}% (attempt {attempt})")
            break

        if attempt > _ATS_MAX_ROUNDS:
            from hunter.notify import notify
            notify(
                f"⚠️ ATS check: {result.score:.1f}% after {_ATS_MAX_ROUNDS} rewrites "
                f"(threshold {_ATS_THRESHOLD}%). Proceeding with best result."
            )
            break

        missing_str = "\n".join(f"  - {k}" for k in result.missing_keywords[:20]) or "  (none identified)"
        recs_str = "\n".join(f"  - {r}" for r in result.recommendations) or "  (none)"
        rewrite_msg = _ATS_REWRITE_PROMPT.format(
            score=result.score,
            threshold=_ATS_THRESHOLD,
            missing=missing_str,
            recs=recs_str,
            gap=result.llm_gap_report or "N/A",
            job_text=job_text[:3000],
            content_json=json.dumps(
                {k: content[k] for k in ("resume_en", "resume_pl", "skills", "stack", "ats_score") if k in content},
                ensure_ascii=False,
            )[:4000],
        )
        try:
            from llm_client import call_llm  # type: ignore[import]
            print(f"[apply_agent] ATS rewrite attempt {attempt}/{_ATS_MAX_ROUNDS}...")
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
            for key in ("resume_en", "resume_pl", "cover_letter_en", "cover_letter_pl",
                        "ats_score", "stack", "to_learn", "skills"):
                if boosted.get(key):
                    content[key] = boosted[key]
            resume_en = content.get("resume_en", resume_en)
        except Exception as e:
            print(f"[apply_agent] ATS rewrite failed: {e}")
            break

    return content


# ── Output folder helpers ────────────────────────────────────────────────────

_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_folder_company(name: str) -> str:
    s = _INVALID_FOLDER_CHARS.sub("_", (name or "").strip())
    s = s.strip("._ ")[:120] or "Unknown"
    return s


def compute_output_folder(company_name: str) -> Path:
    """Return Applications/{date}/{Company} path, adding _2/_3 suffix if needed."""
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


# ── Content validation ───────────────────────────────────────────────────────

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


# ── JobLeads MANUAL flow ─────────────────────────────────────────────────────

APPLY_MANUAL_EXIT_CODE = 44
PASTE_NO_URL_PLACEHOLDER = "paste://no-url"

REACT_SKIP_FORCE_HINT = (
    "\n\n📌 <b>Нужны документы всё равно?</b> В Telegram:\n"
    "• <code>/force</code> и тот же URL (строка 🔗 выше), или\n"
    "• <code>/force</code> и сразу под ним полный текст вакансии (как при обычной вставке).\n"
    "Так включается <code>--force</code> (без React-only); для JobLeads подтянется "
    "<code>job_posting.txt</code>, если ты его уже заполнял."
)


def handle_jobleads_fetch_blocked(
    url: str,
    err: str,
    meta_company: str = "",
    meta_title: str = "",
) -> None:
    """Stub job_posting.txt + MANUAL tracker row; Telegram instructs user; process exits 44."""
    from hunter.tracker import (
        add_manual_jobleads_pending,
        has_manual_pending,
        lookup_url,
        manual_jobleads_job_posting_path,
    )
    from hunter.notify import notify
    from job_fetch.jobleads import JOBLEADS_PASTE_MARKER  # type: ignore[import]

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

    company_folder = _sanitize_folder_company(meta_company or "Unknown")
    title = (meta_title or "Unknown").strip() or "Unknown"
    output_folder = compute_output_folder(company_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    stub = output_folder / "job_posting.txt"
    stub.write_text(
        f"URL: {url}\n\n"
        f"Company (from listing): {meta_company or '—'}\n"
        f"Title (from listing): {meta_title or '—'}\n\n"
        "JobLeads blocks automatic download (Cloudflare).\n"
        "Open the job in your browser, copy the full posting, and paste it below the marker line.\n\n"
        f"{JOBLEADS_PASTE_MARKER}\n\n",
        encoding="utf-8",
    )

    written = add_manual_jobleads_pending(
        url=url,
        company=meta_company or "Unknown",
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
