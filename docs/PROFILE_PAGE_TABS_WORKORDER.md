# Profile Page Tabs — Work Order (site/api companion + one bot-side piece)

**Status:** draft — decided with the owner 2026-08-31, not started.
**Parent:** `docs/RESUME_PROFILE_STORE_PLAN.md` (M1–M4 + steps 2d/4b shipped in this
repo). This document is the companion work order the parent plan deferred ("editor
UI ... belongs to the site/api repos"), written in the `docs/MULTI_USER_UPDATE.md`
cross-repo pattern: the UI/UX decisions and the shared contract live here; the
site/api repos implement against them. The ONE bot-repo work item (the preview job
kind) is at the bottom.

## Owner decisions (2026-08-31 discussion)

1. **`/profile` becomes four tabs:** Uploads → Editor (default) → Rendered files →
   Test resume. The page's mental model matches the pipeline: upload → parse →
   canonical profile → render.
2. **Tab 4 (test resume) is built NOW and shown to the owner from day one**; a
   single owner-visibility flag hides it from regular users later.
3. **Variants ("личности") are an edit/view context only.** The page never
   switches which track the bot hunts/generates for — that already exists
   (automatic per-vacancy base-CV selection via `tracks.base_cv` +
   `gen_prompt.base_cv_files()`, and the `/tracks` Telegram command for which
   tracks are active). Exposing that on the site is explicitly deferred (it
   belongs with the search-spec work, MULTI_USER B3.5), and the UI copy must not
   imply a chip changes bot behavior.
4. **Layout variant A — Core above, variants below** (owner picked A over
   "Core as a highlighted first chip"):

   ```
   Профиль (Core)                          ← the page itself; always visible
   ───────────────────────────────────────
   Личности:  [ Angular ] [ React ] [ + ]  ← chip row BELOW Core, owner-only
   ```

   Core is the foundation, not "one more personality" — it must not sit in the
   same row as the track chips. Clicking a chip overlays the editor with that
   variant's deltas and shows a banner ("Viewing variant Angular — its
   differences from Core") with a "back to Core" action.
5. **The variant chip row is behind the SAME owner flag as tab 4.** Parent plan
   decision #3: a customer has zero or one variant and never sees track UI. A
   customer therefore sees a clean "Профиль" page with no chip row at all — the
   page looks whole, not cut down.

## Per-tab spec

### Tab 1 — Uploads (Загрузки)

- List of `uploads[]` (filename, uploaded/parsed_at, status) + "upload resume"
  (PDF/DOCX/TXT/MD — the set `hunter/profile_parse.py::extract_resume_text`
  accepts).
- Parse is async (`profile_jobs` kind=`parse`, drained every ~20 s by the bot):
  the tab polls the job status the API already exposes and shows
  pending/running/done/error per upload. `error` is terminal — the retry is a
  re-upload (parent contract).
- Re-upload merge flow (parent decision #7): parse output lands as
  `origin: "parsed"` proposals; same-company/overlapping-period roles surface as
  "looks like a duplicate — merge?"; an `edited` element is never silently
  overwritten.
- Track-agnostic: parsing always feeds Core + leftovers, never a variant.

### Tab 2 — Editor (Редактор) — default tab

- **Core view (default):** identity, questionnaire block (home city / hybrid
  tolerance / languages / work authorization — facts no CV contains), roles with
  the FULL bullet superset, skills, extras, education, `generation_notes`.
  Facts are edited here and only here — a variant never forks a fact.
- **Leftovers live in this tab** (not tab 1): they are profile content awaiting
  placement by the user; each shows a "from upload X" provenance link.
- **Variant view (chip clicked, owner-only):** overlay showing that variant's
  headline / summary / `notes` / skills, plus per-role overrides. Per the M0b
  finding, overrides are WHOLESALE rewrites, not per-item filters — so each
  role section shows either "inherited from Core" (grayed, read-only) or
  "overridden for this track" (its own full list), with
  "override" / "revert to Core" actions. No per-bullet checkboxes.
- **Add variant (`+`):** v1 restricts the key to the known track keys
  (angular / react / ai / fullstack_*) — the key is both the
  `base_cv_<track>.md` filename and what the filters understand
  (`_react_track_active` keys on `react` literally). No free-form names.
- **Delete variant:** warns that the next publish deletes its
  `base_cv_<track>.md` (the renderer already does this — `render_all()` removes
  stale files).
- Save/publish enqueues `profile_jobs` kind=`render` (existing flow).

### Tab 3 — Rendered files (Итоговые файлы)

- Read-only, strictly — parent decision #6 (one-way DB → files; hand-editing
  rendered files is rejected; renders are full overwrites).
- Shows `candidate.yaml`, `candidate_profile.md`, `base_cv_<track>.md` per
  variant, `generation_rules.local.md` when present, and `profile.json`.
- **Staleness indicator:** "profile changed since last render — publish again"
  (compare profile revision timestamp vs the last successful render job).
- No variant selector needed — files are listed per track anyway.
- **Visible to ALL users in v1, but behind its own visibility flag** (owner
  decision 2026-08-31): the tab ships enabled for everyone, and the site keeps
  the ability to hide it later (for regular users, or entirely) without
  rework — same flag mechanism as tab 4, separate flag, default ON.

### Tab 4 — Test resume (Тестовое резюме) — owner-flagged

- Purpose: "what will the system actually produce from my profile" — a generic
  (no-vacancy) CV PDF rendered with the production layout.
- Same chip row as the editor selects WHICH variant to preview (this is the
  chips' main practical value today: eyeball angular vs react output).
- Async via a NEW `profile_jobs` kind=`preview` (bot-side work item below);
  the tab enqueues, polls, then displays/downloads the PDF.
- **Previews are kept as a dated history, not overwritten** (owner decision
  2026-08-31): the tab lists past previews per track (date + download), newest
  first. No auto-prune in v1 — the files are small and live under the user's
  own directory; a retention cap is a later concern.
- **Deterministic, $0, no LLM** — owner's standing rule against speculative LLM
  layers. The generic content is assembled straight from the profile.

## Shared contract additions (duplicate into the api/site work orders)

- **New `profile_jobs.kind = 'preview'`.** Payload: JSON
  `{"profile": <full profile document>, "track": "<variant key or 'core'>"}` —
  self-contained like `render` (the bot never reads the API's app.sqlite).
  Result on `done`: JSON list of written file paths (the PDF first), under a
  dated subfolder `users/{uid}/candidate/preview/<track>/<UTC timestamp>/` —
  each preview run gets its own folder so the tab's history list is just a
  directory/DB listing. Terminal `error` + message otherwise. Same statuses,
  claim semantics, stale-reset and polling as the existing kinds.
- **Owner flag:** the API exposes an `is_owner` (or equivalent) boolean on the
  session/user; the site gates tab 4 AND the variant chip row on it. v1 may
  key it on the known owner user id (`DEFAULT_USER_ID`'s user) — mechanism is
  the api repo's choice.
- Everything else (upload endpoint, revisions, enqueue+poll, isolation tests)
  is already specified by `job-hunter-api/docs/RESUME_PROFILE_STORE.md` and the
  parent plan — unchanged.

## Bot-repo work item (the only code in this repo)

**`preview` job kind** in `hunter/schedules/profile_jobs.py` (+ a small
`hunter/profile_preview.py`):

1. Build a generic `content.json` deterministically from the Profile — no LLM:
   summary from core/variant summary, skills from variant (or core) skills in
   `document.skill_categories` shape, roles/bullets via the same
   track-resolution the renderer uses (`bullets_by_track` wholesale override,
   `Role.tracks` visibility), identity from `core.identity`.
2. Run the existing `generate_docs.py` machinery with `--no-tracker` into
   `users/{uid}/candidate/preview/<track>/<UTC timestamp>/` (never a tracker
   row, never delivery/Drive/Sheets — preview is not an application). No
   watermark on the PDF (owner decision 2026-08-31) — the production layout,
   as-is.
3. Return the written file list as the job `result`.

Guardrails: reuse `_resolve_user_relative_path`-style containment for the
output dir; wrap per-job failures in `fail_profile_job` exactly like the other
kinds; `best_effort("profile.jobs")` already covers the tick. Tests mirror
`tests/test_schedules_profile_jobs.py` (fake profile → PDF exists, no tracker
row written, unknown track → terminal error).

## Non-goals

- No generation-side track switching on the site (`/tracks` in Telegram covers
  it; site exposure waits for the search-spec work).
- No free-form variant names / variants not tied to track keys (breaks the
  `base_cv_<track>` + filters key contract).
- No per-vacancy personality picker (the pipeline already selects
  automatically).
- No template/layout variety for the preview (parent non-goal: single output
  format).
- No editing on the rendered-files tab, ever.

## Resolved questions (owner, 2026-08-31)

1. **Preview retention:** keep a dated history list per track, not an
   overwrite. Each run writes into its own timestamped subfolder; tab 4 lists
   them newest-first. No auto-prune in v1.
2. **Watermark on the preview PDF:** no — production layout as-is.
3. **Tab 3 (rendered files):** visible to all users in v1, behind its own
   visibility flag (default ON) so it can be hidden later without rework.
