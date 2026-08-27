"""
hunter/pipeline/validate.py — content.json schema validation for the apply
pipeline. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.
"""

from __future__ import annotations

from hunter.config import GENERATE_PL_RESUME

REQUIRED_JSON_KEYS: list[str] = [
    "company_name",
    "stack",
    "lang",
    "job_title",
    "resume_en",
    "cover_letter_en",
    "cover_letter_pl",
    "about_me_en",
    "about_me_pl",
]
if GENERATE_PL_RESUME:
    REQUIRED_JSON_KEYS.append("resume_pl")

# The three _pl fields the M4 skip-instruction (build_pl_skip_instruction)
# asks the generator to return empty for an EN posting. They're deliberately
# grouped: a repair-round-trip is only worth avoiding when the LLM omitted
# ALL three the same way an intentional skip would — one present and two
# missing is a real inconsistency, not a skip, and must still error.
_PL_SKIPPABLE_KEYS = ("resume_pl", "cover_letter_pl", "about_me_pl")


def validate_content(data: dict, *, pl_optional: bool = False) -> list[str]:
    """Return list of missing/invalid fields.

    `pl_optional=True` (only ever passed by the caller that actually issued
    the M4 skip-instruction — i.e. an EN posting, short mode, flag on) also
    tolerates the LLM omitting the three _pl keys entirely instead of
    returning them as explicit empty values ({}/"", which already pass the
    `is None` check below regardless of this flag). Default False keeps
    every other caller (CLI pipeline, verdict refine rounds, dual-apply,
    tests) exactly as strict as before — a PL posting or a full-mode run
    missing its _pl fields is still a real bug, not an intentional skip.
    """
    errors = []
    pl_all_missing = pl_optional and all(
        key not in data or not data[key] for key in _PL_SKIPPABLE_KEYS
    )
    for key in REQUIRED_JSON_KEYS:
        if key in _PL_SKIPPABLE_KEYS and pl_all_missing:
            continue
        if key not in data or data[key] is None:
            errors.append(f"Missing field: {key}")

    resume = data.get("resume_en")
    if isinstance(resume, dict):
        for sub in ("summary", "skills", "experience", "education"):
            if sub not in resume:
                errors.append(f"resume_en missing: {sub}")
        if isinstance(resume.get("experience"), list) and len(resume["experience"]) < 7:
            errors.append(
                f"resume_en.experience has only {len(resume['experience'])} jobs (expected 7 — ALL roles required)"
            )
    else:
        errors.append("resume_en is not a dict")

    return errors
