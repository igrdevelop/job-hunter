"""Cheap-model stack pre-screen — one Haiku call before the expensive generation.

docs/STACK_PRESCREEN_PLAN.md M3/M4.

The deterministic gates cannot see this. `is_react_only_job_text` is
conservative by contract — any mention of "angular" makes it return False — and
over the seven August postings that reached generation on a React stack it would
have caught **zero**: four mentioned Angular in passing (4/17, 4/3, 2/11, 1/15
angular/react counts) and two mentioned React only twice, under the ≥3 threshold.
Tightening the counts is not the answer either: a posting with 4 Angular and 3
React mentions is genuinely mixed, and one with 0 and 2 is not catchable by any
threshold. The stack is only really known once a model has read the posting, and
today that does not happen until the generator has already been paid for.

So this is one `JUDGE_MODEL` call (Haiku tier, ~$0.0016 on a median 5.9 KB
posting) placed after the free deterministic gates and before the first
generation call. It changes a real decision — generate or skip — which is the
standing bar for adding an LLM step to this pipeline at all.

Deliberately narrow:
  * it judges the STACK, nothing else. Location, work authorization and language
    already have deterministic rules in the doomed gate, and a second opinion on
    them would only add disagreement.
  * `seniority` is returned for the record but no decision keys on it: the
    measurement (docs/STACK_PRESCREEN_PLAN.md M0) showed mid/regular titles are
    sent at the baseline rate, so filtering them would cost real applications.
  * `evidence` must be a verbatim substring of the posting. A verdict whose
    quote is invented is dropped whole — the same defence claim_judge uses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from hunter.config import (
    JUDGE_API_KEY,
    JUDGE_MODEL,
    JUDGE_PROVIDER,
)

SYSTEM_PROMPT = """You read one job posting and report which frontend stack it is
actually for. You are not deciding whether anyone should apply — you only
describe the posting.

Return STRICT JSON, no prose, with exactly these keys:

{
  "primary_stack": "angular" | "react" | "vue" | "svelte" | "fullstack" | "other" | "unclear",
  "secondary": ["<other frameworks the posting names, lowercase>"],
  "angular_required": true | false,
  "seniority": "junior" | "mid" | "senior" | "lead" | "unclear",
  "verdict": "fit" | "mismatch",
  "confidence": 0.0-1.0,
  "evidence": "<one short VERBATIM quote from the posting, under 200 characters>"
}

Rules:
- "primary_stack" is the framework the day-to-day work is in. A posting that
  names several frameworks has one it actually builds in; pick that one. Use
  "unclear" when the posting genuinely does not say.
- "angular_required" is true only when Angular is a requirement, not when it
  appears in a nice-to-have list, a company tech-stack tour, or a list of
  "any of these frameworks".
- "verdict" is "mismatch" when the role's day-to-day framework is NOT Angular
  and Angular is not required. Otherwise "fit".
- "confidence" is how certain you are of "primary_stack". Be honest: a posting
  that mentions two frameworks evenly deserves a low number.
- "evidence" MUST be copied character-for-character from the posting. Do not
  paraphrase, do not fix typos, do not translate. If you cannot quote it, return
  an empty string.
"""


@dataclass
class PrescreenVerdict:
    """One posting's stack assessment. `ok` is False for anything unusable."""

    primary_stack: str = "unclear"
    secondary: list[str] = field(default_factory=list)
    angular_required: bool = False
    seniority: str = "unclear"
    verdict: str = "fit"
    confidence: float = 0.0
    evidence: str = ""
    ok: bool = False

    @property
    def is_mismatch(self) -> bool:
        return self.ok and self.verdict == "mismatch"


def _normalize_for_quote(text: str) -> str:
    """Collapse whitespace so a quote survives the model re-wrapping it.

    Nothing else is normalised: the point of the verbatim check is that the
    model had to read the actual words, and loosening it past whitespace would
    let a paraphrase through.
    """
    return re.sub(r"\s+", " ", text or "").strip().lower()


def evidence_is_verbatim(evidence: str, job_text: str) -> bool:
    """True when `evidence` really appears in the posting.

    A verdict is only as trustworthy as its quote. claim_judge learned this the
    expensive way: a model asked to justify a finding will happily invent the
    sentence it is justifying.
    """
    quote = _normalize_for_quote(evidence)
    if not quote or len(quote) < 12:
        return False
    return quote in _normalize_for_quote(job_text)


def parse_verdict(raw: Any, job_text: str) -> PrescreenVerdict:
    """Turn a raw model response into a verdict, or an unusable one.

    Never raises. A malformed shape, a missing key or an invented quote all
    produce `ok=False`, which every caller treats as "no opinion, carry on".
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return PrescreenVerdict()
    if not isinstance(raw, dict):
        return PrescreenVerdict()

    stack = str(raw.get("primary_stack") or "unclear").strip().lower()
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ("fit", "mismatch"):
        return PrescreenVerdict()

    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    secondary_raw = raw.get("secondary")
    secondary = (
        [str(x).strip().lower() for x in secondary_raw if str(x).strip()]
        if isinstance(secondary_raw, list)
        else []
    )

    evidence = str(raw.get("evidence") or "").strip()
    if not evidence_is_verbatim(evidence, job_text):
        # Keep the assessment, drop its authority: an unquotable verdict must
        # never be allowed to skip a vacancy.
        return PrescreenVerdict(
            primary_stack=stack,
            secondary=secondary,
            angular_required=bool(raw.get("angular_required")),
            seniority=str(raw.get("seniority") or "unclear").strip().lower(),
            verdict=verdict,
            confidence=confidence,
            evidence=evidence,
            ok=False,
        )

    return PrescreenVerdict(
        primary_stack=stack,
        secondary=secondary,
        angular_required=bool(raw.get("angular_required")),
        seniority=str(raw.get("seniority") or "unclear").strip().lower(),
        verdict=verdict,
        confidence=confidence,
        evidence=evidence,
        ok=True,
    )


def assess_stack(job_text: str, *, title: str = "", max_chars: int = 20000) -> PrescreenVerdict:
    """One cheap-model call describing the posting's stack. Never raises.

    Returns a verdict with `ok=False` when the call failed, the response was
    malformed, or the evidence quote was not verbatim — in every one of those
    cases the caller carries on exactly as if the pre-screen did not exist.
    """
    text = (job_text or "").strip()
    if len(text) < 200:
        return PrescreenVerdict()

    user_message = (
        (f"Job title (as advertised): {title}\n\n" if title else "")
        + "--- JOB POSTING ---\n"
        + text[:max_chars]
    )

    try:
        from llm_client import call_llm

        raw = call_llm(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            provider=JUDGE_PROVIDER,
            model=JUDGE_MODEL,
            api_key=JUDGE_API_KEY,
            max_tokens=512,
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        print(f"[prescreen] call failed (skipping): {e}")
        return PrescreenVerdict()

    return parse_verdict(raw, text)
