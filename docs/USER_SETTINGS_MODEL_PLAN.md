# User settings model and search profile — plan

**Status:** draft. M0 not run. No code yet.
**Date:** 2026-09-27
**Owner decisions:** grill session 2026-09-27, Q1–Q21. The decision log is below.
**Supersedes:**
- the open M4–M5 of [FILTERS_YAML_PLAN](FILTERS_YAML_PLAN.md);
- that plan's open questions #3 (`/tracks`), #5 (German derived from languages) and #6 (Poland-only low-frequency hybrid).

**Depends on:** [EVAL_CORPUS_PLAN](EVAL_CORPUS_PLAN.md), which provides the frozen corpus that M0.b replays.

**Feeds:** [MULTI_USER_UPDATE](MULTI_USER_UPDATE.md) B3.5. This plan defines WHAT a user searches for. B3.5 compiles those specs into one fetch plan and fans the results out.

## Why

The owner asked (2026-09-27): settings and filtering "look chaotic and are too tied to me". The goal is to make them logical for other people and for other IT specialists, not only Angular frontend developers.

A code survey the same day found three problems.

**1. Settings live in eight stores.**
- `.env`
- `candidate.yaml`
- `filters.yaml`
- `generation.yaml`
- the `.md` profile files
- `app.sqlite` profiles
- `tracker.db user_settings`
- `tracker.db config`

Most real user preferences are global env vars: `AUTO_APPLY`, `MAX_JOBS_PER_RUN`, the 25 source toggles, `CV_GDPR_CLAUSE`, `OUTREACH_ENABLED`, `GENERATE_PL_RESUME`.

The site's Settings page writes `user_settings` keys the bot never reads:
- The API whitelist in `api/src/settings/user-settings.service.ts` includes `AUTO_APPLY`, `CANDIDATE_TRACKS` and `SOURCE_*`. The bot reads only `tracks_enabled`, `dual_*` and `hunting_enabled`.
- The site expects `{settings: [...]}`, but the API returns a flat Record. The page most likely shows "Could not load settings" (found by reading the code, not by running the page).

**2. Filtering is only half configurable.** Every `builtin_defaults()` knob can be overridden from `filters.yaml`, but the core assumptions are in code that no profile key reaches:
- Search queries are hardcoded as `angular` / `frontend` in almost every source (`linkedin.py`, `pracuj.py`, `bulldogjob.py`, `builtin.py`, `himalayas.py`, …).
- `filters._is_unwanted_fullstack` rejects every fullstack title without "angular".
- `filters._CANDIDATE_FRAMEWORK_RE = angular|react` guards the doomed-gate stack rules.
- `pipeline/gates.is_backend_only_job_text` assumes a frontend candidate unconditionally.
- The `prescreen.py` prompt asks whether the framework "is NOT Angular".
- `filters._HOME_CITY` defaults to Wrocław and is read at import.
- `_PL_ANTI_HYBRID_CITIES` is the "too far from Wrocław" list, with no home-city carve-out.
- `_is_russia_market` has no switch.
- `tracker_cache._ANGULAR_KEYWORDS` is hardcoded.

**3. "Track" means three different things:**
- a React on/off flag (`config.active_tracks`, `/tracks`);
- a stack key that picks the base CV (`gen_prompt._DEFAULT_BASE_CV_FILES`, `apply_api._detect_stack_hint`);
- a profile `variants` key.

A new user who fills in nothing inherits the owner's profile, because the builtin defaults ARE the owner.

Checked before writing:
- `docs/AGENT_LOG.md` has no rejected version of a search profile or role packs.
- `FILTERS_YAML_PLAN.md` recommended NOT folding `/tracks` into the file "until the web UI writes filters". The web UI now writes filters (`PUT /filters` exists), so that condition has lapsed.
- `FILTER_FEEDBACK_PLAN.md` is complementary. Its proposed rules would land in `search.advanced` (below) instead of the builtin.

## Target model: four tiers

| Tier | Contents | Edited by | Stored in |
|---|---|---|---|
| **Account** | identity, contacts, Telegram link, notifications | user | Profile JSON in `app.sqlite` (exists) |
| **Search profile** | role, stacks, seniority, cities, work modes, languages, sources, opinion rules | user (form + Advanced tab) | Profile JSON, `search` section, rendered to `users/{uid}/candidate/filters.yaml` |
| **Automation** | auto_apply, max_jobs_per_run, send_docs, outreach, gdpr_clause, pl_resume, hunting_enabled, disabled sources | user | `tracker.db user_settings`, through one typed registry |
| **Operator** | LLM keys/models, schedule, timeouts, queue, backups, paths, `generation.yaml` | admin | `.env` (unchanged) |

