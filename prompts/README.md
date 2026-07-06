# prompts/ — LLM instructions and candidate data

Two kinds of files live here. **System files** are part of the pipeline and are
version-controlled. **Personal files** describe a specific candidate — they are
gitignored and every user creates their own from the `.example` templates.

## System files (in git — do not put personal data here)

| File | Purpose |
|---|---|
| `generation_rules.md` | LLM instructions for resume / cover-letter generation (incl. RED LINES the quality pipeline enforces) |
| `judge_rules.md` | Instructions for the claim-judge verification pass |

## Personal files (gitignored — create your own)

| File | Purpose | Template |
|---|---|---|
| `candidate_profile.md` | **Single source of truth** for who you are: contact, stack, work history, education. Read by generation, the claim judge, the sanitizer and the refine loop. | `candidate_profile.example.md` |
| `base_cv_angular.md` | Pre-polished bullets for the Angular track | `base_cv_angular.example.md` |
| `base_cv_react.md` | Same, React / JS track | same structure |
| `base_cv_ai.md` | Same, AI-first track | same structure |
| `base_cv_fullstack_angular_nest.md` | Same, Angular + NestJS track | same structure |
| `base_cv_fullstack_react_next.md` | Same, React + Next.js track | same structure |
| `examples/` | Few-shot examples: your best cover letters (`cl_examples_en.md`, `cl_examples_pl.md`) and about-me texts (`about_me_en.md`, `about_me_pl.md`) | optional |
| `candidate/` | Free-form private notes (not read by the pipeline) | optional |

## Setup

```bash
cp prompts/candidate_profile.example.md prompts/candidate_profile.md
cp prompts/base_cv_angular.example.md prompts/base_cv_angular.md
# fill both in with your real data; add other track CVs as needed
```

Notes:

- `candidate_profile.md` is **required** — the apply pipeline exits without it.
- Base CVs are optional per track: if a track file is missing, generation still
  works, just without pre-polished bullets for that track.
- `examples/` is optional: missing files simply mean no few-shot examples.
- Keep the section structure of the templates (`### Work Experience`, role
  lines, `**Education**:`) — `hunter/resume_sanitizer.py` parses it to
  cross-check generated CVs against your real history.
- **Docker:** these files are not in the image (they are gitignored). The
  provided `docker-compose.yml` mounts them from the host — copy your personal
  files to the deploy host once.
