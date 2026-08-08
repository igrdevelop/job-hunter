# FILTERS_YAML Plan — per-user job-filter settings as data files

**Status:** M1 done (2026-08-08); M2–M5 pending
**Date:** 2026-08-08
**Motivation:** owner request (chat, 2026-08-08): "надо бы вынести это в отдельные
файлы с настройками, чтобы потом другие люди могли свои настройки юзать".
Phase B3.5 (docs/MULTI_USER_UPDATE.md) already promises "for each active user
apply THEIR filters" but has no concrete design for where those filters live —
this plan is that design. It also serves the public-repo goal
(docs/quality/07): today a new adopter must edit source code
(`hunter/filter_config.py`) to change what jobs the bot hunts for.

## Problem

Job-intake policy is spread across two stages and three kinds of places, and
all of it is single-user:

1. **Listing-stage filters** — the `FILTER` dict in `hunter/filter_config.py`
   (~25 keys: title keywords, level/stack exclusions, ~60 regex patterns,
   policy toggles, company blocklist, anti-hybrid city list). Consumed by
   `hunter/filters.py` (23 read sites) **and directly by 10 source modules**
   (bulldogjob, pracuj, theprotocol, justjoin, solidjobs, inhire, jobleads,
   telegram_channels, workingnomads, sources/base — mostly `title_keywords`
   for prefiltering).
2. **Full-text stage (doomed gate)** — `assess_job_text` in filters.py +
   module-level constants built at import (`_ANTI_HYBRID_CITIES` is
   `frozenset(...) | FILTER["extra_anti_hybrid_cities"]`,
   `_GERMAN_REQUIRED_RES`, `_FULLSTACK_RE`).
3. **Already-per-user pieces** — `FILTER["locations"]` and the home-city
   substring derive from `candidate.yaml`; spoken languages too.

A second user with a different stack (React/Vue) or city gets the owner's
Angular/Wrocław policy no matter what their candidate.yaml says. And the owner
himself cannot tune a filter without a code commit + deploy.