### Search profile (decisions Q1–Q5, Q9–Q11, Q21)

Where it lives:
- **One source of truth.** The `search` section lives in the Profile JSON, with revisions and restore for free.
- `profile_render.render_all()` expands it into `filters.yaml`. The bot's read path does not change: `filter_profile.load_profile()` plus the mtime cache.
- **Advanced knobs live there too** (`search.advanced`: raw regex, `exclude_patterns`, gate modes) and are rendered into the same file.
- `PUT /filters` becomes an edit of the profile. Nothing writes `filters.yaml` directly any more, so a render can never erase a hand edit.

Proposed shape:

```yaml
search:
  role: frontend                 # exactly one main role (Q3)
  adjacent_roles: [fullstack]    # only from that role pack's fixed adjacency list
  stacks:                        # items from the stack vocabulary; unknown = raw token (Q4)
    primary: [angular]
    acceptable: [react, typescript, rxjs]
    excluded: [php, wordpress]   # "excluded unless a primary stack is present"
  seniority: [mid, senior]       # scale intern|junior|mid|senior|lead|manager (Q10)
  location:                      # home city + acceptable_hybrid come from candidate.yaml (Q9)
    work_modes: [remote, hybrid_home, hybrid_low_frequency]
  rules:                         # opinion rules, all OFF by default (Q21)
    russia_market: false
    ai_training_mills: false
    part_time: false
    relocation_required: false
  advanced: {}                   # raw FILTER-knob overrides (Q2)
```

**Layering in `filter_profile`** extends today's `_merge_user`; it does not rewrite it:

`neutral builtin → role pack → expand_search(search) → search.advanced`

`expand_search` is a pure function.

**Neutral builtin.** The owner's values stop being the defaults. The builtin keeps only rules nobody would object to. Everything owner-flavoured moves either into the frontend pack (stack exclusions, CMS/low-code) or into the owner's own profile (the opinion rules, set to on by the migration in M2).

**Stack vocabulary** (`hunter/role_packs/stacks.yaml`). Each entry has:
- a key;
- aliases;
- a prebuilt regex (so `c#`, `.net`, `node.js` and `go` match correctly);
- the roles it belongs to.

A token outside the vocabulary is accepted as a raw, escaped `\b…\b` token flagged "no aliases". Unknown tokens are logged, and the vocabulary grows from that log.

**Cities (Q9).** Hybrid/onsite is acceptable in the home city plus `location.acceptable_hybrid`, a list the user picks from a city vocabulary.
- The anti-hybrid set = all known PL + foreign cities minus the acceptable ones. The Polish base gets the same carve-out as `extra_anti_hybrid_cities` already does (`_carve_home_city`).
- Low-frequency hybrid (at most once a week) stays allowed in any Polish city for everyone.

**Languages (Q11).** Language gates are DERIVED from `languages.spoken`: a gate-vocabulary language (de, fr, nl, …) the user does not speak disqualifies a posting that requires it.
- `exclude_german_language_required` stops being a knob of its own.
- `disqualify_required` stays as an explicit override.
- No proficiency levels.

**Seniority (Q10).** Multi-select over the scale. Every unselected level contributes its vocabulary tokens (EN/PL/RU) to `exclude_levels`. A title with no level always passes; no LLM is involved.

### Role packs (Q3, Q7, Q12)

`hunter/role_packs/<role>.yaml` holds:
- `title_keywords`;
- the base `exclude_patterns`;
- the adjacency list;
- **search terms**: a keyword list per source, which each source turns into its own URL syntax;
- `skill_categories` for the CV;
- prescreen hints.

Rules:
- The **main role's** pack contributes everything.
- An adjacent role's pack contributes only search terms and `title_keywords`. Exclusions always come from the main role, so frontend's "exclude backend" and backend's "exclude frontend" never collide.
- **Only `frontend.yaml` ships in this plan.** It is the owner's current values moved out one to one.
- A new pack is built only when the first real user of that role arrives, from that user's data. It ships only if M0.c's rule passes for it.

