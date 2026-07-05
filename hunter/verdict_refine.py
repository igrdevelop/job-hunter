"""hunter/verdict_refine.py — dожать independent verdict with honest edits.

See docs/VERDICT_REFINE_PLAN.md for the full rationale (in Russian, for the
project owner). Short version: the independent PDF verdict
(``hunter.ats_pdf_roundtrip.run_llm_verdict``) used to be computed once and
just recorded. Half of its "missing keyword" feedback is presentational
(the candidate genuinely has the skill, the resume just didn't surface it
clearly), so this module closes the loop: rewrite → re-render → re-verdict,
keeping only improvements.

Two escalating rounds (see key decisions in the plan doc):
  round 1 (honest)  — only facts already supported by candidate_profile.md.
  round 2 (stretch) — may add posting technologies absent from the profile,
                       as plain skills/summary entries; every addition is
                       tracked in content["to_learn"]. May be woven into ONE
                       flexible Altoros project (2018-2022); never into the
                       recent/verifiable employers (Atruvia, Fairmarkit,
                       Intel, SII, SolbegSoft).

Both functions are pure orchestration: no Telegram, no tracker writes. The
caller (apply_api / apply_cli) decides what to notify and persists the
final content.json.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Callable

from hunter.apply_shared import PROMPTS_DIR, _llm_p, validate_content

# Recommendations the independent verdict sometimes returns that no CV edit
# can fix — they're facts about the candidate/logistics, not about the text.
# Dropped deterministically before the feedback ever reaches the rewrite
# prompt (plan decision #5).
_DROP_RE = re.compile(
    r"relocat|\bhybrid\b|\bon-?site\b|\bonsite\b|\blocation\b|"
    r"cover\s+(?:note|letter)|linkedin|\byears?\s+of\s+experience\b",
    re.IGNORECASE,
)

# Recent, verifiable employers — never touched by a round-2 stretch addition.
_PROTECTED_EMPLOYERS = ("Atruvia", "Fairmarkit", "Intel", "SII", "SolbegSoft")

# Flexible Altoros client projects a round-2 stretch tech MAY be woven into
# (2018-2022 — era-plausible, owner-approved precedent: React prototypes in
# the E-commerce project).
_ALTOROS_FLEXIBLE_PROJECTS = ("E-commerce", "Insurance", "Healthcare", "Grant Management")


def _is_actionable(item: object) -> bool:
    return isinstance(item, str) and bool(item.strip()) and not _DROP_RE.search(item)


def build_refine_feedback(verdict: dict) -> str | None:
    """Turn a verdict dict into rewrite-prompt feedback text.

    Combines ``missing_keywords`` + ``recommendations`` (deterministically
    dropping non-CV items — location/relocation/hybrid/on-site/cover-note/
    LinkedIn/years-of-experience, see ``_DROP_RE``) plus ``gap_report`` as
    context. Returns None when nothing actionable survives — the refine loop
    then no-ops without spending an LLM call.
    """
    if not isinstance(verdict, dict):
        return None

    missing = [k for k in (verdict.get("missing_keywords") or []) if _is_actionable(k)]
    recs = [r for r in (verdict.get("recommendations") or []) if _is_actionable(r)]
    if not missing and not recs:
        return None

    parts: list[str] = []
    if missing:
        parts.append(
            "Missing keywords flagged by the independent ATS verdict:\n"
            + "\n".join(f"- {k}" for k in missing)
        )
    if recs:
        parts.append("Verdict recommendations:\n" + "\n".join(f"- {r}" for r in recs))
    gap = str(verdict.get("gap_report") or "").strip()
    if gap:
        parts.append(f"Verdict gap summary: {gap}")
    return "\n\n".join(parts)


def _load_system_prompt() -> str:
    """Same system prompt as the original generation call (candidate_profile
    + generation_rules) so the rewrite is grounded in the same facts."""
    instructions = (PROMPTS_DIR / "generation_rules.md").read_text(encoding="utf-8")
    profile_path = PROMPTS_DIR / "candidate_profile.md"
    if profile_path.exists():
        return profile_path.read_text(encoding="utf-8") + "\n\n---\n\n" + instructions
    return instructions


_HONEST_BLOCK = """ROUND {round} — HONEST REFINEMENT

Only make claims fully supported by the candidate profile above. Do NOT add
any technology, tool, employer, project, or metric that isn't already true
of the candidate — you are only making existing, real skills/experience
MORE VISIBLE where the independent ATS verdict says they're under-represented.

Return the complete JSON: {{"resume_en": <same schema as the current resume_en, fully populated>}}."""

_STRETCH_BLOCK = """ROUND {round} — STRETCH ESCALATION (owner-approved)

You may now ADD technologies named in the job posting / missing-keywords list
that are NOT in the candidate profile, as PLAIN entries in resume_en.skills
and/or resume_en.summary — no hedging language such as "familiar with" or
"working knowledge of".

Every technology you add this way MUST also be listed in a top-level JSON
field "stretch_additions" (a flat array of strings, one per added
technology) so it can be tracked as the candidate's learning debt.

