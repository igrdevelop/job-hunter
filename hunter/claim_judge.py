"""
hunter/claim_judge.py — LLM-as-judge verification pass for generated CVs.

A second, cheap model (JUDGE_MODEL, default Haiku) checks every claim in the
generated content against the two ground-truth sources the pipeline already has
— the candidate profile and the job posting — and returns a structured list of
violations. This closes the *class* of fabrications the deterministic regex
scrubs (`_strip_prestige_claims`, `_strip_compliance_claims`,
`_dedup_skill_glosses`) chase one phrasing at a time.

Public API (mirrors the scrub contract `tuple[dict, list[str]]` where possible):

    judge_content(content, job_text)            -> JudgeReport
    repair_content(content, report, job_text)   -> (content, list[str])

Both are best-effort: any exception is the caller's signal to continue with the
unjudged content. The judge must never be the reason an application fails.

Pipeline placement: after the deterministic scrubs and BEFORE the language
enforce-gate (a repair could introduce language drift; the gate stays the last
word on language). See docs/CV_JUDGE_PLAN.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunter.config import (
    JUDGE_MAX_REPAIR_ROUNDS,
    JUDGE_MODEL,
    LLM_API_KEY,
    LLM_PROVIDER,
)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Severities that justify a repair / block. `style` is report-only — the
# deterministic gloss-dedup owns it.
ACTIONABLE_SEVERITIES = frozenset({"fabrication", "exaggeration"})
_VALID_SEVERITIES = frozenset({"fabrication", "exaggeration", "style"})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    field: str        # dotted path, e.g. "resume_en.experience[2].bullets[1]"
    quote: str        # verbatim substring of the offending text
    reason: str       # one-line human-readable explanation
    severity: str     # "fabrication" | "exaggeration" | "style"

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "quote": self.quote,
            "reason": self.reason,
            "severity": self.severity,
        }


@dataclass
class JudgeReport:
    violations: list[Violation] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> list[Violation]:
        """Violations that warrant repair/block (fabrication + exaggeration)."""
        return [v for v in self.violations if v.severity in ACTIONABLE_SEVERITIES]

    @property
    def fabrications(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "fabrication"]

    @property
    def passed(self) -> bool:
        """True when nothing actionable was found."""
        return not self.actionable

    def to_dict(self) -> dict[str, Any]:
        return {"violations": [v.to_dict() for v in self.violations]}

    def telegram_summary(self, url: str) -> str:
        if self.passed:
            return f"✅ <b>Claim judge: clean</b>\n🔗 {url}"
        lines = []
        for v in self.actionable[:5]:
            icon = "🚫" if v.severity == "fabrication" else "⚠️"
            lines.append(f"{icon} <b>{v.field}</b>: {v.reason[:120]}")
        return (
            f"⚖️ <b>Claim judge: {len(self.actionable)} issue(s)</b>\n"
            f"🔗 {url}\n\n" + "\n".join(lines)
        )


# ---------------------------------------------------------------------------
# Field iteration + dotted-path resolution
# ---------------------------------------------------------------------------

# Skill categories whose value is a comma-joined string of keywords; these are
# the ATS-mirroring injection point and worth judging. `languages` is excluded
# (language proficiency names are expected, not claims).
def iter_judged_fields(content: dict[str, Any]) -> dict[str, str]:
    """Flatten the judged subset of content into {dotted_path: text}.

    Judged: summary, skills.*, every experience[i].bullets[j] for resume_en and
    resume_pl (when present); cover_letter_en/pl; about_me_en/pl. Verbatim-locked
    fields (company/period/title/education) are excluded — they're checked by
    validate_content / content_qa.
    """
    out: dict[str, str] = {}

    for rk in ("resume_en", "resume_pl"):
        resume = content.get(rk)
        if not isinstance(resume, dict):
            continue
        summary = resume.get("summary")
        if isinstance(summary, str) and summary.strip():
            out[f"{rk}.summary"] = summary
        skills = resume.get("skills")
        if isinstance(skills, dict):
            for cat, val in skills.items():
                if cat == "languages":
                    continue
                text = val if isinstance(val, str) else (
                    ", ".join(str(i) for i in val) if isinstance(val, list) else ""
                )
                if text.strip():
                    out[f"{rk}.skills.{cat}"] = text
        for i, role in enumerate(resume.get("experience") or []):
            if not isinstance(role, dict):
                continue
            for j, bullet in enumerate(role.get("bullets") or []):
                if isinstance(bullet, str) and bullet.strip():
                    out[f"{rk}.experience[{i}].bullets[{j}]"] = bullet

    for ak in ("cover_letter_en", "cover_letter_pl", "about_me_en", "about_me_pl"):
        val = content.get(ak)
        if isinstance(val, str) and val.strip():
            out[ak] = val

    return out


_PATH_TOKEN_RE = re.compile(r"([a-zA-Z_]+)(?:\[(\d+)\])?")


def _resolve_path(content: dict[str, Any], path: str):
    """Return (holder, key) for a dotted field path so the caller can read or
    assign content[...][key]. Returns (None, None) if the path does not resolve.

    Supports "a.b.c" and "a.b[2].c[3]" forms (list indices in brackets).
    """
    parts = path.split(".")
    cur: Any = content
    holder: Any = None
    key: Any = None
    for part in parts:
        m = _PATH_TOKEN_RE.fullmatch(part)
        if not m:
            return None, None
        name, idx = m.group(1), m.group(2)
        if not isinstance(cur, dict) or name not in cur:
            return None, None
        holder, key = cur, name
        cur = cur[name]
        if idx is not None:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                return None, None
            holder, key = cur, i
            cur = cur[i]
    return holder, key


def _field_text(content: dict[str, Any], path: str) -> str | None:
    holder, key = _resolve_path(content, path)
    if holder is None:
        return None
    val = holder[key]
    return val if isinstance(val, str) else None


def quote_survives(content: dict[str, Any], path: str, quote: str) -> bool:
    """True if `quote` still appears verbatim in the named field after a repair.

    Cheap post-repair re-check (no second LLM call): used by the pipeline to
    decide whether a flagged fabrication actually survived the repair round.
    """
    text = _field_text(content, path)
    return bool(text and quote and quote in text)


# ---------------------------------------------------------------------------
# Judge call
# ---------------------------------------------------------------------------

def _load_rules() -> str:
    path = PROMPTS_DIR / "judge_rules.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _load_profile() -> str:
    path = PROMPTS_DIR / "candidate_profile.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _build_user_message(fields: dict[str, str], job_text: str, base_cv: str = "") -> str:
    parts = [
        "## Candidate profile (ground truth)\n",
        _load_profile(),
    ]
    if base_cv:
        parts += ["\n\n## Base CV bullets (approved phrasings)\n", base_cv]
    parts += [
        "\n\n## Job posting\n",
        job_text or "(none)",
        "\n\n## Generated content fields to verify (JSON)\n",
        json.dumps(fields, ensure_ascii=False, indent=2),
        "\n\nReturn ONLY the violations JSON object.",
    ]
    return "".join(parts)


def _parse_violations(raw: dict[str, Any], fields: dict[str, str]) -> list[Violation]:
    """Build validated Violation objects from the judge's raw JSON.

    A finding is dropped (with no error) when: severity is unknown, the field
    name is not one we submitted, or the quote is not a verbatim substring of
    that field. This neutralises judge hallucinations deterministically.
    """
    out: list[Violation] = []
    items = raw.get("violations")
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        fld = str(it.get("field", "")).strip()
        quote = str(it.get("quote", "")).strip()
        reason = str(it.get("reason", "")).strip()
        severity = str(it.get("severity", "")).strip().lower()
        if severity not in _VALID_SEVERITIES:
            continue
        if fld not in fields:
            continue
        if not quote or quote not in fields[fld]:
            # Quote must be verbatim — otherwise we can't trust or repair it.
            continue
        out.append(Violation(field=fld, quote=quote, reason=reason, severity=severity))
    return out


def judge_content(content: dict[str, Any], job_text: str, base_cv: str = "") -> JudgeReport:
    """Verify generated claims against the profile + posting. Best-effort.

    Returns a JudgeReport. On any failure (no API key, LLM error, bad JSON) the
    report is empty/passing so the caller continues unharmed.
    """
    fields = iter_judged_fields(content)
    if not fields:
        return JudgeReport()

    try:
        from llm_client import call_llm
        raw = call_llm(
            system_prompt=_load_rules(),
            user_message=_build_user_message(fields, job_text, base_cv),
            provider=LLM_PROVIDER,
            model=JUDGE_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=2048,
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        print(f"[claim_judge] judge call failed (skipping): {e}")
        return JudgeReport()

    violations = _parse_violations(raw if isinstance(raw, dict) else {}, fields)
    return JudgeReport(violations=violations, raw=raw if isinstance(raw, dict) else {})


# ---------------------------------------------------------------------------
# Repair — deterministic clause-drop first, LLM rewrite only for broken fields
# ---------------------------------------------------------------------------

# Connector words that introduce a tacked-on clause ("...banks AND Fortune 500
# firms", "dev SERVING Fortune 500 clients"). Used as a left clause boundary so
# the drop excises only the embellishment, never the honest clause before it.
_DROP_CONNECTORS = (
    "and", "or", "including", "serving", "for", "with", "as well as",
    "such as", "like", "alongside",
    "oraz", "i", "w tym", "takich jak", "dla", "wraz z",
)
_DROP_CONNECTOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in _DROP_CONNECTORS) + r")\b",
    re.IGNORECASE,
)


def _drop_quote(text: str, quote: str) -> str:
    """Remove a fabricated quote from a sentence-ish text.

    Tier 1: drop the clause containing the quote, bounded on the left by the
    nearest punctuation OR connector word ("and"/"serving"/"including"...) so an
    honest preceding clause survives ("...300+ German banks and Fortune 500
    firms" → "...300+ German banks"). Mirrors `_scrub_prestige_text` intent.
    Tier 2: if removal would empty the text (quote spans the whole sentence),
    drop the whole sentence and let the LLM-rewrite tier refill the field.
    """
    if not quote or quote not in text:
        return text

    start = text.index(quote)
    end = start + len(quote)
    # If a connector word ("and"/"serving"/"such as"...) sits immediately before
    # the quote, drop it too — it only introduced the embellishment. We check the
    # IMMEDIATELY preceding word, not the whole clause, so an essential earlier
    # connector ("apps FOR 300+ German banks ...") is never swallowed.
    pre = text[:start]
    m = re.search(r"(\s*\b(?:" + "|".join(re.escape(c) for c in _DROP_CONNECTORS)
                  + r")\b\s*)$", pre, re.IGNORECASE)
    drop_start = start - len(m.group(1)) if m else start

    remainder = (text[:drop_start] + text[end:]).strip()
    if remainder and remainder not in (",", ";", ".", "-"):
        candidate = text[:drop_start] + text[end:]
    else:
        # Tier 2 — sentence drop (quote spans the whole sentence).
        parts = re.split(r"(?<=[.!?])\s+", text)
        candidate = " ".join(p for p in parts if quote not in p)

    # Cleanup: whitespace, double/dangling punctuation, trailing connectors.
    candidate = re.sub(r"\s{2,}", " ", candidate)
    candidate = re.sub(r"\s+([,.;:])", r"\1", candidate)        # " ,"  -> ","
    candidate = re.sub(r"([,;:])\s*(?=[,;:])", "", candidate)   # ", ," -> ","
    candidate = re.sub(r"[,;:]\s*([.!?])", r"\1", candidate)    # ",."  -> "."
    candidate = re.sub(
        r"\s+(?:and|or|including|serving|with|oraz|i|dla)\s*([.;,]|$)",
        r"\1", candidate, flags=re.IGNORECASE,
    )
    candidate = re.sub(r"^\s*[,;:.\-]\s*", "", candidate)
    candidate = re.sub(r"\s*[,;:]\s*$", "", candidate)
    return candidate.strip()


def _deterministic_repair(
    content: dict[str, Any], violations: list[Violation]
) -> tuple[list[Violation], list[str]]:
    """Apply clause/sentence drops for each violation. Returns
    (still_broken, fixes) — still_broken are fields a drop emptied or couldn't
    reach, which need the LLM rewrite tier."""
    fixes: list[str] = []
    still_broken: list[Violation] = []
    for v in violations:
        holder, key = _resolve_path(content, v.field)
        if holder is None or not isinstance(holder[key], str):
            still_broken.append(v)
            continue
        original = holder[key]
        repaired = _drop_quote(original, v.quote)
        if repaired == original:
            still_broken.append(v)
            continue
        if not repaired.strip():
            # Drop emptied the field — needs a rewrite, not a hole.
            still_broken.append(v)
            continue
        holder[key] = repaired
        fixes.append(f"[{v.severity}] dropped from {v.field}: '{v.quote[:50]}'")
    return still_broken, fixes


def _llm_rewrite(
    content: dict[str, Any], violations: list[Violation], job_text: str
) -> list[str]:
    """One targeted rewrite call for fields a deterministic drop can't fix.

    Rewrites ONLY the affected fields, instructs the model to remove/correct the
    listed claims and change nothing else, then writes back per-field (verbatim
    locking everything not named). Best-effort. Returns a fix log."""
    affected = sorted({v.field for v in violations})
    field_texts = {f: _field_text(content, f) for f in affected}
    field_texts = {f: t for f, t in field_texts.items() if t is not None}
    if not field_texts:
        return []

    viol_lines = "\n".join(
        f"- in `{v.field}`: remove/correct \"{v.quote}\" ({v.reason})"
        for v in violations if v.field in field_texts
    )
    user_message = (
        "The following resume/cover-letter fields contain unsupported claims. "
        "Remove or correct ONLY these claims; keep everything else identical; "
        "do NOT introduce any new facts, foreign-language words, or keywords. "
        "Return a JSON object mapping each field path to its corrected text, "
        "using these exact keys.\n\n"
        f"Claims to fix:\n{viol_lines}\n\n"
        f"Fields (JSON):\n{json.dumps(field_texts, ensure_ascii=False, indent=2)}"
    )
    try:
        from llm_client import call_llm
        from hunter.config import LLM_MODEL
        rewritten = call_llm(
            system_prompt=(
                "You correct individual resume fields. Output strict JSON mapping "
                "the given field paths to corrected strings. No prose."
            ),
            user_message=user_message,
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=2048,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[claim_judge] rewrite call failed (skipping): {e}")
        return []

    fixes: list[str] = []
    if not isinstance(rewritten, dict):
        return fixes
    for fld, new_text in rewritten.items():
        if fld not in field_texts or not isinstance(new_text, str) or not new_text.strip():
            continue
        holder, key = _resolve_path(content, fld)
        if holder is None:
            continue
        holder[key] = new_text.strip()
        fixes.append(f"[rewrite] {fld}")
    return fixes


def repair_content(
    content: dict[str, Any],
    report: JudgeReport,
    job_text: str,
    *,
    severities: frozenset[str] | set[str] | tuple[str, ...] = ACTIONABLE_SEVERITIES,
) -> tuple[dict[str, Any], list[str]]:
    """Repair violations of the given severities. Deterministic clause-drop first,
    single LLM rewrite for what's left. Guards the 7-role count: a repair that
    drops a role is rejected (returns the pre-repair content). Returns
    (content, fixes).

    `severities` defaults to all actionable (fabrication + exaggeration); the
    pipeline passes a narrower set (fabrication only) for the conservative
    rollout, since `exaggeration` is a judgment call with a higher false-positive
    rate (e.g. a tool genuinely in the profile mis-flagged as inflated).
    """
    actionable = [v for v in report.violations if v.severity in severities]
    if not actionable:
        return content, []

    import copy
    from hunter.apply_shared import validate_content

    before_errors = validate_content(content)
    working = copy.deepcopy(content)

    still_broken, fixes = _deterministic_repair(working, actionable)
    if still_broken and JUDGE_MAX_REPAIR_ROUNDS >= 1:
        fixes += _llm_rewrite(working, still_broken, job_text)

    # Guard: a repair must not introduce *new* structural errors (e.g. drop a
    # role). If it does, discard the whole repair — a fabrication is less bad
    # than a structurally broken CV (the scrubs already ran; the language gate
    # runs next).
    after_errors = validate_content(working)
    if len(after_errors) > len(before_errors):
        print(
            f"[claim_judge] repair rejected — introduced structural errors "
            f"({len(before_errors)} -> {len(after_errors)}); keeping pre-repair content"
        )
        return content, []

    return working, fixes


# ---------------------------------------------------------------------------
# Pipeline stage — mode-aware orchestration shared by both pipelines
# ---------------------------------------------------------------------------

# Severities auto-repaired in warn/block mode. `fabrication` only: it's the
# high-precision class (claim absent from BOTH profile and posting, quote
# verbatim-validated). `exaggeration` is a judgment call with a higher
# false-positive rate (a tool genuinely in the profile can be mis-flagged), so it
# is surfaced (notify) but not auto-dropped until the prompt is tuned (plan M4).
REPAIR_SEVERITIES = frozenset({"fabrication"})


@dataclass
class JudgeOutcome:
    content: dict[str, Any]
    report: JudgeReport
    fixes: list[str] = field(default_factory=list)
    survivors: list[Violation] = field(default_factory=list)
    blocked: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.fixes)


def run_judge_stage(
    content: dict[str, Any],
    job_text: str,
    base_cv: str = "",
    *,
    enabled: bool = True,
    mode: str = "warn",
) -> JudgeOutcome:
    """Run the judge + (mode-gated) repair as one stage. Pure orchestration —
    no Telegram, no sys.exit; the caller decides how to notify and block.

    - disabled                → no-op outcome (judge not run).
    - mode == "report"        → judge only, NO content change (dry run / artifact).
    - mode in {"warn","block"}→ repair REPAIR_SEVERITIES (fabrication); compute
                                surviving fabrications.
    - blocked                 → mode == "block" AND a fabrication survived repair.

    Best-effort: a judge/repair exception yields a passing no-op outcome.
    """
    if not enabled:
        return JudgeOutcome(content=content, report=JudgeReport())

    try:
        report = judge_content(content, job_text, base_cv)
    except Exception as e:  # noqa: BLE001 — never fatal
        print(f"[claim_judge] stage failed (skipping): {e}")
        return JudgeOutcome(content=content, report=JudgeReport())

    if mode == "report" or not report.actionable:
        return JudgeOutcome(content=content, report=report)

    try:
        content, fixes = repair_content(content, report, job_text, severities=REPAIR_SEVERITIES)
    except Exception as e:  # noqa: BLE001
        print(f"[claim_judge] repair failed (skipping): {e}")
        return JudgeOutcome(content=content, report=report)

    survivors = [
        v for v in report.fabrications
        if quote_survives(content, v.field, v.quote)
    ]
    blocked = mode == "block" and bool(survivors)
    return JudgeOutcome(
        content=content, report=report, fixes=fixes, survivors=survivors, blocked=blocked
    )
