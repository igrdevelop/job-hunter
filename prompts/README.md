# prompts/ — LLM system instructions

System files that define the pipeline's behavior. These are version-controlled
and apply to every candidate equally.

| File | Purpose |
|------|---------|
| `generation_rules.md` | LLM instructions for resume / cover-letter generation (incl. RED LINES the quality pipeline enforces) |
| `judge_rules.md` | Instructions for the claim-judge verification pass |
| `resume_parse.md` | Instructions for the resume-profile-store parser (`hunter/profile_parse.py`, docs/RESUME_PROFILE_STORE_PLAN.md M3) — turns uploaded resume text into a structured profile document, not a CV |

Candidate-personal files (profile, base CVs, examples) live in
[`candidate/`](../candidate/README.md).

`resume_parse.md` is candidate-AGNOSTIC in a different sense than the two
generation prompts above: it never gets a candidate's own facts spliced in
(there's no `<!-- MARKER -->` to fill), because its job is to read whoever's
resume gets uploaded, not to generate for one known candidate.