### CV variants replace tracks (Q5)

A **track is a CV variant**, a profile `variants` key:
- It has a free-form name and its own stack list (`variants.fe_enterprise.stacks: [angular, rxjs]`).
- It has a "use this variant" checkbox.

Selection works like this:
- `_detect_stack_hint` is replaced by a deterministic score: the overlap between the posting's stack mentions and each enabled variant's list. The highest score wins; a tie or no overlap falls back to the main variant.
- The owner's six current keys map one to one (`fullstack_angular_nest → [angular, nest]`).

What goes away:
- The React flag. React is allowed exactly when it is in `stacks.primary ∪ acceptable`.
- `/tracks`, `CANDIDATE_TRACKS`, `config.active_tracks()` and the `tracks_enabled` key, removed after M5 with one release of aliases.

### Stack gates for other users (Q6)

`is_react_only_job_text`, `is_backend_only_job_text` and the prescreen collapse into one rule: **the posting's main stack is in `excluded` or outside `primary ∪ acceptable`, and none of my `primary` stacks appear.**

- For every profile except the owner's, the gate ships in **warn** mode (`search.advanced.gates.stack_mode: warn|skip`, default `warn`). Switching to skip is manual, after data, the same way the owner introduced the prescreen.
- The owner keeps today's behavior byte for byte, including prescreen's calibrated react-first-only skip.
- The prescreen prompt is parameterised with the candidate's stacks instead of the literal "Angular".

### Automation registry (Q8, Q20)

`hunter/user_prefs.py` is a registry of key, type, default and description:
- `get(key)` resolves `user_settings[current_user]`, then the env var (the operator default), then the registry default.
- Every key is **user-editable with no operator cap** (owner decision Q8). This includes `hunting_enabled`.
- Cost control is deferred to billing (SAAS_PIVOT_PLAN). Until then the operator enables auto_apply for new users on trust.

Keys:
- `auto_apply`
- `max_jobs_per_run`
- `apply_delay_sec`
- `send_docs`
- `outreach`
- `gdpr_clause`
- `pl_resume`
- `hunting_enabled`
- `disabled_sources`

**Disabled sources (Q20).** The hunt is shared, so a per-user switch is a **filter**: jobs from that source are not queued or carded for that user. A board stops being crawled only when the operator's `*_ENABLED` is off, or when every active user has disabled it.

**Contract.** `tests/fixtures/user_prefs_contract.json` lists key, type and allowed values. The API copies it byte for byte, the same pattern as `scout_payload_v1.json` and the pipeline snapshot fixtures. This fixes the key drift:
- `CANDIDATE_TRACKS` → gone;
- `SOURCE_*` → `disabled_sources`;
- `AUTO_APPLY` → `auto_apply`, now actually read.

### Hunt with many roles (Q7)

The hunt stays shared. Query terms come from the packs of the active users' roles (main + adjacent), not from their individual stacks. That keeps the number of distinct queries bounded by the number of packs (≤ 9), not the number of users.

A pack may add stack-specific terms only from the vocabulary (for example bulldogjob `skills,<Stack>`). User stacks drive the per-user FILTER only.

The fan-out itself (union fetch plan, per-source query budget, per-user dedup) is MULTI_USER_UPDATE B3.5. This plan supplies its `SearchSpec`.

### A new user (Q18)

A user who has not confirmed a search profile gets no hunt.

1. The site's onboarding wizard pre-fills `role` and `stacks.primary` from the uploaded resume: a **deterministic** mapping of `core.skills` / role titles onto the stack vocabulary, with no LLM.
2. The user confirms, and the hunt is enabled.
3. With no resume uploaded, the wizard starts empty.

## M0 — free, read-only measurements (decision rules stated first)

- **M0.a Hardcode inventory.** `tools/owner_literals.py` counts `angular|react|frontend|wroc|wrocław` literals in `hunter/filters.py`, `hunter/pipeline/`, `hunter/prescreen.py`, `hunter/sources/` and `hunter/tracker_cache.py`, per file and function.
  - *Rule:* the output IS the M3/M4 checklist.
  - After M4 the count must be 0 outside `role_packs/` and tests. The tool then becomes a ratchet in `test_handoff_readiness.py`.
