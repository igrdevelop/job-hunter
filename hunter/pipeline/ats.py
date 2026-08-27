"""
hunter/pipeline/ats.py — deterministic ATS keyword loop for the apply
pipeline. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.

``build_ats_keyword_checklist()`` / ``_ats_check_loop()`` deliberately
re-read ``_filter_self_description_keywords`` from ``hunter.apply_shared``
(not a plain module-global call) at call time: that module remains the
attribute tests.test_apply_shared.py monkeypatches directly, and a bare
in-module call would silently stop observing that patch once this function
moved out of apply_shared.py.
"""

from __future__ import annotations

import json

from hunter import gen_profile
from hunter.pipeline.profiles import _llm_p
from hunter.pipeline.scrubs import _COMPLIANCE_CLAIM_RE

# Defaults below (ats.threshold / ats.honest_rounds / ats.checklist_cap /
# ats.total_rounds in generation.yaml) are read at CALL time inside the
# functions that use them, not stashed here as module constants — a
# default-arg snapshot would freeze the value at import time and stop
# observing a profile change within the same process (see
# hunter.filters._resolve_flt's docstring for why this matters).

# Regulatory / compliance terms that job postings list as the EMPLOYER's own
# credentials ("we work in accordance with DORA, RODO"). The ATS keyword extractor
# picks them up as job keywords, and the aggressive rewrite would inject them into
# the candidate's Skills as if they were personal expertise — a fabrication. These
# are stripped from the ATS "missing keywords" so the rewrite never adds them.
# (Mirrors the RED LINE in prompts/generation_rules.md.)
_ATS_KEYWORD_BLOCKLIST = frozenset(
    {
        "dora",
        "rodo",
        "gdpr",
        "iso",
        "iso 27001",
        "iso27001",
        "soc2",
        "soc 2",
        "hipaa",
        "pci",
        "pci-dss",
        "pci dss",
    }
)


def _filter_self_description_keywords(keywords: list[str]) -> list[str]:
    """Drop employer-credential / regulatory terms that must not be claimed as
    the candidate's own skills (see _ATS_KEYWORD_BLOCKLIST)."""
    return [k for k in keywords if k.strip().lower() not in _ATS_KEYWORD_BLOCKLIST]


def build_ats_keyword_checklist(job_text: str) -> str:
    """Deterministic (regex-only, $0.00) keyword checklist for the FIRST
    generation prompt.

    The ATS keyword loop (_ats_check_loop) already extracts these same
    keywords and rewrites the resume until they're covered — but only AFTER
    a first draft that didn't see them misses them, burning 1-2 avoidable
    rewrite rounds. Handing the same deterministic list to the first call
    lets it get there in one shot most of the time; the rewrite loop remains
    the safety net, unchanged.

    Returns "" when no actionable keyword survives (posting has none, or
    every hit is an employer-credential term filtered by
    _filter_self_description_keywords) — callers should skip the block
    entirely rather than inject an empty checklist.
    """
    from hunter.apply_shared import _filter_self_description_keywords
    from hunter.ats_checker import extract_job_keywords

    keywords = _filter_self_description_keywords(extract_job_keywords(job_text))
    if not keywords:
        return ""
    # Cap on how many keywords go into the checklist (M3, docs/
    # LLM_COST_REDUCTION_PLAN.md) — keeps the prompt addition small even for
    # a keyword-dense posting.
    checklist_cap = gen_profile.get("ats.checklist_cap", 30)
    keywords = keywords[:checklist_cap]
    bullet_list = "\n".join(f"- {k}" for k in keywords)
    return (
        "\n\n## ATS keyword checklist (deterministic scan of this posting)\n"
        "Make sure EACH of these terms appears naturally in resume_en (skills "
        "and/or experience bullets). Do not fabricate experience — place "
        f"honestly:\n{bullet_list}"
    )


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
    """Deterministic ATS keyword loop: rewrite the resume only while the
    checker reports actionable missing keywords.

    Round 1-2: honest rewrite ("do NOT invent facts").
    Round 3:   soft-liar pass — synonyms, adjacent tech, exact JD phrasing.
    Round 4-5: aggressive pass — inject all missing keywords into Skills directly.

    Exit conditions (checked before every rewrite):
    - combined score ≥ threshold, OR
    - no actionable missing keywords (after the employer-credential blocklist).
      A rewrite can only ADD keywords; once none are missing the combined
      score is capped by TF-IDF, which no wording change meaningfully moves —
      prod data showed 88% of runs burning all 5 rewrites at keyword=100%.

    No LLM review runs inside this loop (pure regex + TF-IDF): the independent
    LLM verdict now happens ONCE, on the rendered PDF, after generate_docs
    (ats_pdf_roundtrip.run_llm_verdict).
    """
    from hunter import ats_checker
    from hunter.apply_shared import _filter_self_description_keywords

    _ATS_THRESHOLD = gen_profile.get("ats.threshold", 95.0)
    _ATS_MAX_ROUNDS = gen_profile.get("ats.honest_rounds", 2)
    _TOTAL_ROUNDS = gen_profile.get("ats.total_rounds", 5)  # rewrite rounds before final check

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
        return (
            r.get("experience")
            if isinstance(r, dict) and isinstance(r.get("experience"), list)
            else []
        )

    _orig_exp_en = copy.deepcopy(_exp_of(content.get("resume_en")))
    _orig_exp_pl = copy.deepcopy(_exp_of(content.get("resume_pl")))

    # Job text shown to the rewrite passes, with employer self-description /
    # regulatory terms removed so the LLM can't lift DORA/RODO/ISO from the posting
    # and inject them into the candidate's bullets. The ATS *checker* above still
    # gets the full, unmodified job_text.
    _rewrite_job_text = _COMPLIANCE_CLAIM_RE.sub("", job_text)[:3000]

    for attempt in range(1, _TOTAL_ROUNDS + 2):
        result = ats_checker.check(
            job_text=job_text,
            resume_text=resume_text_for_ats,
            run_llm_review=False,
        )
        print(f"[apply_agent] ATS check (attempt {attempt}):\n{result.summary()}")
        content["ats_check"] = result.to_dict()

        if result.passed(_ATS_THRESHOLD):
            break

        _missing_kw = _filter_self_description_keywords(result.missing_keywords)
        if not _missing_kw:
            print(
                "[apply_agent] ATS loop: all actionable keywords present "
                f"(keyword score {result.keyword_score:.1f}%) — no rewrite can "
                "improve the score, stopping"
            )
            break

        if attempt > _TOTAL_ROUNDS:
            break

        # _missing_kw is guaranteed non-empty here (the early-exit above breaks
        # when it's empty), so no "(none identified)" fallback is needed.
        missing_str = "\n".join(f"  - {k}" for k in _missing_kw[:20])
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
                # No LLM review runs inside this loop anymore, so llm_gap_report
                # is always empty — the placeholder is a constant by design.
                gap="N/A",
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
                provider=_llm_p().provider,
                model=_llm_p().model,
                api_key=_llm_p().api_key,
            )
            for key in ("resume_en", "resume_pl", "ats_score", "stack", "to_learn", "skills"):
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
