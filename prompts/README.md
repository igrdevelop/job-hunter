# prompts/ — LLM system instructions

System files that define the pipeline's behavior. These are version-controlled
and apply to every candidate equally.

| File | Purpose |
|------|---------|
| `generation_rules.md` | LLM instructions for resume / cover-letter generation (incl. RED LINES the quality pipeline enforces) |
| `judge_rules.md` | Instructions for the claim-judge verification pass |

Candidate-personal files (profile, base CVs, examples) live in
[`candidate/`](../candidate/README.md).
