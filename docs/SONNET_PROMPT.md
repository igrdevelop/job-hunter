# Prompt for Sonnet — candidate.yaml multi-user refactor

You are working in a worktree branch `worktree-candidate-yaml-multi-user`.
The plan is in `docs/CANDIDATE_YAML_PLAN.md` — read it first.

## Context

This project is a job-hunting bot. Currently the candidate's personal identity
(name, city, employers, languages) is hardcoded in ~65 places across 14
production files. We need to extract all of it into a single `candidate.yaml`
config file so that a different person can use the bot without editing source code.

## What to build — 5 waves, one commit each

### Wave 1: Skeleton + identity

1. Add `pyyaml` to `pyproject.toml` `[project.dependencies]`.
   Regenerate lock: `uv pip compile pyproject.toml --all-extras --python-platform linux --python-version 3.11 -o requirements.lock`
   (if `uv` unavailable, use `pip-compile`).

2. Create `candidate.example.yaml` (tracked) — full template with all fields
   and helpful comments. Schema is in the plan. Use realistic PLACEHOLDER
   values (not the real owner's data).

3. Create `hunter/candidate.py`:
   - `load() -> dict` — cached loader, reads `candidate.yaml` from repo root,
     validates required fields, raises `SystemExit` with a clear message if
     missing.
   - `get(dotpath, default=None)` — nested key accessor
     (`get("identity.full_name")`).
   - For tests: make the file path overridable via an env var or a
     `_set_path()` test helper.

4. Edit `generate_docs.py`:
   - `resume_docx_basename()` line 89: name prefix from
     `candidate.get("identity.cv_filename_prefix")`, year from
     `datetime.now().year` (not hardcoded 2026).
   - `build_resume()` line 180-183: name, subtitle (aka), headline, contact
     all from `candidate.get("identity.*")`.
   - `set_author()` line 324: default name from candidate config.

5. Fix test fixtures that assert on "Ihar_Petrasheuski" filenames — they should
   either monkeypatch `hunter.candidate.load` or use the test candidate data.

### Wave 2: Employers

1. `hunter/verdict_refine.py` lines 50-55:
   - `_PROTECTED_EMPLOYERS` → `tuple(candidate.get("employers.protected"))`
   - `_ALTOROS_FLEXIBLE_PROJECTS` → from `candidate.get("employers.flexible.projects")`
   - Stretch prompt (lines 124-131): employer name/period/projects from config.

2. `hunter/content_qa.py`:
   - `_EXPECTED_ROLE_COUNT` (line 37) → `candidate.get("education.expected_role_count", 7)`
   - `_PROFILE_TITLES_NORM` (lines 40-46) → `set(candidate.get("employers.profile_titles", []))`
   - `_REAL_COMPANIES` (lines 49-57) → `set(candidate.get("employers.real_companies", []))`
   - `_check_education()` (line 211) school → `candidate.get("education.school_keyword")`

3. `hunter/apply_api.py`:
   - `_BASE_CV_FILES` (lines 46-53): merge with `candidate.get("tracks.base_cv", {})`
   - Line 475 repair prompt employer list → from config.

4. `hunter/lang_guard.py` line 181-188:
   - Employer names in `_TECH_TERMS`: dynamically extend from
     `candidate.get("employers.real_companies", [])`.

### Wave 3: Location + languages

1. `hunter/filter_config.py` lines 54-62:
   - `FILTER["locations"]` = `["remote", "zdalnie", "zdalna"] + candidate.get("location.home_city_aliases", [])`

2. `hunter/filters.py`:
   - Every `"wroc"` substring check (lines 630, 945) → use the first 4 chars
     of `candidate.get("location.home_city").lower()`.
   - `_assess_foreign_onsite()`: "Wroclaw" reason string → candidate's city.
   - German/French/Dutch language disqualifiers: generalize to read from
     `candidate.get("languages.disqualify_required", ["de", "fr", "nl"])`.

3. `hunter/apply_shared.py` line 755: translator prompt language pair →
   from `candidate.get("languages.cv_languages")`.

### Wave 4: Source URLs

1. `hunter/sources/pracuj.py` line 37: `/wroclaw;wp` → from
   `candidate.get("source_urls.pracuj_location", "wroclaw")`.
2. `hunter/sources/theprotocol.py` line 34: same pattern.
3. `hunter/sources/jobleads.py` line 42: `location=wroclaw` → from config.

Skip `linkedin_scout/` — it's out of scope (standalone tool, different repo).

### Wave 5: Docs

1. Create `docs/SETUP_NEW_USER.md` — step-by-step from clone to first `/hunt`.
2. Update `README.md` Quick Start to reference `candidate.yaml`.
3. Update `CLAUDE.md`: Repository Layout (add candidate.yaml + hunter/candidate.py),
   Important Rules (add "personal facts only through hunter/candidate.py").

## Critical rules

- **Default values = current owner's behavior unchanged.** If `candidate.yaml`
  is absent, the bot must still work with the old hardcoded defaults (graceful
  degradation, not a crash). Load the config lazily and provide fallbacks.
  BUT: log a deprecation warning ("candidate.yaml not found, using built-in
  defaults — create one from candidate.example.yaml").
- **No behavior change for existing tests.** `pytest tests/` must pass without
  editing test assertions (tests may need a `monkeypatch` fixture for the
  candidate config, but the assertions about filtering/generation behavior
  stay identical).
- **One commit per wave.** Commit message format: `Wave N: <what changed>`.
- Run `python -m compileall .` + `ruff check .` + `ruff format .` after each wave.
- Run `pytest tests/` after each wave — all green before committing.
- **Do NOT touch** `.env`, `tracker.xlsx`, `prompts/candidate_profile.md`,
  or any gitignored personal files.
- **Do NOT** add the real owner's data (name, phone, email) into tracked files.
  The `candidate.example.yaml` must use generic placeholder values.
- Update `CLAUDE.md` in the wave-5 commit (or earlier if schema changes warrant it).

## Final verification

After all 5 waves:
```bash
python -m compileall . && ruff check . && ruff format --check .
pytest tests/
grep -rniE "ihar|petrasheuski" hunter/ generate_docs.py apply_agent.py \
  | grep -v candidate.py | grep -v example | grep -v '\.pyc'
# ^ must return 0 lines (all personal data moved to config)
```

## Don't forget

- `pyproject.toml` edit → regenerate `requirements.lock`
- The `content_qa.py` education check (line 211) is the sneakiest one — it
  checks for "belarusian state technological university" by exact substring.
- `generate_docs.py` line 183 has phone number, email, LinkedIn URL, and city
  all in one string — all must come from config.
- `verdict_refine.py` stretch prompt (lines 124-131) injects employer name +
  period + project list into the LLM prompt — must come from config.
- Tests in `test_ats_pdf_roundtrip.py` and `test_dual_apply.py` create files
  named `Ihar_Petrasheuski_CV_*.pdf` — these need to use the test fixture's
  candidate config.
