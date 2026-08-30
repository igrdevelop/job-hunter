# RESUME_PROFILE_STORE Plan

**Status:** in progress — M1-M4 shipped in this repo (#238, #239, #240, #241:
schema, renderer, parser, CLI seam). Remaining: M5 (owner migration, manual on
the VPS) and the companion api/site work orders (upload endpoint, editor UI,
revisions) — both explicitly out of this repo's scope, see "Non-goals" below.
**Date:** 2026-08-29 (revised 2026-08-30 after wave 2, PR #235, merged to master)
**Motivation:** Owner decision (2026-08-29 discussion): users who upload their original
resume(s) on the site (https://job-hunter.igrflex.work/) should see everything the
parser extracted as editable fields — and be able to ADD skills, roles, and anything
else — instead of the current "copy three `.example` files and hand-edit markdown +
YAML" onboarding, which no customer will ever do. This is the concrete design for
Stage 4 of `docs/SAAS_PIVOT_PLAN_supersedes_PYTHON_CORE_PLAN.md` ("upload → parse →
our standard structure, with a confirmation screen"), which that plan calls the stage
nothing can be sold without. The owner migrates onto this editor too (dogfooding),
which makes his own five-track profile the hardest test case the schema will ever face.

## Problem

Today a candidate's identity and career data live in three hand-maintained files per
user (`users/{uid}/candidate/`): `candidate.yaml` (structured facts, read via
`hunter/candidate.py::get(dotpath)`), `candidate_profile.md` (free-text career
narrative fed to the generation LLM), and `base_cv_<track>.md` (pre-polished bullets
per stack). Evidence that this cannot onboard anyone but the owner:

- `docs/SETUP_NEW_USER.md` requires hand-writing all three files ("This step is not
  optional") — a 40-field YAML plus long-form markdown. The SAAS plan's own design
  decision table says why that fails: "A 40-field form kills conversion, nobody
  writes markdown, and the parser *will* be wrong on unusual layouts — so the 'check
  what we understood' step is part of the product."
- The three files silently duplicate facts (employer names appear in
  `employers.protected`, `employers.real_companies`, `candidate_profile.md` prose,
  AND every `base_cv_*.md`). The repo's history shows what duplicated stores do:
  three handoff-readiness audits in three weeks each found fresh drift before
  `tests/test_handoff_readiness.py` became a gate.
- There is no site surface at all for this data — the web app can register a user
  and store files, but the files must arrive by hand.

## Design summary (decided in the 2026-08-29 discussion)

1. **A structured profile is the canonical store; the three files become a
   deterministic render of it.** The apply pipeline, `/tracks`, `candidate.py`,
   filters, judge, and content-QA are NOT touched — they keep reading the same three
   files at the same paths. Strangler order, same as the rest of the SAAS plan.
2. *(Revision 2026-08-30)* **Wave 2 (PR #235) already met this plan halfway.**
   `employers.history[]` in candidate.yaml is now the structured single source of
   truth for employment facts (per-role `company/title/period` reproduced VERBATIM
   in every CV, plus `backend`, `bullets_max`, `legacy_stack_ok`, `title_by_track`),
   rendered into the prompts at call time by the new `hunter/gen_prompt.py`. This
   plan's `core.roles` is therefore a **superset of a history entry** (adds
   `description` + `bullets`), the renderer MUST emit `employers.history` from
   `core.roles`, and per-track title variation reuses wave 2's `title_by_track`
   key instead of inventing a parallel mechanism. Wave 2 also added
   `experience.{years_label,since_year}` (hard fields — the prompt forbids rounding
   them down) and an optional per-user free-text prompt tail
   `candidate/generation_rules.local.md` ("story bank"), which becomes one more
   optional render target.
3. **Core + track variants, not N independent profiles.** Facts (identity, contacts,
   location, languages, employers, education, roles with dates) exist exactly once
   in `core`. A track "personality" (angular / react / …) is a *delta of
   presentation*: per-track headline/summary/skill-ordering, and `tracks` tags on
   individual bullets and skills. This mirrors what the three files already encode
   (shared `candidate.yaml` + `candidate_profile.md`, per-track `base_cv_*.md`) and
   prevents fact drift between "personalities" — which the judge/content-QA would
   otherwise misread as fabrication. It also matches the SAAS plan's pricing shape
   ("one base profile per customer; multiple profiles are a paid upsell"): a customer
   has zero or one variant and never sees the track UI; the owner has several.
4. **The profile is a superset, not a resume.** Users ADD material beyond any single
   uploaded CV; the pipeline *cuts* tailored documents from it — the same philosophy
   as today's deliberately over-full `candidate_profile.md`. The UI must say so.
5. **Hybrid rigidity.** Hard fields = exactly the set of `candidate.get()` dotpaths
   production code reads (enumerable — M0a). Everything narrative stays free text
   INSIDE the structure (per-role description paragraph, bullets as text, overall
   summary). Over-normalizing the prose is the main quality risk: generation feeds
   on narrative nuance.
6. **One-way sync, DB → files.** The API stores the canonical profile document
   (JSON, `schema_version`, last-N revisions for undo); "save/publish" renders the
   three files into `users/{uid}/candidate/`. Hand-editing the rendered files is no
   longer supported once a user (including the owner) is migrated — two-way sync is
   explicitly rejected.
7. **Merge, not replace, on re-upload.** A second uploaded resume adds to the
   profile; same-company/overlapping-period roles surface as "looks like a
   duplicate — merge?". Every element carries a provenance flag
   (`parsed` / `edited`); a later parse NEVER silently overwrites an `edited`
   element.
8. **A "couldn't place this" leftovers bucket.** Raw fragments the parser could not
   assign are shown, not dropped — the user reassigns or deletes them.
9. **A questionnaire block for facts no CV contains:** home city + hybrid
   tolerances, `languages.disqualify_required`, work authorization, desired tracks.
   Shown next to (not mixed into) the parsed data, or the doomed gate and filters
   run on empty defaults.
10. **No proficiency levels on skills in v1.** The 2026-08-29 prompt fix removed
   proficiency qualifiers precisely because the generator's "React (familiar)" was
   flagged by the judge; levels in the profile would push them straight back in.
   Flat skill list + optional category (aligns with `document.skill_categories`).
11. **Single output document format for now** — the existing `generate_docs.py`
    layout. Template variety is a later, separate concern (the SAAS plan already
    rejected preserving customers' original CV designs).

## Non-goals

- **No pipeline refactor.** `apply_api` / `apply_cli` / `candidate.py` /
  `generate_docs.py` keep consuming the three files unchanged. Making the pipeline
  read the structure directly is a possible later wave, only after the render path
  has proven itself.
- **No new output document formats/templates.**
- **No skill proficiency levels, no per-skill years.**
- **`filters.yaml` and `generation.yaml` are out of scope** — they have their own
  loaders and plans (FILTERS_YAML_PLAN, GENERATION_ARCHITECTURE_ANALYSIS §6). This
  plan covers candidate identity/career data only.
- **No editor UI work in this repo.** The site editor (fields, chips, leftovers
  bucket, questionnaire), API storage/revisions/auth, and upload flow belong to the
  `site`/`api` repos via companion work orders (the `docs/MULTI_USER_UPDATE.md`
  pattern: a shared contract section duplicated across repos). This repo owns the
  schema, the parser, and the renderer — the pieces that must agree with the
  pipeline.
- **No hunt/search-spec changes** (that is MULTI_USER B3.5).

## Shared contract (to be duplicated into the api/site work orders)

- **Canonical store:** one JSON document per user, owned by the API
  (app.sqlite row; PostgreSQL later per SAAS Stage 8), with `schema_version` and
  last-N revisions. Deletion of the row + `users/{uid}/candidate/` +
  `users/{uid}/uploads/` is the erasure operation.
- **Document shape (v1 sketch — M1 finalizes as JSON Schema):**

  ```jsonc
  {
    "schema_version": 1,
    "core": {
      "identity":  { "full_name", "aka", "headline", "contact", "cv_filename_prefix" },
      "location":  { "home_city", "home_city_aliases", "acceptable_hybrid",
                     "weekly_hybrid", "work_authorization" },
      "languages": { "spoken", "cv_languages", "disqualify_required" },
      "employers": { "protected", "flexible": { "name", "period", "projects" } },
      "education": { "entries": [...], "school_keyword", "expected_role_count" },
      "experience": { "years_label", "since_year" },          // wave 2 hard fields
      "summary":   "free text",
      "roles": [ { "id", "company", "title", "period", "subtitle", "description",
                   "backend", "bullets_max", "legacy_stack_ok",   // wave 2 history fields
                   "title_by_track":     { "ai": "AI Tooling Engineer" },
                   "subtitle_by_track":  { },
                   "stack_line", "stack_line_by_track": { },
                   "bullets": [ { "text", "origin": "parsed|edited" } ],   // core narrative, superset
                   "bullets_by_track": { "react": ["polished bullet", "..."] },
                   "origin": "parsed|edited" } ],
      "skills": [ { "category", "items": ["Angular (2-22)", "..."], "origin" } ],
      "extras":  [ { "kind": "certification|link|award|other", "text", "origin" } ],
      "generation_notes": "free text, optional — the story-bank prompt tail"
    },
    "variants": { "angular": { "headline", "summary",
                               "skills": [ { "category", "items": [] } ] } },
    "leftovers": [ { "text", "source_upload_id" } ],
    "uploads":   [ { "id", "filename", "sha256", "parsed_at" } ]
  }
  ```

  *M0b finding (2026-08-30), first schema revision:* the owner's per-track base CVs
  are REWRITES, not subsets — the same achievement is rephrased per track
  ("Angular templates" → "TypeScript/JavaScript templates"), bullet counts per role
  differ across tracks, roles carry `subtitle` and `stack_line` lines the first
  sketch lacked, and per-track skills are full lists with their own labels
  ("Angular (background)"), not a reordering. Per-element `tracks` tags could not
  express any of that, so the model is now **role-level `*_by_track` overrides**
  (the idiom wave 2 already established with `title_by_track`): a role's
  `bullets_by_track[track]` replaces the core bullet list wholesale for that
  track's base CV; absent = the core list is used. Core `bullets` remain the
  narrative superset that renders into `candidate_profile.md`. `employers.real_companies`
  and `employers.profile_titles` are NOT stored — they are derived at render time
  from `protected` + `flexible.name` and from role titles, killing today's
  keep-in-sync-by-hand duplication. `employers.history` is not stored either — a
  history entry is a projection of a `roles[]` element (its wave-2 fields minus
  description/bullets), emitted by the renderer. `employers.protected` defaults to
  all role companies except `flexible.name` (override only if a role must be
  refine-flexible).
- **Render targets (this repo owns the renderer):** `candidate.yaml` (core hard
  fields + `employers.history` projected from roles + `tracks.base_cv` map),
  `candidate_profile.md` (summary + roles rendered as narrative, ALL bullets — the
  superset), `base_cv_<track>.md` per variant key (track-filtered bullets + variant
  headline/summary/skill order), and — only when `generation_notes` is non-empty —
  `generation_rules.local.md` (the wave-2 optional prompt tail, verbatim).
- **Direction:** DB → files only, on save/publish. The bot never writes the profile.
- **Merge rules:** parse output lands as `origin: "parsed"` proposals; `edited`
  elements are never auto-overwritten; role dedup key = normalized company +
  overlapping period, surfaced to the user, never auto-merged.

## M0 — Measure

Three free/near-free checks, decision rules stated before running.

**M0a — enumerate the hard-field contract ($0, read-only).** The mandatory schema
fields are exactly the dotpaths production code reads. Reuse the extraction regex
from `tests/test_handoff_readiness.py::test_candidate_example_covers_every_dotpath_used_in_code`:

```bash
python -c "
import re, pathlib
paths = [*pathlib.Path('hunter').rglob('*.py'), pathlib.Path('apply_agent.py'),
         pathlib.Path('generate_docs.py'), pathlib.Path('llm_client.py')]
dps = set()
for p in paths:
    dps.update(re.findall(r'candidate\.get\(\s*\"([a-z_][a-z_.]*)\"',
                          p.read_text(encoding='utf-8', errors='replace')))
print('\n'.join(sorted(dps)))"
```

Decision rule: none (this is input, not a gate) — but the M1 schema MUST cover every
dotpath returned, or it cannot render a valid `candidate.yaml`. Re-run 2026-08-30 on
master @ c233b87 (wave 2 merged): **24 dotpaths** —
`identity.{full_name,aka,headline,contact,cv_filename_prefix}`,
`location.{home_city,home_city_aliases}`, `languages.{cv_languages,disqualify_required}`,
`employers.{protected,profile_titles,real_companies,history,flexible.{name,period,projects}}`,
`education.{school_keyword,expected_role_count}`, `experience.{years_label,since_year}`,
`tracks.base_cv`, `source_urls.{pracuj,theprotocol,jobleads}_location` (wave 2 added
`employers.history` + the two `experience.*` keys). Side finding: four keys present
in `candidate.yaml.example` (`location.acceptable_hybrid`, `location.weekly_hybrid`,
`location.work_authorization`, `languages.spoken`) are NOT read via `candidate.get()`
in production code — M1 should confirm whether they are consumed through another
accessor or are documented-but-dead, and the schema keeps them either way (they feed
the questionnaire block).

**M0b — owner round-trip ($0, offline).** Hand-build the owner's real profile
document once (his files are the richest instance this schema will ever hold), write
a throwaway ~100-line render script, and diff the rendered three files against the
real, hand-polished ones (`candidate.yaml`, `candidate_profile.md`,
`base_cv_angular.md`, `base_cv_react.md`). Wave 2 shrinks this job: the owner's
`employers.history` + `experience.*` are already structured in his live
candidate.yaml — copy them in, hand-build only roles' descriptions/bullets and the
variants.
Decision rule: **if the rendered `base_cv_angular.md` loses or distorts any bullet's
substance (not formatting) that the schema cannot express, the schema is revised and
M0b re-run BEFORE any site/API work starts.** Formatting-only diffs (heading style,
ordering the owner accepts) pass. This is the gate on "over-normalization kills the
prose".

*Result (run 2026-08-30):* **PASSED after one schema revision.** The first sketch
(per-element `tracks` tags) could not express the owner's files — that revision is
recorded in the Shared contract section above. Under the revised role-level
`*_by_track` model, all FIVE base CVs (angular, react, ai, fullstack×2) round-trip
with **zero changed substance lines** (93–96 lines each), and `candidate.yaml`
reconstructs by construction (history is a projection of roles). Owner confirmed
the revision same day.

**M0c — parser accuracy probe (a few LLM calls, cents; the one deliberately non-free
check).** Run a draft parse prompt (resume text → schema JSON + leftovers) over the
owner's real original resume (docx/pdf → text via the existing `python-docx` /
`ats_pdf_roundtrip` extraction) plus 3–4 varied public sample resumes. Score hard
fields (identity, role company/title/period, education) against ground truth read by
a human.
Decision rule: **≥ 80% of hard fields correct on conventional layouts → build the
parser + confirmation screen as designed. Below 80% → the site ships a wizard form
first and the parser becomes a later enhancement** — the confirmation screen cannot
carry a parser that is mostly wrong.

*Result (run 2026-08-30, `claude-haiku-4-5-20251001`, 6 documents):* **PASSED —
build.** Conventional layouts: 4 freshly generated CV PDFs scored **132/132 hard
fields (100%)** against their own content.json (identity, role count, per-role
company/title/period/bullet-count, education; periods compared semantically —
the PDF prints "04/2026 – 05/2026" where content.json says "Apr 2026"), and the
owner's clean 2026 original .docx parsed at ~100% (identity 7/7, 6/6 roles with
verbatim NDA-masked companies, RODO clause correctly binned into leftovers). The
deliberately hard case — a 2025 CorelDRAW-exported two-column PDF whose extracted
text interleaves the courses column into the experience section — still scored
~83%: all periods/titles correct, 4/6 companies, and every failure surfaced as an
explicit leftover ("company unclear", "no dates provided") instead of a
fabrication, which is exactly the behavior the confirmation screen is designed to
absorb. All six parses returned valid JSON on the first attempt. Note: the probe
ran through the `claude -p` CLI fallback pinned to the same model (API balance was
drained at run time — the same outage path prod was on that day), so the marginal
cost was $0.

## M1..Mn — Milestones (this repo; one commit each)

> Executor breakdown: `docs/RESUME_PROFILE_STORE_PROMPT.md` splits these
> milestones into 9 one-commit steps sized for a Sonnet-tier agent, with per-step
> file lists, tests and guardrails. That file is the work order; this one is the
> argument.

**M1 — Schema module.** `hunter/profile_schema.py`: dataclass/pydantic models +
JSON-Schema export, `schema_version: 1`, validation helpers, and
`candidate/profile.example.json` (neutral placeholder data, tracked). Test: every
M0a dotpath is derivable from a valid document; example validates.
Rollback: dead code, nothing consumes it yet.

**M2 — Renderer.** `hunter/profile_render.py` + `tools/render_profile.py` CLI:
profile JSON → the three files (+ optional `generation_rules.local.md`),
deterministic, byte-stable for identical input. Derives
`real_companies`/`profile_titles` and projects `employers.history` from `roles[]`.
Golden test: render the neutral example, snapshot-compare; a round-trip test
asserting the rendered `candidate.yaml` satisfies `candidate.require_identity()`
and resolves every M0a dotpath; AND a wave-2 compatibility test — feed the rendered
`candidate.yaml` to `hunter/gen_prompt.py`'s employment-facts renderer and assert
it produces the real facts table, not its no-history degrade paragraph. Verify with
the `mutation-verify` skill on the derivation logic.
Rollback: CLI-only, nothing calls it in prod.

**M3 — Parser.** `hunter/profile_parse.py` + `tools/parse_resume.py`: docx/pdf →
text (existing extraction code) → ONE `JUDGE_MODEL`-tier call → schema-validated
draft document + `leftovers[]`; deterministic pre-fills where possible
(`contact_extract.py` for email/phone). Every element `origin: "parsed"`. Any
LLM/validation failure returns the raw text as one big leftover — parse never hard
fails. Test: fixture resumes under `tests/fixtures/resumes/` (fake identities) with
a `fake_llm`-style routed response.
Rollback: CLI-only until M4.

**M4 — Service seam.** Expose parse + render to the API: two commands on the Python
side (subprocess CLI first; folds into the SAAS Stage 1 HTTP service when that
lands — do not build a parallel transport here). Contract: input path / profile
JSON in, JSON out on stdout, non-zero exit + stderr on failure. The api repo's
companion work order wires upload → parse → editor → save → render into
`users/{uid}/candidate/`.
Rollback: API feature-flags the editor; hand-placed files keep working throughout —
render output is indistinguishable from hand-written files to the pipeline.

**M5 — Owner migration.** Commit nothing personal: on the VPS, build the owner's
profile document (from M0b), render, diff against the live files one final time,
swap in the rendered files (originals kept as dated backups next to them), and from
then on the owner edits via the site only.
Verification: one real apply run end-to-end on a rendered profile with an
unchanged `ats_verdict` ballpark and a clean judge report.
Rollback: restore the backed-up hand-written files — one `cp`.

Companion work orders (api/site repos, written when M1 lands, sharing the contract
section above): upload endpoint + storage/revisions, editor UI (fields, track chips,
duplicate-role merge prompts, leftovers bucket, questionnaire block), enqueue+poll
for parse jobs, isolation tests per SAAS plan risk #1.

## Risks

- **Prose degradation** (the render flattens narrative → generation quality drops).
  Caught by M0b's diff gate before any build, and by M5's real-apply verification
  (`ats_verdict` + judge report) before the owner's files are swapped.
- **Parser wrongness.** Mitigated by the mandatory confirmation screen (SAAS plan
  risk #4), the leftovers bucket (errors become reassignment, not data loss), and
  M0c's build/no-build gate.
- **Fact drift between store and files** (someone hand-edits a rendered file).
  Mitigated by the one-way rule + owner migration; renders are full overwrites, so
  drift cannot survive the next save. If a transition period needs it, the renderer
  can stamp a "GENERATED — edit on the site" header comment into `candidate.yaml`.
- **Judge/content-QA desync of employer lists.** Eliminated structurally:
  `real_companies`/`profile_titles` become render-time derivations of the single
  employer list instead of a third hand-synced copy.
- **Breaking the wave-2 prompt contract.** `employers.history` entries are
  reproduced VERBATIM in every generated CV and feed the RED LINES table
  (`hunter/gen_prompt.py`); a renderer that projects them with a wrong key name or
  shape silently degrades the generation prompt to its generic no-history
  paragraph. Caught by the M2 compatibility test (render → gen_prompt → assert
  real facts table) plus `tests/test_gen_prompt.py`'s own golden fixtures.
- **Tenant isolation.** No new surface in this repo (parser/renderer are per-path
  CLIs); the API-side upload/storage inherits the MULTI_USER isolation tests — the
  companion work order restates SAAS risk #1.
- **Schema evolution.** `schema_version` from day one; the API migrates documents
  forward on read, never in place without a revision snapshot.
- **Parse failure mid-onboarding.** Parse never hard-fails (M3 leftover fallback);
  the site path is enqueue+poll, so a stuck job is visible, not silent.

## Cost

- **Parse: one `JUDGE_MODEL` (Haiku-tier) call per uploaded resume** — an onboarding
  event, not a per-vacancy cost; ~$0.01–0.05 per upload. It changes a real decision
  (the contents of the user's profile, corrected on the confirmation screen).
- **Render: $0, deterministic.**
- **Per-vacancy pipeline delta: $0** — the pipeline is unchanged.
- M0c spends a few cents once, as stated.

## Open questions

1. Store the profile as ONE document with `variants` inside (recommended) — or as
   `core.json` + `variant_<track>.json` files sharing the core? (Storage detail;
   the schema is identical either way.)
2. How many revisions does the API keep per profile — is last 20 enough?
3. Is `JUDGE_MODEL` (Haiku) acceptable for the parser if M0c passes with it, or
   should M0c also score a Sonnet-tier parse to compare? (Haiku passing alone =
   ship Haiku.)
4. During the transition, may the renderer stamp a "GENERATED — do not hand-edit"
   header into `candidate.yaml`, or must rendered files stay byte-indistinguishable
   from hand-written ones?
