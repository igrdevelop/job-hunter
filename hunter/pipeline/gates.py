"""
hunter/pipeline/gates.py — pre-LLM stack screening, the doomed-vacancy gate,
the manual stack-gate override, and the stack pre-screen. Moved out of
hunter/apply_shared.py (docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) —
see hunter.apply_shared for the backward-compat re-export.

``run_doomed_gate()`` / ``stack_gate_allows_manual()`` / ``run_prescreen()``
deliberately re-read ``notify`` from ``hunter.apply_shared`` (not a plain
module-global call) at call time: that module remains the attribute several
tests monkeypatch directly (test_doomed_gate_wiring.py, test_manual_stack_gate.py,
test_prescreen.py, the golden E2E tests), and a bare in-module call would
silently stop observing that patch once these functions moved out of
apply_shared.py.
"""

from __future__ import annotations

import re

from hunter import gen_profile
from hunter.best_effort import best_effort
from hunter.pipeline.errors import PASTE_NO_URL_PLACEHOLDER

# Shown after React-only auto-skip.
_REACT_SKIP_FORCE_HINT = (
    "\n\n📌 <b>Need docs anyway?</b> In Telegram:\n"
    "• <code>/force</code> and the same URL (🔗 above), or\n"
    "• <code>/force</code> followed by the full job posting text (same as paste flow).\n"
    "This enables <code>--force</code> (bypasses React-only filter); for JobLeads "
    "<code>job_posting.txt</code> will be used if already filled in."
)


# ── Pre-LLM text-based stack screening ───────────────────────────────────────

# Default minimum number of React mentions (no Angular present) to auto-skip
# pre-LLM. Configurable via generation.yaml (gates.react_skip_min_mentions),
# read at call time inside is_react_only_job_text() — this constant is only
# the fallback default (see hunter.filters._resolve_flt's docstring for why
# a module-level snapshot would break monkeypatching / profile reload).
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
    min_mentions = gen_profile.get("gates.react_skip_min_mentions", _REACT_SKIP_MIN_MENTIONS)
    return len(re.findall(r"\breact\b", t)) >= min_mentions


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
    from hunter.apply_shared import notify
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
    from hunter.apply_shared import notify

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
    from hunter.apply_shared import notify
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