Default placement: Skills section and/or summary. If (and only if) a
technology needs experience-level grounding, you MAY weave it into ONE of
these flexible Altoros client projects (2018-2022, choose the single most
era/stack-plausible one): {altoros_projects} — add it to that project's
Stack line and/or ONE modest bullet. NEVER invent numbers, metrics, or scale
that aren't already there.

NEVER touch these employers — recent, verifiable, off-limits for ANY
addition: {protected_employers}.

NEVER invent employers, projects, metrics, or years — on any round.

Return the complete JSON:
{{"resume_en": <same schema as the current resume_en, fully populated>, "stretch_additions": [<strings>]}}."""


def _round_block(round_num: int, kind: str) -> str:
    if kind == "stretch":
        return _STRETCH_BLOCK.format(
            round=round_num,
            altoros_projects=", ".join(_ALTOROS_FLEXIBLE_PROJECTS),
            protected_employers=", ".join(_PROTECTED_EMPLOYERS),
        )
    return _HONEST_BLOCK.format(round=round_num)


def _exp_len(resume: object) -> int:
    if isinstance(resume, dict) and isinstance(resume.get("experience"), list):
        return len(resume["experience"])
    return 0


def _rewrite_round(content: dict, job_text: str, feedback: str, *, round_num: int, kind: str) -> dict | None:
    """One LLM call: rewrite resume_en per the round's policy. Raises on
    transport/LLM errors so the caller's best-effort wrapper can stop the
    whole loop (a broken rewrite call is not worth retrying mid-round)."""
    from llm_client import call_llm

    system_prompt = _load_system_prompt()
    block = _round_block(round_num, kind)
    resume_en = content.get("resume_en") or {}
    user_message = (
        "The independent ATS verdict — an assessor model that did NOT write "
        "this resume — scored the rendered PDF and flagged these gaps:\n\n"
        f"{feedback}\n\n{block}\n\n"
        f"Job posting:\n{job_text[:6000]}\n\n"
        f"Current resume_en JSON:\n{json.dumps(resume_en, ensure_ascii=False)}"
    )
    result = call_llm(
        system_prompt=system_prompt,
        user_message=user_message,
        provider=_llm_p().provider,
        model=_llm_p().model,
        api_key=_llm_p().api_key,
    )
    if not isinstance(result, dict) or not isinstance(result.get("resume_en"), dict):
        return None
    return result


def _merge_to_learn(content: dict, additions: object) -> None:
    """Append round-2 stretch additions to content["to_learn"] (deduped,
    comma-joined) so they reach the tracker's To Learn column."""
    if not isinstance(additions, list) or not additions:
        return
    existing = str(content.get("to_learn") or "").strip()
    items = [s.strip() for s in existing.split(",") if s.strip()] if existing else []
    for add in additions:
        add = str(add).strip()
        if add and add not in items:
            items.append(add)
    content["to_learn"] = ", ".join(items)


def _run_safety_stages(content: dict, job_text: str, base_cv: str) -> tuple[dict, bool, list[str]]:
    """Re-run sanitize -> scrubs -> claim judge -> language gate on a revised
    content dict, mirroring the generation pipeline's own post-processing so
    a rewrite can't reintroduce a fabrication or a language-contamination
    bug the pipeline already guards against. Returns (content, blocked, log).

    The refine loop never blocks the pipeline on its own — JUDGE_MODE=block
    is capped to "warn" here; the language gate below is the only hard stop
    (a round with surviving Polish-in-English is discarded, not shipped).
    """
    report: list[str] = []

    try:
        from hunter.resume_sanitizer import sanitize_content
        content = sanitize_content(content)
    except Exception as e:  # noqa: BLE001 — best-effort
        report.append(f"sanitize failed: {e}")

    try:
        from hunter.apply_shared import (
            _dedup_skill_glosses,
            _strip_compliance_claims,
            _strip_prestige_claims,
        )
        content, fixes = _strip_compliance_claims(content)
        report.extend(fixes)
        content, fixes = _strip_prestige_claims(content, job_text)
        report.extend(fixes)
        content, fixes = _dedup_skill_glosses(content)
        report.extend(fixes)
    except Exception as e:  # noqa: BLE001
        report.append(f"scrub failed: {e}")

    try:
        from hunter.claim_judge import run_judge_stage
        from hunter.config import JUDGE_ENABLED, JUDGE_MODE
        mode = "warn" if JUDGE_MODE == "block" else JUDGE_MODE
        outcome = run_judge_stage(content, job_text, base_cv, enabled=JUDGE_ENABLED, mode=mode)
        content = outcome.content
        report.extend(outcome.fixes)
    except Exception as e:  # noqa: BLE001
        report.append(f"judge failed: {e}")

    blocked = False
    try:
        from hunter.apply_shared import enforce_language_separation
        content, blocked, lang_report = enforce_language_separation(content)
        report.extend(lang_report)
    except Exception as e:  # noqa: BLE001
        report.append(f"language gate failed: {e}")

    return content, blocked, report


