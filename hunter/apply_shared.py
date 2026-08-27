"""
hunter/apply_shared.py — shared helpers used by both apply_api and apply_cli.

Exported symbols used by apply_agent.py for backward compatibility:
    _already_processed, ApplyError, APPLY_MANUAL_EXIT_CODE, PASTE_NO_URL_PLACEHOLDER
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests  # noqa: F401 — kept for `apply_shared.requests.post` backward compat

from hunter import candidate
from hunter.best_effort import best_effort

# Kept as DIRECT (not re-exported) imports: hunter.pipeline.notify / .folders
# read these back from hunter.apply_shared dynamically at call time (see the
# docstrings there), so this module must stay their live source of truth —
# it's also the attribute path several tests monkeypatch
# (tests/conftest.py's autouse `_no_telegram` fixture, tests/test_apply_shared.py,
# tests/test_repost_gate.py, tests/test_cli_empty_run.py, the golden E2E tests).
from hunter.config import (  # noqa: F401
    APPLICATIONS_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_SEND_DOCS,
)

# ── Re-exports (docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) ────────────
# These symbols now live in hunter/pipeline/*; re-exported here for backward
# compat (32 call sites across the repo import them from hunter.apply_shared).
from hunter.pipeline.errors import (  # noqa: F401
    APPLY_LLM_OUTAGE_EXIT_CODE,
    APPLY_MANUAL_EXIT_CODE,
    APPLY_RATE_LIMITED_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    ApplyError,
    is_rate_limit_error,
    is_transient_fetch_error,
)
from hunter.pipeline.folders import (  # noqa: F401
    CANDIDATE_DIR,
    PROMPTS_DIR,
    _sanitize_folder_company,
    compute_output_folder,
)
from hunter.pipeline.ats import (  # noqa: F401
    _ats_check_loop,
    _filter_self_description_keywords,
    build_ats_keyword_checklist,
)
from hunter.pipeline.notify import notify, send_telegram_documents  # noqa: F401
from hunter.pipeline.profiles import _llm_p, _translate_p  # noqa: F401
from hunter.pipeline.scrubs import (  # noqa: F401
    _collapse_gloss_item,
    _dedup_skill_glosses,
    _prestige_claim_re,
    _scrub_compliance_clause,
    _scrub_prestige_text,
    _split_skill_items,
    _strip_compliance_claims,
    _strip_prestige_claims,
)
from hunter.pipeline.validate import REQUIRED_JSON_KEYS, validate_content  # noqa: F401

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


# ── Doomed-vacancy gate (docs/DOOMED_GATE_PLAN.md) ──────────────────────────────


def run_doomed_gate(
    job_text: str,
    url: str,
    *,
    title: str = "",
    company: str = "",
    is_force_override: bool = False,
) -> bool:
    """Deterministic full-text screen run right after expired-check, before any
    LLM call (Step 1.5f in both pipelines). Zero-cost (regex only) — see
    `hunter.filters.assess_job_text` for the rule families.

    Returns True when the caller should ABORT generation — a SKIP row has
    already been written to the tracker and Telegram notified. Returns False
    to continue (SOFT findings only, a HARD finding degraded to warn because
    of `is_force_override`, the gate is disabled, or it errored — best-effort,
    a gate failure never blocks an apply).

    `is_force_override`: True ONLY for `/force` (`skip_dedup`) — an explicit
    owner command meaning "generate this one anyway", so a HARD finding
    degrades to the same warn-but-allow behavior as SOFT (existing semantics
    shared with `screen_job_text`). A plain manual URL/text paste is NOT an
    override anymore (docs/DOOMED_GATE_PASTE_PLAN.md): a HARD finding on a
    pasted job now blocks generation exactly like an auto-discovered one —
    calibration showed real $ wasted on postings (Santander .NET+Angular,
    QuantumBlackMcKinsey fullstack/AI) that were pasted, not force-applied,
    and would have been caught by the new title-based HARD rule if paste had
    been treated the same as any other source.
    """
    from hunter.config import DOOMED_GATE_ENABLED, DOOMED_GATE_HARD_ACTION

    if not DOOMED_GATE_ENABLED:
        return False

    try:
        from hunter.filters import assess_job_text

        findings = assess_job_text(job_text, title=title, company=company)
    except Exception as e:  # noqa: BLE001 — best-effort, never block apply
        print(f"[apply_agent] Warning: doomed gate failed (continuing): {e}")
        return False

    if not findings:
        return False

    hard = [f for f in findings if f.severity == "hard"]
    soft = [f for f in findings if f.severity == "soft"]

    if hard and DOOMED_GATE_HARD_ACTION == "skip" and not is_force_override:
        finding = hard[0]
        reason = f'{finding.rule} — "{finding.evidence}"'
        notify(f"⛔ <b>Skipped before generation</b>\nReason: {reason}\n🔗 {url}")
        print(f"[apply_agent] SKIP (doomed gate, HARD) — {reason}: {url}")
        try:
            from hunter.models import Job
            from hunter.tracker import add_skipped

            add_skipped(
                Job(
                    title=title,
                    company=company,
                    location="",
                    salary=None,
                    url=url,
                    source="doomed_gate",
                )
            )
        except Exception as e:
            print(f"[apply_agent] Warning: could not write doomed-gate SKIP to tracker: {e}")
        return True

    # SOFT findings, or HARD degraded to warn (manual override / DOOMED_GATE_HARD_ACTION=warn):
    # generate anyway, just surface every finding in one Telegram message.
    warn_findings = hard + soft
    lines = "\n".join(f"• {f.rule}: {f.evidence}" for f in warn_findings[:5])
    degraded_note = " (force override — generating anyway)" if hard and is_force_override else ""
    notify(
        f"⚠️ <b>Heads-up — doomed-gate finding(s)</b>{degraded_note}\n"
        f"{lines}\n🔗 {url}\n\n"
        f"Generating documents anyway…"
    )
    print(f"[apply_agent] WARN (doomed gate) — {len(warn_findings)} finding(s): {url}")
    return False


def stack_gate_allows_manual(is_manual: bool, url: str, what: str) -> bool:
    """True when a STACK-mismatch gate must degrade to a warning instead of
    skipping, because the owner asked for this vacancy by hand.

    Owner decision 2026-08-24 (docs/STACK_PRESCREEN_PLAN.md M2): the auto-hunt
    keeps filtering React-only postings, but a URL the owner pasted himself is
    generated without argument -- he can read the title before sending it, and
    the measured cost of the other policy is real (37 of 38 React packages the
    bot generated on its own went unsent).

    Deliberately narrow: this relaxes stack rules only. The doomed gate's HARD
    rules (location / work authorization / language) still block a pasted
    posting, because that exception was REMOVED on purpose after calibration
    showed real money lost on pasted postings (docs/DOOMED_GATE_PASTE_PLAN.md).
    `/force` (skip_dedup) remains the override that bypasses everything.

    Call it as the last term of the gate condition -- it notifies, so it must
    run only once the gate has actually matched:

        if <gate matched> and not stack_gate_allows_manual(is_manual, url, "..."):
            <write the SKIP row and return>
    """
    if not is_manual:
        return False
    notify(
        f"⚠️ <b>{what}</b>\n"
        f"🔗 {url}\n"
        "You asked for this one by hand, so it is being generated anyway."
    )
    print(f"[apply_agent] {what}: manual request -- stack gate degraded to warn")
    return True


def run_prescreen(
    job_text: str,
    url: str,
    *,
    title: str = "",
    company: str = "",
    is_force_override: bool = False,
    is_manual: bool = False,
) -> bool:
    """Step 1.5h — one cheap-model read of the posting's stack, before generation.

    Returns True when the caller should ABORT (a SKIP row is already written and
    Telegram notified). False means carry on — and so does every failure path: an
    unavailable pre-screen must never cost a vacancy.

    Runs only after the deterministic gates have passed the posting, and only
    when the candidate's active tracks do not already cover React. `/force` and a
    manual request degrade it to a warning, exactly like the other stack gates
    (docs/STACK_PRESCREEN_PLAN.md M2).

    `PRESCREEN_MODE` stages the rollout — `report` logs, `warn` also notifies,
    `skip` acts. It ships at `warn`: a Telegram line per react-first posting IS
    the week of observation that earns the flip to `skip`, where `report` would
    pay for the call and show the owner nothing.
    """
    from hunter.config import (
        PRESCREEN_ENABLED,
        PRESCREEN_MIN_CONFIDENCE,
        PRESCREEN_MODE,
    )

    if not PRESCREEN_ENABLED or not (job_text or "").strip():
        return False

    from hunter.filters import _react_track_active

    if _react_track_active():
        return False  # React is a stack the candidate applies for — nothing to screen

    verdict = None
    acts = False
    with best_effort("apply.prescreen"):
        from hunter.prescreen import assess_stack, should_skip

        verdict = assess_stack(job_text, title=title)
        acts = should_skip(verdict, min_confidence=PRESCREEN_MIN_CONFIDENCE)

    if verdict is None:
        return False

    print(
        f"[prescreen] stack={verdict.primary_stack} verdict={verdict.verdict} "
        f"conf={verdict.confidence:.2f} usable={verdict.ok} seniority={verdict.seniority}"
    )
    if not acts:
        return False

    mode = PRESCREEN_MODE if PRESCREEN_MODE in ("report", "warn", "skip") else "report"
    quote = (verdict.evidence or "").strip()[:200]

    if mode == "report":
        print(f"[prescreen] would skip (report mode only): {url}")
        return False

    if is_force_override or is_manual or mode == "warn":
        why = (
            "you asked for this one by hand"
            if (is_manual or is_force_override)
            else "warn mode — not skipping yet"
        )
        notify(
            f"⚠️ <b>Pre-screen: React-first posting</b>\n"
            f"🔗 {url}\n"
            f"Generating anyway ({why}).\n"
            f"<i>{quote}</i>"
        )
        return False

    try:
        from hunter.tracker import add_react_skipped

        add_react_skipped(
            {
                "stack": f"React (pre-screen {verdict.confidence:.2f})",
                "company_name": company,
                "job_title": title,
            },
            url,
        )
    except Exception as e:  # noqa: BLE001 — a tracker failure must not crash the apply
        print(f"[prescreen] Warning: could not write the SKIP row: {e}")

    notify(
        f"⏭ <b>Skipped — React-first posting (pre-screen)</b>\n"
        f"🔗 {url}\n"
        f"<i>{quote}</i>"
        f"{_REACT_SKIP_FORCE_HINT}"
    )
    print(f"[prescreen] SKIP — react-first posting: {url}")
    return True


def abort_after_generation(
    folder: Path | None,
    url: str,
    *,
    reason: str,
    telegram_text: str = "",
    content: dict | None = None,
) -> bool:
    """Undo a package the pipeline decided to throw away AFTER it was rendered.

    The CLI pipeline's abort stages (React-only stack, company+title dedup,
    judge block, language-gate block) all run once the CLI skill has already
    rendered the documents AND written the tracker row -- `.claude/commands/
    apply.md` calls generate_docs.py WITHOUT `--no-tracker`, so the row exists
    inside the CLI call. Deleting the PDFs alone is not enough: the row stays
    APPLIED, exit 0 makes `apply_worker._resolve_outcome` see
    `has_successful_entry`, and the package is mirrored to Sheets and uploaded
    to Drive anyway (docs/STACK_PRESCREEN_PLAN.md M1 -- the 2026-08-24 Interia
    incident, and 5 more like it in the same two-week window).

    So this does all of it in one place: drop the rendered documents, settle the
    tracker row, notify. After it the parent's own terminal-row branch takes
    over: no delivery, and the URL stays deduped.

    `content` is the content.json the row was written from, and it is the
    IDENTITY -- `apply_url` and `output_folder` are the literal values
    `add_applied` stored. The pipeline's own `url` is only a fallback: paste
    mode never hands the skill a URL (the row lands with `url_norm=''`) and
    `.claude/commands/apply.md` lets the skill record the apply-button URL
    instead of the input one, so keying on it alone made this a guaranteed
    no-op for the whole paste flow.

    When no applied row could be converted, a terminal SKIP row is written here
    so no call site has to remember to. A run that ends with NO terminal row is
    not a harmless no-op: the worker clears the placeholder, the vacancy returns
    on the next hunt, the CLI regenerates the whole package and the same gate
    blocks again, forever -- the defect fixed for the backend-only pre-LLM skip
    on 2026-08-17 (one posting processed 8 times in 40 h).

    Kept on purpose: `job_posting.txt` and `content.json` (diagnostics, and the
    posting text the re-post gate reads -- a SKIP row can never become a donor).
    Only rendered output goes.

    Returns True when an applied row was converted.

    Wrapped in best_effort: the swallow is correct -- an abort must never become
    a FAIL -- but this path IS the fix for a delivery incident, so silent
    degradation has to surface as an alert. Settling NOTHING raises for exactly
    that reason: it is the failure mode with no exception of its own, and two of
    the four call sites cannot even see the return value.
    """
    if folder is not None:
        for path in list(folder.glob("*.pdf")) + list(folder.glob("*.docx")):
            try:
                path.unlink()
            except OSError as e:
                print(f"[apply_agent] abort: could not delete {path.name}: {e}")

    meta = content or {}
    row_url = (meta.get("apply_url") or "").strip() or url
    row_folder = (meta.get("output_folder") or "").strip() or (
        str(folder) if folder is not None else ""
    )

    converted = False
    with best_effort("apply.abort_undo"):
        from hunter.tracker import convert_own_applied_row

        converted = convert_own_applied_row(
            row_url if row_url and row_url != PASTE_NO_URL_PLACEHOLDER else "",
            folder=row_folder,
        )
        if not converted and not _write_abort_skip_row(row_url or url, meta):
            raise RuntimeError(
                f"post-generation abort settled nothing for {row_url or url!r} "
                f"(folder={row_folder!r}) - the applied row may still be delivered"
            )

    print(
        f"[apply_agent] ABORT after generation ({reason}) -- "
        f"docs dropped, applied row converted={converted}: {row_url or url}"
    )
    if telegram_text:
        notify(telegram_text)
    return converted


def _write_abort_skip_row(url: str, content: dict) -> bool:
    """Last resort when no applied row could be converted: write the SKIP row.

    True when a row was actually written. add_skipped returns None when an
    existing terminal row already covers this URL or its company+title -- and
    that is NOT good enough here: the row it is matching may be the very applied
    row this abort failed to convert, in which case reporting success would hide
    the original incident behind a false negative.
    """
    if not url or url == PASTE_NO_URL_PLACEHOLDER:
        return False
    from hunter.models import Job
    from hunter.tracker import add_skipped

    written = add_skipped(
        Job(
            title=(content.get("job_title") or "").strip(),
            company=(content.get("company_name") or "").strip(),
            location="",
            salary=None,
            url=url,
            source="post_generation_abort",
        )
    )
    return bool(written)


# ── Language enforce-gate ─────────────────────────────────────────────────────
# After generation + ATS rewrites, English fields can still contain Polish keywords
# (the ATS loop mirrors a Polish posting's keywords verbatim into resume_en). This
# gate detects contamination (hunter.lang_guard), repairs it by *translating* from
# the clean opposite-language counterpart, and — if strong contamination survives —
# signals the caller to BLOCK delivery rather than ship a broken document.

# Language pair shown in the translator's own system prompt — cosmetic wording
# only (the actual target language is always passed explicitly per call), but
# reads from candidate.yaml (languages.cv_languages) so a non-PL/EN candidate
# sees an accurate description. Default order reproduces the project owner's
# original "Polish/English" phrasing when candidate.yaml is absent.
_CV_LANG_NAMES = {"en": "English", "pl": "Polish", "de": "German", "fr": "French", "nl": "Dutch"}
_cv_lang_codes = candidate.get("languages.cv_languages", ["pl", "en"])
_cv_lang_names = [_CV_LANG_NAMES.get(str(c).lower(), str(c).title()) for c in _cv_lang_codes] or [
    "Polish",
    "English",
]

_RESUME_TRANSLATE_SYS = (
    f"You are a professional bilingual ({'/'.join(_cv_lang_names)}) resume translator. "
    f"You translate resume content between {' and '.join(_cv_lang_names)}. "
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
    _prof = _translate_p()
    if not _prof.api_key or not isinstance(source_resume, dict):
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
            provider=_prof.provider,
            model=_prof.model,
            api_key=_prof.api_key,
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
    _prof = _translate_p()
    if not _prof.api_key or not isinstance(text, str) or not text.strip():
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
            provider=_prof.provider,
            model=_prof.model,
            api_key=_prof.api_key,
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


def enforce_language_separation(content: dict) -> tuple[dict, bool, list[str]]:
    """Enforce-gate: each `_en` field must be clean English, each `_pl` field clean Polish.

    Repair strategy (language routing): when a contaminated field has a CLEAN
    opposite-language counterpart, regenerate it by translating the clean one — far
    more reliable than patching, with no re-fabrication or ATS keyword re-stuffing.
    Falls back to in-place cleanup translation when both sides are dirty.

    The repair direction is driven entirely by which side is actually contaminated
    (not by the posting language), so no posting-language argument is needed.

    Returns (content, blocked, report). `blocked=True` means strong Polish survived
    in an English field after repair — the caller must NOT ship the documents.
    """
    from hunter.lang_guard import scan_content, has_blocking_contamination, needs_repair

    report: list[str] = []
    scan = scan_content(content)
    if not needs_repair(scan):
        return content, False, report

    contaminated = sorted(
        set(scan.get("en_strong", {}))
        | set(scan.get("en_soft", {}))
        | set(scan.get("pl_english", {}))
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

        kind = (
            "cover letter"
            if "letter" in en_key
            else ("about-me text" if "about" in en_key else "text")
        )
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
        dirty_units = dict.fromkeys(k.split(".")[0] for k in final_scan.get("en_strong", {}))
        for unit_key in dirty_units:
            is_resume = en_keys.get(unit_key, False)
            src = content.get(unit_key)
            if not src:
                continue
            kind = (
                "cover letter"
                if "letter" in unit_key
                else ("about-me text" if "about" in unit_key else "text")
            )
            fixed = _retranslate(src, "EN", is_resume, kind)
            if fixed and fixed != src:
                content[unit_key] = fixed
                report.append(f"{unit_key}: cleaned in place (round {_round + 1})")

    # Final PL-repair pass: a Polish field whose English counterpart was ALSO dirty
    # was skipped by Round 0 (it only translates from an already-clean side). Now that
    # the EN side has been cleaned, translate any still-contaminated PL field from it.
    pl_scan = scan_content(content)

    def _en_strong_dirty(en_key: str) -> bool:
        bucket = pl_scan.get("en_strong", {})
        return any(p == en_key or p.startswith(en_key + ".") for p in bucket)

    for en_key, pl_key, is_resume in units:
        if (
            not _is_unit_clean(pl_scan, pl_key, "pl")
            and content.get(en_key)
            and not _en_strong_dirty(en_key)
        ):
            kind = (
                "cover letter"
                if "letter" in pl_key
                else ("about-me text" if "about" in pl_key else "text")
            )
            fixed = _retranslate(content[en_key], "PL", is_resume, kind)
            if fixed:
                content[pl_key] = fixed
                report.append(f"{pl_key}: re-translated from clean {en_key} (final PL pass)")

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


def ensure_pl_resume(content: dict, posting_lang: str) -> list[str]:
    """A Polish posting must ship a Polish CV — mirror it if the generator didn't.

    Both pipelines ask the generator for `resume_pl` on a PL posting, but neither
    can force it: the API path relies on the prompt, and the CLI skill
    (`.claude/commands/apply.md`) was told to return `"resume_pl": null` unless
    `--full` — an unconditional rule that also fired on Polish postings. Measured
    on the live corpus 2026-08-22: 15 of 250 PL applications shipped an English CV
    with a Polish cover letter, all of them from July onwards as prod moved onto
    the CLI path.

    Mirroring here rather than re-prompting keeps the fix independent of prompt
    compliance, and reuses the same cheap translate model + role-count guard the
    verdict-refine PL mirror uses (`_translate_resume`). Runs AFTER the judge and
    the language gate, so what gets translated is the already-verified EN text —
    no new fabrication surface. Best-effort: returns [] and changes nothing when
    the posting is not Polish, a PL resume is already present, or translation
    fails. Never raises.
    """
    if (posting_lang or "").upper() != "PL":
        return []
    resume_en = content.get("resume_en")
    if not isinstance(resume_en, dict) or not resume_en:
        return []
    existing = content.get("resume_pl")
    if isinstance(existing, dict) and existing:
        return []

    # Wrapped in best_effort (CLAUDE.md rule for new swallow-and-continue code):
    # a mirror that silently stops working recreates the exact bug this closes —
    # Polish employers receiving an English CV — and that went unnoticed for a
    # month the first time. The `raise` re-raises out of the local except so the
    # failure still reaches best_effort's counter; best_effort then swallows it.
    from hunter.best_effort import best_effort

    mirrored = None
    with best_effort("apply.pl_mirror"):
        try:
            mirrored = _translate_resume(
                resume_en, "PL", expected_roles=len(resume_en.get("experience") or [])
            )
        except Exception as e:
            print(f"[apply_agent] PL mirror failed (continuing with EN only): {e}")
            raise
    if not mirrored:
        print("[apply_agent] PL mirror returned nothing — continuing with EN only")
        return []
    content["resume_pl"] = mirrored
    return ["[PL] mirrored resume_pl from resume_en (generator returned none)"]


def build_pl_skip_instruction(posting_lang: str, *, full_mode: bool) -> str:
    """Prompt addition telling the generator to skip the _pl fields for an
    EN-language posting in short mode (docs/LLM_COST_REDUCTION_PLAN.md M4).

    Short mode never delivers the PL CV for an EN posting (see
    generate_docs.py's primary_lang-driven routing), so generating a full
    resume_pl/cover_letter_pl/about_me_pl is ~40-50% of the first call's
    output tokens spent on fields nobody receives. Returns "" (no prompt
    change) when the flag is off, the posting is PL, or full_mode is set —
    those cases still get the complete bilingual set exactly as before.
    """
    from hunter.config import GEN_SKIP_PL_FOR_EN

    if not GEN_SKIP_PL_FOR_EN or full_mode or posting_lang != "EN":
        return ""
    return (
        "\n\n**Language optimization:** this posting is in English and the "
        "Polish documents will not be delivered for it. Return empty values "
        "for the Polish fields — "
        '"resume_pl": {}, "cover_letter_pl": "", "about_me_pl": "" — '
        "and put your full effort into resume_en, cover_letter_en, and "
        "about_me_en instead."
    )


# ── JobLeads MANUAL flow ──────────────────────────────────────────────────────


def _handle_jobleads_fetch_blocked(url: str, err: str, company: str = "", title: str = "") -> None:
    """Stub job_posting.txt + MANUAL tracker row; Telegram instructs user; process exits 44."""
    from hunter.tracker import (
        _is_known_terminal,
        add_manual_jobleads_pending,
        has_manual_pending,
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

    # A PENDING/IN_PROGRESS placeholder for THIS url (M1, queue mode — the
    # worker's own claim row) must not trip this dedup check; only a genuine
    # terminal row (FAIL/SKIP/MANUAL/score/...) means "already tracked".
    if _is_known_terminal(url):
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
