# candidate/ — your personal data

Everything the bot needs to know about **you** lives in this folder. Edit
these files with your real experience, then run the bot — no other setup
needed for CV generation.

## Files

| File | Required? | Purpose |
|------|-----------|---------|
| `candidate.yaml` | Yes | Structured identity: name, city, languages, employers, and (`employers.history`, `experience`) the exact employer/title/period table + years-of-experience label the generation prompt enforces as RED LINES. Drives filters, QA checks and LLM prompts. |
| `candidate_profile.md` | Yes | Free-text career history: contact, stack, work experience, education. The LLM reads this + the job posting to generate your CV. |
| `generation_rules.local.md` | Optional | Free-text narrative the generation prompt can't express as YAML structure — a personal cover-letter "story bank" (which achievement to cite for which posting theme), tone notes, personal writing taboos. Appended after the tracked prompt by `hunter/gen_prompt.py`; absent by default. |
| `filters.yaml` | No | Job-intake policy overrides (title keywords, exclude patterns, hybrid rules, AI-mill blocklist additions…). Copy from `filters.example.yaml`. Missing = shared Layer-1 defaults (`hunter.filter_profile.builtin_defaults()`). Edit + next `/hunt` — no deploy. See [docs/FILTERS_YAML_PLAN.md](../docs/FILTERS_YAML_PLAN.md). |
| `filters.example.yaml` | Tracked template | Today's default knobs + merge notes. Do not edit in place for personal policy — copy to `filters.yaml`. |
| `base_cv_angular.md` | Per track | Pre-polished resume bullets for the Angular track. The LLM uses these as a starting point instead of inventing from scratch. |
| `base_cv_react.md` | Per track | Same, for React / JS roles. |
| `base_cv_ai.md` | Per track | Same, for AI-first roles. |
| `base_cv_fullstack_angular_nest.md` | Per track | Same, for Angular + NestJS full-stack roles. |
| `base_cv_fullstack_react_next.md` | Per track | Same, for React + Next.js full-stack roles. |
| `examples/` | Optional | Few-shot examples: your best cover letters and about-me texts. |
| `notes/` | Optional | Private interview notes (gitignored, not read by the pipeline). |

## How to set up

1. Copy the `.example` templates to their real filenames:
   ```bash
   cp candidate.yaml.example candidate.yaml
   cp candidate_profile.example.md candidate_profile.md
   cp base_cv_angular.example.md base_cv_angular.md
   cp filters.example.yaml filters.yaml   # optional — tune hunt policy
   ```
2. Open `candidate.yaml` and fill in your name, city, languages and employers —
   including `employers.history` (your real employers, titles and periods, in
   reverse-chronological order) and `experience.years_label`/`since_year`. The
   generation prompt renders this into its RED LINES at call time
   (`hunter/gen_prompt.py`), so this is the ONLY place that table needs to be edited.
3. Open `candidate_profile.md` and replace the example experience with yours.
4. Open `base_cv_angular.md` (or whichever track you target) and write your
   real pre-polished bullets. Dates and companies must match
   `candidate_profile.md` — the sanitizer cross-checks them.
5. (Optional) Edit `filters.yaml` — start from the example defaults and change
   only what differs for you (e.g. React keywords, turn off German exclusion).
   Omit keys you want to keep at the shared default. `exclude_companies` and
   `extra_anti_hybrid_cities` are extend-only (you can add, not remove the
   calibrated entries). Regenerating the example from code:
   `python tools/gen_filters_example.py`.
6. (Optional) Add cover letter examples to `examples/`.

The real files are **gitignored** — they stay on disk but never enter git.
Only the `.example` / `filters.example.yaml` templates are tracked.

## Notes

- `candidate_profile.md` is **required** — the apply pipeline exits without it.
- Base CVs are optional per track: if a track file is missing, generation
  still works, just without pre-polished bullets for that track.
- Keep the section structure (`## Skills`, `### Role N`, `## Education`) —
  `hunter/resume_sanitizer.py` parses it to cross-check generated CVs.
- **Docker:** `docker-compose.yml` mounts this entire folder read-only into
  the container. Edit the files on the host; the bot picks them up on the
  next run.
