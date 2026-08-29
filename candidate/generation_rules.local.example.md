<!--
Optional narrative tail for the generation prompt (docs/
GENERATION_ARCHITECTURE_ANALYSIS.md §6, wave 2). Copy this to
generation_rules.local.md (gitignored, like candidate_profile.md) and fill
in your own material; hunter/gen_prompt.py appends it verbatim after the
tracked prompts/generation_rules.md + your rendered employment facts. Leave
it absent if you have nothing to add here — the generation prompt works
fine without it.

Use this for things that are prose, not structured data: which real
achievement to cite for which posting theme (a "story bank" for the cover
letter's proof paragraphs), personal tone preferences, phrasing you never
want the model to use about you. Anything that's a flat fact (an employer
name, a period, a backend, a years-of-experience number) belongs in
candidate.yaml's employers.history / experience instead — this file is read
as free text, so it can't be validated the way structured fields are.
-->

**Story bank** (rotate; tie to posting must-haves — replace with your own):

- Team leadership / code review → describe your own team-leadership example, with the real employer and scale
- Performance / complex data grids → your own performance-optimization project
- Migration / version upgrade → a real migration you did, with real scope numbers
- Greenfield / E2E ownership → a real project you built from scratch
