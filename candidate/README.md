# candidate/ — your personal data

Everything the bot needs to know about **you** lives in this folder. Edit
these files with your real experience, then run the bot — no other setup
needed for CV generation.

## Files

| File | Required? | Purpose |
|------|-----------|---------|
| `candidate.yaml` | Yes | Structured identity: name, city, languages, employers. Drives filters, QA checks and LLM prompts. |
| `candidate_profile.md` | Yes | Free-text career history: contact, stack, work experience, education. The LLM reads this + the job posting to generate your CV. |
| `base_cv_angular.md` | Per track | Pre-polished resume bullets for the Angular track. The LLM uses these as a starting point instead of inventing from scratch. |
| `base_cv_react.md` | Per track | Same, for React / JS roles. |
| `base_cv_ai.md` | Per track | Same, for AI-first roles. |
| `base_cv_fullstack_angular_nest.md` | Per track | Same, for Angular + NestJS full-stack roles. |
| `base_cv_fullstack_react_next.md` | Per track | Same, for React + Next.js full-stack roles. |
| `examples/` | Optional | Few-shot examples: your best cover letters and about-me texts. |
| `notes/` | Optional | Private interview notes (gitignored, not read by the pipeline). |

## How to set up

1. Open `candidate.yaml` and fill in your name, city, languages and employers.
2. Open `candidate_profile.md` and replace the example experience with yours.
3. Open `base_cv_angular.md` (or whichever track you target) and write your
   real pre-polished bullets. Dates and companies must match
   `candidate_profile.md` — the sanitizer cross-checks them.
4. (Optional) Add cover letter examples to `examples/`.

## Notes

- `candidate_profile.md` is **required** — the apply pipeline exits without it.
- Base CVs are optional per track: if a track file is missing, generation
  still works, just without pre-polished bullets for that track.
- Keep the section structure (`## Skills`, `### Role N`, `## Education`) —
  `hunter/resume_sanitizer.py` parses it to cross-check generated CVs.
- **Docker:** `docker-compose.yml` mounts this entire folder read-only into
  the container. Edit the files on the host; the bot picks them up on the
  next run.