def _rollback(content_path: Path, best_content: dict, folder: Path, regenerate_docs: Callable[[Path], None]) -> None:
    try:
        content_path.write_text(
            json.dumps(best_content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        regenerate_docs(folder)
    except Exception as e:  # noqa: BLE001 — best-effort
        print(f"[verdict_refine] rollback regen failed: {e}")


def refine_loop(
    content: dict,
    job_text: str,
    base_cv: str,
    folder: Path,
    verdict: dict,
    *,
    regenerate_docs: Callable[[Path], None],
    target: float = 95.0,
    max_rounds: int = 1,
) -> tuple[dict, dict]:
    """Round N (1-based): round 1 = HONEST, round 2+ = STRETCH.

    Keep-best guard: a round is accepted only if the new verdict score is
    STRICTLY greater than the current best; otherwise content.json + the
    rendered docs are rolled back to the pre-round version. Round 2 always
    starts from the best version so far, even if round 1 was rolled back.

    Best-effort: any exception inside a round aborts the WHOLE loop and
    returns the current best (content, verdict) pair — a round that merely
    fails a soft check (language-gate block, validation regression, no score
    improvement) instead just discards that round and lets the next round
    try again from the best version.

    Returns (final_content, final_verdict). A no-op (verdict already at
    target, or max_rounds <= 0) makes zero LLM calls.
    """
    if max_rounds <= 0 or not isinstance(verdict, dict):
        return content, verdict

    from hunter.ats_pdf_roundtrip import run_llm_verdict

    content_path = folder / "content.json"
    best_content = content
    best_verdict = verdict

    for round_num in range(1, max_rounds + 1):
        try:
            score = float(best_verdict.get("score") or 0)
            if score >= target:
                break

            feedback = build_refine_feedback(best_verdict)
            if feedback is None:
                print(f"[verdict_refine] round {round_num}: no actionable feedback — stopping")
                break

            kind = "honest" if round_num == 1 else "stretch"
            print(
                f"[verdict_refine] round {round_num} ({kind}): "
                f"verdict {score} < target {target} — rewriting..."
            )

            revised = _rewrite_round(best_content, job_text, feedback, round_num=round_num, kind=kind)
            if revised is None:
                print(f"[verdict_refine] round {round_num}: rewrite returned no usable resume — stopping")
                break

            candidate = copy.deepcopy(best_content)
            candidate["resume_en"] = revised["resume_en"]

            if _exp_len(candidate["resume_en"]) < _exp_len(best_content.get("resume_en")):
                print(f"[verdict_refine] round {round_num}: rewrite dropped roles — discarding round")
                continue

            if kind == "stretch":
                _merge_to_learn(candidate, revised.get("stretch_additions"))

            candidate, blocked, safety_report = _run_safety_stages(candidate, job_text, base_cv)
            for line in safety_report:
                print(f"[verdict_refine] round {round_num}: {line}")
            if blocked:
                print(f"[verdict_refine] round {round_num}: language gate blocked — discarding round")
                continue

            if len(validate_content(candidate)) > len(validate_content(best_content)):
                print(f"[verdict_refine] round {round_num}: rewrite broke validation — discarding round")
                continue

            content_path.write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            regenerate_docs(folder)
            new_verdict = run_llm_verdict(folder=folder, job_text=job_text)
            new_score = float(new_verdict.get("score")) if isinstance(new_verdict, dict) else None

            if new_score is not None and new_score > score:
                print(f"[verdict_refine] round {round_num}: verdict improved {score} -> {new_score} — accepted")
                best_content, best_verdict = candidate, new_verdict
            else:
                print(
                    f"[verdict_refine] round {round_num}: verdict did not improve "
                    f"({score} -> {new_score}) — rolling back"
                )
                _rollback(content_path, best_content, folder, regenerate_docs)
        except Exception as e:  # noqa: BLE001 — best-effort: stop, keep current best
            print(f"[verdict_refine] round {round_num} failed unexpectedly (keeping best): {e}")
            break

    # PL mirror — ONCE, after the loop, and only if at least one round was
    # actually accepted (best_content is no longer the object the caller
    # passed in). Doing this per-round (as before) spent a translation call
    # on rounds that got rolled back anyway; the local re-render below is
    # free, so it's cheaper to mirror once at the end than to translate on
    # every round.
    if best_content is not content and str(best_content.get("primary_lang") or "").upper() == "PL":
        try:
            from hunter.apply_shared import _translate_resume
            mirrored = _translate_resume(
                best_content["resume_en"], "PL", expected_roles=_exp_len(best_content.get("resume_en"))
            )
            if mirrored:
                best_content["resume_pl"] = mirrored
                content_path.write_text(
                    json.dumps(best_content, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                regenerate_docs(folder)
        except Exception as e:  # noqa: BLE001 — best-effort
            print(f"[verdict_refine] final PL mirror failed (continuing): {e}")

    return best_content, best_verdict
