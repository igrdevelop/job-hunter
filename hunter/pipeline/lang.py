"""
hunter/pipeline/lang.py — language enforce-gate and the Polish-CV mirror
safety net for the apply pipeline. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.

``_translate_resume()`` / ``_translate_plain()`` deliberately re-read
``_translate_p`` from ``hunter.apply_shared`` (not a plain module-global
call) at call time, and ``ensure_pl_resume()`` does the same for
``_translate_resume``: that module remains the attribute several tests
monkeypatch directly (test_lang_enforce_gate.py, test_pl_resume_mirror.py),
and a bare in-module call would silently stop observing that patch once
these functions moved out of apply_shared.py.
"""

from __future__ import annotations

import json

from hunter import candidate

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
    from hunter.apply_shared import _translate_p

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
    from hunter.apply_shared import _translate_p

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
        from hunter.apply_shared import _translate_resume

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