**Owner-hardcoded spots this audit surfaced** (they must be generalized, or a
second user can't meaningfully use the file at all):

- `require_angular` and `exclude_react_without_angular` hardcode the *word*
  "angular"/"react" in filters.py logic — useless knobs for a React user.
- The low-frequency-hybrid exception (`allow_low_frequency_hybrid`, renamed
  and broadened 2026-08-08 from the old Warsaw/Kraków-only
  `allow_weekly_hybrid_warsaw_krakow`, PR #194) is already city-agnostic
  within Poland — but "within Poland" itself (`_PL_ANTI_HYBRID_CITIES`) is
  a home-country assumption baked into filters.py. Good enough for now;
  full home-country generalization is open question #6, NOT this plan's
  scope.
- A user whose home city is IN `extra_anti_hybrid_cities` (e.g. Berlin) would
  have their own city rejected — home city must auto-carve out of the
  anti-hybrid set.

## The three layers — where everything lives

```
Layer 0 — MECHANISMS (code, hunter/filters.py; not user-editable)
    HOW things are detected: the 40-pattern German-requirement regex battery,
    _FULLSTACK_RE (EN+RU spellings), onsite/hybrid phrase detection, the
    days-per-week hybrid-frequency parser, relocation phrasing, min-text
    floors. Users don't write these regexes — they switch them on/off and
    feed them lists via Layers 1-2.

Layer 1 — SHARED DEFAULTS (hunter/filter_profile.py :: builtin_defaults())
    THE common settings, one copy for everyone: every knob below with today's
    FILTER value. Code-reviewed like today. This is what you get with no user
    file at all, and what every user file is merged ON TOP of.

Layer 2 — USER OVERRIDES (users/{uid}/candidate/filters.yaml)
    Personal policy. Missing file ⇒ Layer 1 as-is (owner behavior today,
    byte-for-byte). Merge semantics per key: `replace` or `extend_only` —
    see the table.

(Cross-cutting, NOT in filters.yaml: candidate.yaml supplies home city
aliases + spoken languages; DOOMED_GATE_* env vars stay global ops levers;
/tracks stays a runtime Telegram toggle.)
```

An optional **Layer 1.5** — a host-level `shared/filters.yaml` the admin can
edit without a deploy, applied between builtin and user — is deliberately an
open question (#4): it adds real flexibility for a multi-user host but also a
third place to look when debugging "why was this job skipped".

## Knob-by-knob map

Merge column: **replace** = user value fully replaces the default;
**extend** = user can only add entries, calibrated defaults can't be removed;
**derived** = computed from candidate.yaml, not present in filters.yaml;
**code** = Layer 0, not a setting.

### Stage 1 — listing filter (title / company / location)

| Knob | Today | User-editable? | Merge |
|---|---|---|---|
| `title_keywords` | angular, frontend, js, ts | yes — a React user replaces the list | replace |
| `require_title_terms` (generalizes `require_angular`) | off | yes — e.g. `["react"]` to demand it in every title | replace |
| `exclude_levels` | junior/intern/techlead (EN+RU)… | yes | replace |
| `exclude_patterns` (title regex) | ~60 patterns: java, .net, vue, wordpress… | yes — these encode the OWNER's stack; a Java-seeker empties/replaces them | replace |
| `exclude_react_without_angular` → `exclude_stacks_without` (generalized: `{unless: "angular", block: ["react"]}`) | on | yes | replace |
| `exclude_fullstack_with_backend` + `fullstack_backend_stacks` | on; java/spring/.net/python/… | yes — both the toggle and the backend list | replace |
| `locations` (accepted location tokens) | remote + Wrocław aliases | — | derived (candidate.yaml `home_city_aliases`) |
| `exclude_companies` (AI-mill blocklist) | micro1, alignerr, mercor… | can ADD own; cannot remove (2026-07-06 incident calibration) | extend |

### Stage 2 — full-text gates (listing body + doomed gate)

| Knob | Today | User-editable? | Merge |
|---|---|---|---|
| `exclude_german_language_required` | on | yes — turn OFF if you speak German. Default smarter: auto-off when candidate.yaml `languages` contains German | replace |
| `exclude_body_disqualifiers` + `body_exclude_patterns` | on; blazor/mendix/wordpress… | yes — stack-personal, same as title patterns | replace |
| `exclude_body_onsite_city` (hybrid-elsewhere rejection) | on | yes (toggle). The city SET is: base list (code) + `extra_anti_hybrid_cities` (extend) **minus the user's own home-city aliases** (auto-carve-out, new rule) | toggle: replace; city list: extend |
| `allow_low_frequency_hybrid` (boolean; renamed 2026-08-08 from `allow_weekly_hybrid_warsaw_krakow`, now city-agnostic within Poland) | on | yes — keep/reject hybrid roles needing the office ≤1 day/week | replace |
| `exclude_unacceptable_contract` (part-time/short) | on | yes | replace |
| `exclude_relocation_required` | on | yes | replace |
| `exclude_ai_training` (toggle) | on | yes (it's their spam budget) — but the company list above stays extend-only while on | replace |
| German/fullstack/onsite/relocation DETECTION regexes | — | no | code |
| Doomed-gate HARD/SOFT machinery, thresholds, min-text floors | — | no (`DOOMED_GATE_*` env = global ops) | code |

### What a user file looks like

```yaml
# users/42/candidate/filters.yaml — React developer, Gdańsk, speaks German
title_keywords: [react, frontend, front-end, javascript, typescript]
require_title_terms: []            # don't demand any single term in titles

exclude_levels:
  - junior
  - intern
  - praktykant

exclude_patterns:                  # replaces the owner's Angular-centric list
  - '\bjava\b'
  - '\bphp\b'
  - '\bangular\b'                  # this user does NOT want Angular roles
  - '\bwordpress\b'

exclude_stacks_without: null       # no "X without Y" rule at all
exclude_fullstack_with_backend: true
fullstack_backend_stacks: ['\bjava\b', '\.net\b', '\bpython\b']

exclude_german_language_required: false   # speaks German — keep those jobs
allow_low_frequency_hybrid: true          # hybrid with rare office visits is OK

exclude_companies:                 # ADDED to the shared blocklist, not replacing it
  - "some local staffing mill"
```

Everything not mentioned in the file keeps the Layer 1 default. Home city /
accepted locations come from that user's candidate.yaml, as today.

## Non-goals

- Web UI implementation details (component code, styling) — but the PAGE
  design and its API contract are in scope now, as M5: the file format is
  designed so the site's settings page is just another writer of the same
  keys (owner request 2026-08-08).
- No per-user scrape queries — that's B3.5's SearchSpec/union-fetch work.
  Source modules keep reading the owner profile at scrape time; per-user
  filtering happens at the filter/fan-out step. SearchSpec can later read the
  same file.
- No change to `DOOMED_GATE_*` env toggles — global ops levers.
- No new LLM calls (pure deterministic config plumbing).
- linkedin_scout's vendored `location_gate.py` untouched (repo split pending).

## M0 — Measure (done, 2026-08-08)

Read-only plumbing audit:

```bash
grep -rn "FILTER\[\|FILTER\.get" hunter/ | grep -v filter_config.py
```

Result: 23 read sites in filters.py + ~20 across 10 source modules; one
import-time constant mixing FILTER data into compiled form
(`_ANTI_HYBRID_CITIES`). Decision rule was: ≤3 personal keys all derivable
from candidate.yaml ⇒ extend candidate.yaml instead. Actual: ~14 personal
knobs of genuine filter policy that do NOT belong in an identity file ⇒ a
separate `filters.yaml` next to candidate.yaml is justified. Additionally the
audit surfaced the three owner-hardcoded generalization targets listed in
Problem — they become part of M2's scope.

## M1 — Loader + builtin profile (one commit) ✅ done 2026-08-08

**Migration mapping (what moves where):**

| Today | After M1 |
|---|---|
| `filter_config.py` FILTER dict (values + WHY-comments) | `filter_profile.builtin_defaults()` — moved verbatim, comments preserved |
| `filter_config.py` module surface | kept as a shim: `FILTER = load_profile()` — every existing `from hunter.config import FILTER` import works unchanged |
| filters.py `_ANTI_HYBRID_CITIES` (import-time, bakes FILTER data in) | per-profile cached builder (M2) |
| filters.py detection regexes (`_GERMAN_REQUIRED_RES`, `_FULLSTACK_RE`, …) | stay in filters.py — mechanism, not settings |
| — (new) | `candidate/filters.example.yaml` template (M3), `users/{uid}/candidate/filters.yaml` at runtime (M4) |

- New `hunter/filter_profile.py`:
  - `builtin_defaults()` — today's FILTER verbatim (moved from
    filter_config.py, WHY-comments preserved; filter_config.py re-exports
    `FILTER = load_profile()` so every existing import keeps working).
  - `load_profile(path=None)` — deep-copy Layer 1, merge user YAML per the
    knob table's strategy column. Cache keyed by **(resolved path,
    file mtime_ns)** — NOT a plain `@lru_cache` on path: the file will be
    written by an external process (the API's `PUT /filters` endpoint, M5),
    so a long-lived bot process must pick up changes without a restart.
    An edit bumps mtime ⇒ new cache key ⇒ fresh load, by construction; a
    missing file caches on `(path, None)`. Same per-path idea as
    candidate.py post-B3.4, one extra tuple element.
  - Validation at load: `re.compile` every `*_patterns` entry — invalid
    pattern dropped with ONE warning naming file/key/error, never raises;
    type checks per key; unknown keys warn + ignore.
  - Home-city carve-out: the effective anti-hybrid set subtracts the
    profile's own `home_city_aliases`.
- Missing file ⇒ byte-for-byte today's FILTER.
- **Quality-parity guard — two levels, both committed BEFORE the move:**
  1. *Dict equality:* `load_profile()` (no user file) `==` a frozen literal
     copy of today's FILTER, key for key.
  2. *Golden verdict parity:* new `tests/test_filter_profile_parity.py` +
     `tests/fixtures/filter_parity/golden_verdicts.json`. The golden file is
     generated against CURRENT (pre-refactor) code by a tiny one-off script:
     it runs `classify_job`/`screen_job_text`/`assess_job_text` over a fixed
     corpus — the existing `tests/fixtures/sample_jobs/` postings plus one
     synthetic case per rule family (each exclude_level, a sample of
     exclude_patterns incl. the tricky `\bc#` one, react-without-angular,
     fullstack±angular±backend, German required/not-required, onsite-city,
     low-frequency hybrid 1 day/week vs 3 days/week, AI-mill company, RU
     tech-lead titles) — and records each verdict + reason string. After the move the
     test replays the corpus through the new loader path and asserts every
     verdict/reason is IDENTICAL. This catches what dict equality can't: a
     subtle behavior change in how a moved value is consumed.
  3. Mutation-verify both (flip one builtin default → parity test must fail).
- Other tests: merge strategies, invalid-regex drop, unknown-key warn,
  carve-out, mtime-based cache invalidation (write file, assert reload).
- Rollback: revert the commit — the re-export restores the literal dict.

## M2 — Thread the profile through filters.py + generalize (one commit)

- `classify_job`, `apply_filters_with_stats`, `screen_job_text`,
  `assess_job_text` + helpers gain `flt: Mapping = FILTER` (default = owner
  profile ⇒ zero behavior change, zero caller changes yet).
- `_ANTI_HYBRID_CITIES` becomes a cached per-profile builder.
- Generalizations (defaults reproduce today's behavior exactly):
  - `require_angular` → `require_title_terms: list[str]` (default `[]`;
    legacy key still honored by the loader).
  - `exclude_react_without_angular` → `exclude_stacks_without` (default
    `{unless: "angular", block: ["react"]}`; legacy key honored). NOTE: the
    react-track gating (`_react_track_active()`) is preserved on top.
  - ~~`allow_weekly_hybrid_warsaw_krakow`~~ — already generalized in code
    (PR #194, 2026-08-08): now the boolean `allow_low_frequency_hybrid`,
    city-agnostic within Poland. Nothing to do here beyond treating it as a
    normal user-editable toggle. The remaining Poland-scoping is open
    question #6, not M2 work.
- Tests: entire existing filter suite passes unchanged; legacy-key aliases
  tested; one test proves a non-default `flt` flips a classify_job verdict
  (mutation-verify).

## M3 — User file template + docs (one commit)

- `candidate/filters.example.yaml` (tracked): every personal knob, today's
  default value, calibration comments carried over.
- `docs/SETUP_NEW_USER.md` + `candidate/README.md` sections; CLAUDE.md same
  commit (house rule).
- Owner's own optional `filters.yaml` documented: edit + `/hunt`, no deploy.

## M4 — Per-user wiring (blocked on B3.5, lands with it)

- Path via `users.user_paths()` (the `UserPaths` dataclass in
  `hunter/users.py` currently exposes only `candidate_yaml`/
  `applications_dir`/`templates_dir` — add a `filters_yaml` attribute
  pointing into the same candidate dir); B3.5 hunt fan-out passes
  `flt=load_profile(user_filters_path)` per user; apply subprocess gets
  `FILTERS_YAML_PATH` via `user_env()` (mirrors `CANDIDATE_YAML_PATH`) so the
  doomed gate in the child screens with the right profile.
- Until B3.5, M1–M3 already deliver: single-user no-deploy tuning +
  public-repo adoptability.

## M5 — Web settings page (site + api repos; after M1–M3, independent of M4)

The bot side needs nothing beyond M1's mtime-aware cache — the page is just
another writer of `users/{uid}/candidate/filters.yaml`.

### API contract (NestJS, api repo — see that repo's docs/FILTERS_API_PLAN.md)

Routes follow the api repo's existing convention: the user comes from the JWT
(`@CurrentUser()`), never from a path param — same shape as the existing
`GET/PUT /api/settings`.

- `GET /api/filters` → `{ defaults, overrides, effective, meta }`:
  - `defaults` — Layer 1 values (so the UI can render placeholder state and
    "reset to default");
  - `overrides` — the user file's raw content (what the user actually set);
  - `effective` — the merged result the bot will use;
  - `meta` — per-key merge strategy (`replace`/`extend_only`) + which keys
    are derived (locations → candidate.yaml) so the UI knows what to render
    read-only.
- `PUT /api/filters` — body = overrides only (never the merged dict);
  server validates with THE SAME rules as the bot loader (regex compile,
  types, unknown keys, extend-only protection) and writes the YAML
  atomically to `users/{uid}/candidate/filters.yaml` (via the api's
  `UserPathsService`; the `candidate/` volume is already rw-mounted there).
  Validation errors return per-field, e.g.
  `{ "exclude_patterns[3]": "invalid regex: unbalanced parenthesis" }`.
- **Validation parity across languages** (checked 2026-08-08): the api
  container is `node:22-alpine` in a SEPARATE compose project — no Python,
  no shared exec, so "shell out to the bot's loader" is impossible. The api
  therefore implements the validator in TypeScript, and parity is enforced
  the same way the scout payload contract is: a **versioned shared fixture
  file** (`filters_contract_v1.json`: valid profiles, invalid regexes,
  type errors, extend-only violations, merge input/expected-output cases)
  committed to BOTH repos, with a contract test on each side asserting
  identical accept/reject/merge results. Schema drift then fails a test
  loudly instead of diverging silently. One JS/Python regex-dialect caveat
  lives in the fixture set: patterns must stick to the common subset (no
  Python-only `(?P<name>)`, no JS-only `(?<name>)` in user files — the
  validator rejects both with a "portable regex only" error).
- `POST /api/filters/preview` ("test on a vacancy") — **deferred to v2**:
  the verdict comes from Python `classify_job`, which the api container
  cannot run (above). Options recorded for v2: a minimal HTTP sidecar in
  the bot's compose, or a shared-volume request/response file the bot polls.
  The page ships without the preview section (or with it visibly disabled)
  until then.

### Page layout ("Job Filters" page, site repo, Angular)

Sections mirror the knob table, grouped by what the user is deciding, not by
implementation stage:

1. **Что ищем** — `title_keywords` (chip input), `require_title_terms`
   (chip input, empty = off).
2. **Уровень и роль** — `exclude_levels` (chip input, prefilled with
   defaults; quick-toggles for common groups: junior/intern, lead/management,
   part-time). The group checkboxes are UI-only shortcuts over the flat word
   list — `filters.yaml` stores ONLY the resulting words, groups never appear
   in the file. Group→words mapping is a UI constant (junior/intern =
   junior, intern, internship, trainee, stażysta, praktykant, staz;
   lead/management = the tech-lead EN+RU spellings, project lead, engineering
   manager, head of engineering, vp of engineering, cto; part-time = the
   three spellings). A group checkbox is checked when ALL its words are in
   the list, unchecked when NONE are, and **indeterminate** (partial state)
   when the user hand-removed some of them via the chip list — it must never
   show a plain checkmark over a partially-disabled group. Toggling an
   indeterminate checkbox re-adds the full group.
3. **Стек** — `exclude_patterns` (chip input in "simple words" mode by
   default — the loader wraps plain words in `\b...\b`; an "advanced: raw
   regex" toggle per open question #1 reveals raw entries with live
   validation); `exclude_stacks_without` (two selects: "block [react] unless
   title also has [angular]", clearable); fullstack block (checkbox
   `exclude_fullstack_with_backend` + chip list `fullstack_backend_stacks`,
   list disabled when checkbox off).
4. **Локация и гибрид** — read-only info card: home city + accepted
   location tokens, sourced from candidate.yaml, with a link to the profile
   page ("менять здесь" — NOT duplicated into filters); checkbox
   `exclude_body_onsite_city`; checkbox `allow_low_frequency_hybrid`
   ("гибрид с визитами ≤1 дня в неделю — ок", Polish cities only per the
   current mechanism); anti-hybrid city additions (extend-only pattern:
   shared list rendered as non-removable grey chips, user's own additions
   as normal removable chips).
5. **Языки** — checkbox `exclude_german_language_required`, with an
   auto-hint when candidate.yaml lists German ("вы указали немецкий в
   профиле — фильтр отключён по умолчанию", per open question #5).
6. **Контракт** — checkboxes `exclude_unacceptable_contract`,
   `exclude_relocation_required`.
7. **Защита от спама** — checkbox `exclude_ai_training`; `exclude_companies`
   in the same extend-only two-tone chip pattern as the city list.
8. **Проверить на вакансии** (preview widget) — paste a
   title/company/location (or full text) → shows ACCEPT/SKIP + the exact
   reason string, evaluated against the CURRENT DRAFT (unsaved) state via
   the preview endpoint. **v2 — deferred:** the endpoint needs Python
   `classify_job`, unavailable to the api container (see API contract
   above); the section ships hidden or visibly disabled in v1.

### Interaction rules

- **Default vs overridden is always visible:** a control at its Layer 1
  default renders normally; an overridden one gets a marker (dot/badge) and
  a per-field "reset to default" affordance. Page-level "reset all" too.
  This is why `GET` returns `defaults`+`overrides` separately.
- **Save model:** dirty-state save bar (save / discard), PUT sends only
  overrides — a field reset to default is REMOVED from the file, not saved
  as an equal copy (otherwise a later builtin-default improvement would be
  silently pinned by stale user copies).
- **Validation:** inline per-field from the PUT error shape; the save is
  all-or-nothing (server never writes a partially valid file).
- **Extend-only keys:** shared entries visually distinct and non-removable;
  tooltip explains why ("защита, откалиброванная на реальных инцидентах").
- **Effect timing:** after save, the bot picks the change up on the next
  hunt cycle via the mtime cache key — the page states this ("применится со
  следующего цикла охоты") so nobody expects retroactive re-filtering.

## Risks

- **Broken user regex kills filtering** → compile-at-load, drop-with-warning.
- **User file disables calibrated protections** → `extend_only` on
  `exclude_companies`/`extra_anti_hybrid_cities` makes removal impossible;
  a test locks the strategy table.
- **Owner behavior drift during refactor** → M1 byte-for-byte equality test +
  untouched existing filter suite + legacy-key alias tests; mutation-verify.
- **Import-time constant staleness** → per-profile cached builders (M2), test
  feeds two profiles, asserts different city sets.
- **Two sources of truth** → filters.yaml never duplicates candidate.yaml
  (locations/languages stay derived); the loader reads them through
  `candidate.get()` exactly as filter_config.py does today.
- **UI validation drifting from loader validation** (M5) → the api's TS
  validator and the bot's Python loader both test against the SAME versioned
  fixture file (`filters_contract_v1.json`, committed to both repos — the
  scout-payload-v1 pattern); drift fails a contract test on whichever side
  changed. Portable-regex-subset rule keeps the two regex dialects honest.
- **Stale profile in a long-lived bot process after a web save** → mtime in
  the cache key makes staleness impossible by construction; the M1 test
  writes the file mid-process and asserts the next load sees it.

## Cost

Zero LLM calls, zero network. One YAML read per profile per process (cached).
No schedule/Docker changes until M4 (one env var in `user_env()`).

## Open questions (owner decisions)

1. **Regex power for users:** raw regex in user `exclude_patterns` (validated
   at load) vs plain keyword lists only (loader wraps in `\b...\b`)?
   Recommendation: plain lists for `exclude_levels`-style keys, validated raw
   regex allowed in `*_patterns` — power users exist.
2. **Empty `exclude_patterns` allowed** (user genuinely wants Java)?
   Recommendation: yes — stack exclusions are personal, `replace` semantics.
3. **`/tracks` fold into filters.yaml?** Recommendation: no for now — it's
   runtime-switchable via Telegram, filters.yaml is file-edit-only; revisit
   when the web UI writes filters.
4. **Layer 1.5 (host-level `shared/filters.yaml`, admin-editable, applied
   between builtin and user)?** Recommendation: skip until a second real user
   exists — builtin defaults + extend-only keys cover the protection story;
   a third merge source complicates "why was this job skipped" debugging.
5. **Auto-derive `exclude_german_language_required` from candidate.yaml
   `languages`** (speaks German ⇒ default off)? NOTE: candidate.yaml has TWO
   candidate fields — `languages.spoken` (what the user speaks) and
   `languages.disqualify_required` (language codes that already disqualify a
   posting in the doomed gate; removing `de` there already keeps
   German-required jobs). Deriving from `languages.spoken` would partially
   duplicate the second knob. Recommendation: derive the default from
   `languages.spoken`, keep `disqualify_required` as the doomed-gate list it
   is, and let an explicit filters.yaml key win over both.
6. **Home-country generalization of the low-frequency-hybrid exception:**
   `allow_low_frequency_hybrid` currently applies only to POLISH anti-hybrid
   cities (`_PL_ANTI_HYBRID_CITIES` in filters.py) — an owner-geography
   assumption. Generalize "domestic = user's home country from
   candidate.yaml" later, or accept Poland-scoping for now? Recommendation:
   accept for now (all current users are Poland-based); revisit with B3.5.