- **M0.b Owner equivalence.** Replay `filters.classify_job` + `filters.assess_job_text` (+ the stack gates) over the frozen corpus ([EVAL_CORPUS_PLAN](EVAL_CORPUS_PLAN.md)) and today's `postings_seen`, twice:
  1. the current builtin;
  2. the neutral builtin + frontend pack + the owner's migrated `search` section.

  *Rule (Q15):* **100% identical pass/skip verdicts on the whole corpus.** Only the reason NAME may differ; old names stay as aliases for reports (`react_no_angular` → `stack_excluded_without_primary`, `require_angular` → `primary_stack_missing`). Any other diff is fixed in the pack, vocabulary or migration, never waived. M2 and M3 do not merge until this is green.
- **M0.c Pack viability**, run per pack, only when a pack is proposed. `tools/market_memory_m0.py --dump` on the VPS with the proposed pack's search terms.
  - *Rule (Q12):* the pack ships only if **≥ 5 board sources each return ≥ 20 unique listings in one sweep**. Otherwise it waits, and the user is told which boards cover their role poorly.
- **M0.d Settings inventory.** Every setting → current store → target tier → owner. Appendix A below. Done 2026-09-27.

## Milestones — one PR each, in this order (Q14)

**M1 — Automation registry.**
- `hunter/user_prefs.py` + the contract fixture.
- The bot reads per-user `auto_apply` / `max_jobs_per_run` / `send_docs` / `outreach` / `gdpr_clause` / `pl_resume` / `disabled_sources`.
- API: switch the whitelist to the contract, and fix the `/settings` response shape (site expects `{settings: [...]}`).
- Fixes what is broken today, independent of everything else.

**M2 — Search profile core.**
- The `search` section in `profile_schema`, and `profile_render` → `filters.yaml`.
- `role_packs/stacks.yaml` (vocabulary) + `role_packs/frontend.yaml`.
- `filter_profile.expand_search` + the neutral builtin split.
- `UserPaths.filters_yaml` + `FILTERS_YAML_PATH` through `user_env()` (the old FILTERS_YAML M4).
- **Owner migration (Q16):** `tools/migrate_owner_search.py` reads the owner's live `candidate.yaml` / `filters.yaml` / base-CV map. It builds `search` + `search.advanced` + variant stack lists and prints a **dry-run diff** of today's `filters.yaml` against what the render would write. The profile revision is written only after the owner confirms.
- M0.b green.

**M3 — Rules without hardcode.** Each item below sits behind the profile, with a mutation-verified test per rule and M0.b green:
- `_is_unwanted_fullstack`;
- the framework guard;
- `_HOME_CITY` read per call;
- the Polish city carve-out;
- `acceptable_hybrid`;
- languages derived from `spoken`;
- seniority expansion;
- the opinion rules as switches;
- the unified stack gate + `stack_mode`;
- the prescreen prompt parameterised;
- `run_doomed_gate(flt=)`;
- `tracker_cache` primary-stack stat.

**M4 — Search terms from packs.**
- One accessor, `sources.base.search_terms(source_name, spec)`, and every query-driven source reads it.
- LinkedIn geo and the Polish board city slugs keep coming from `candidate.yaml`.
- This is the point where the B3.5 `SearchSpec` plugs in.

**M5 — Variants replace tracks.**
- Variant stack lists and the overlap-score selection.
- Remove `/tracks`, `CANDIDATE_TRACKS`, `active_tracks()` and `tracks_enabled` after one release of aliases.

**M6 — Site/api.**
- The onboarding wizard (resume → role/stacks pre-fill → cities → modes → sources → confirm → hunt on).
- The "Search" form, the "Advanced" tab and the "Automation" form.
- Lives in job-hunter-site / job-hunter-api, against the M1 contract and the M2 schema.

## Non-goals

- Countries outside Poland; Poland + remote only (owner decision).
- Contract type and salary filters (Q13). The only kept contract rule is the part-time / one-month opinion switch.
- Role packs other than frontend. Each waits for a real user and M0.c.
- Generation for non-developer roles and for juniors (tone, `skill_categories`, a `generation_rules.md` audit). Trigger: the second pack ships.
- Per-user schedule or timezone.
- Operator cost caps. Cost control is deferred to billing (Q8).
- User-owned sources (their own Telegram channels / ATS companies). This is the next feature after M4, not this plan.
- Changing any owner calibration (prescreen rule, doomed-gate thresholds).

## Risks

- **A new user's filters are uncalibrated.** Mitigated by the warn-only stack gate and warn-only opinion rules. The first user of a new role is effectively a calibration run.
- **Render latency.** A search edit goes through `profile_jobs` render (≤ 20 s) before the bot sees it. That is acceptable for settings.
- **Equivalence brittleness.** A vocabulary regex that differs subtly from today's hand regex flips a verdict. That is exactly what M0.b exists to catch, on a corpus that does not move ([EVAL_CORPUS_PLAN](EVAL_CORPUS_PLAN.md)).
- **No cost cap (Q8).** A user can set `max_jobs_per_run` high with auto_apply on. The operator keeps the kill switch (`hunting_enabled` is still writable by the admin).

## Decision log — grill session 2026-09-27

| Q | Question | Decision |
|---|---|---|
| 1 | Where the search profile lives | Profile JSON, rendered to `filters.yaml` |
| 2 | Advanced knobs vs render overwrite | Also in Profile JSON (`search.advanced`) |
| 3 | Roles per user | One main role + adjacent roles from the pack's fixed list; exclusions only from the main role |
| 4 | How stacks are entered | Vocabulary with aliases + raw tokens allowed |
| 5 | Choosing a CV variant | Free-named variants with stack lists, overlap score; `/tracks` removed |
| 6 | Stack gates for other users | Warn by default, manual switch to skip; owner unchanged |
| 7 | Hunt with many roles | Shared hunt; queries from the active roles' packs |
| 8 | Automation settings and caps | User edits everything, no operator caps |
| 9 | Hybrid/onsite cities | Home city + acceptable cities; low-frequency hybrid anywhere in PL |
| 10 | Seniority | Multi-select scale; no-level titles pass; junior generation out of scope |
| 11 | Language gates | Derived from `languages.spoken` |
| 12 | Which packs first | Frontend only + the mechanism; a new pack per real user, rule ≥ 5 × ≥ 20 |
| 13 | Contract / salary filters | Neither |
| 14 | Milestone order | Sequential M1 → M6 |
| 15 | Owner equivalence strictness | 100% pass/skip on the whole corpus; reason renames allowed |
| 16 | Owner migration | Script with dry-run diff; revision written after confirmation |
| 17 | Test corpus | Private repo, manifest only here → EVAL_CORPUS_PLAN |
| 18 | New user with no search profile | Pre-filled from the resume, hunt on after confirmation |
| 19 | Corpus and generation scope | Corpus: own plan. Generation for other roles: non-goal |
| 20 | Per-user source switch | A filter; crawl stops only when everyone or the operator disables it |
| 21 | Owner opinion rules | Off by default; on for the owner via the migration |

## Appendix A — settings inventory (M0.d, 2026-09-27)

| Setting | Stored today | Target tier |
|---|---|---|
| identity, contacts, languages, employers | `candidate.yaml` (rendered from Profile JSON) | Account |
| home city, `acceptable_hybrid` | `candidate.yaml` | Search (read from Account) |
| `title_keywords`, `exclude_levels`, `exclude_patterns`, stack rules, body/location rules | `filters.yaml` / builtin | Search (pack + `search` + advanced) |
| `CANDIDATE_TRACKS`, `tracks_enabled`, `/tracks` | env / `config` / `user_settings` | Search (variant stack lists) — removed |
| `tracks.base_cv` | `candidate.yaml` | Profile variants |
| per-source search queries | hardcoded in `hunter/sources/*.py` | Role pack |
| `AUTO_APPLY`, `MAX_JOBS_PER_RUN`, `APPLY_DELAY_SEC`, `TELEGRAM_SEND_DOCS`, `OUTREACH_ENABLED`, `GENERATE_PL_RESUME`, `CV_GDPR_CLAUSE` | env (global) | Automation (`user_settings`, env = default) |
| `*_ENABLED` source toggles | env (global) | Operator (crawl) + Automation (`disabled_sources` filter) |
| `hunting_enabled` | `user_settings` (forced off for non-owners) | Automation |
| `dual_apply_enabled`, `dual_shadow_profile`, `active_llm_profile` | `user_settings` / `config` | Operator |
| judge / verdict / gates / document knobs | `generation.yaml` / env | Operator (advanced) |
| schedule, blackout, retry times, timezone | env | Operator |
| LLM keys, timeouts, queue, backups, paths | env | Operator |
