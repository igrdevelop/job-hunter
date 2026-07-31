# candidate.yaml — Multi-User Configurability Plan

**Goal:** a friend clones the repo, fills in ONE config file + prompt files,
and runs the bot on their own machine without editing any source code.

**Scope:** "easy version" — single user per instance, runs locally on their PC
(not Docker deploy). Telegram bot + LLM key + candidate config = working system.

---

## What exists today

- Personal prompt files are already gitignored; `.example` templates ship.
- `.env.example` documents all env vars.
- `README.md` has a Quick Start section.

## What blocks a second user

The candidate's identity is hardcoded in **~65 places across 14 production files**.
All of them need to read from a single config file instead.

---

## The config file: `candidate.yaml`

A new gitignored YAML file (tracked `candidate.example.yaml` as template).
Lives in the repo root next to `.env`. Loaded once at startup by a new
`hunter/candidate.py` singleton module.

```yaml
# candidate.example.yaml
identity:
  full_name: "Jane Doe"
  aka: ""                          # optional "also known as" subtitle on CV
  cv_filename_prefix: "Jane_Doe_CV"
  headline: "Senior Frontend Developer"
  contact: "+48 123 456 789 | jane@example.com | linkedin.com/in/jane | Warsaw, Poland"

location:
  home_city: "Warsaw"              # primary city for filters
  home_city_aliases:               # lowercase variants the filters should match
    - "warszawa"
    - "warsaw"
  acceptable_hybrid: ["Warsaw"]    # cities where hybrid is fine
  weekly_hybrid: ["Kraków"]        # cities for ≤1 day/week hybrid
  work_authorization: "EU"         # EU | US | any — feeds doomed gate

languages:
  spoken: ["pl", "en"]             # all languages candidate speaks
  cv_languages: ["en", "pl"]       # which CV language variants to generate
  disqualify_required:             # required-language in posting = skip
    - "de"
    - "fr"
    - "nl"

employers:
  protected:                       # verifiable, never touched by stretch rounds
    - "Company A"
    - "Company B"
  flexible:                        # may absorb stretch-round tech additions
    name: "Agency X"
    period: "2018-2022"
    projects: ["E-commerce", "Healthcare"]
  real_companies:                  # known real employers (QA check)
    - "company a"
    - "company b"
    - "agency x"
  profile_titles:                  # canonical role titles from the profile
    - "senior frontend developer"
    - "frontend developer"

education:
  school_keyword: "university of warsaw"   # lowercase substring for QA check
  expected_role_count: 5                   # how many roles in resume_en

tracks:
  base_cv:                         # stack key -> prompt filename
    angular: "base_cv_angular.md"
    react: "base_cv_react.md"

# Source-specific location overrides (optional)
source_urls:
  pracuj_location: "warszawa"      # replaces /wroclaw;wp in Pracuj URLs
  theprotocol_location: "warszawa"
  jobleads_location: "warszawa"
```

---

## Implementation: 5 waves (one PR each)

### Wave 1 — Skeleton + identity (generate_docs.py)

**New files:**
- `candidate.example.yaml` — tracked, full template with comments
- `hunter/candidate.py` — loader module

**`hunter/candidate.py`** contract:
```python
import yaml
from pathlib import Path
from functools import lru_cache

_CANDIDATE_PATH = Path(__file__).resolve().parent.parent / "candidate.yaml"

@lru_cache(maxsize=1)
def load() -> dict:
    """Load and validate candidate.yaml. Raises SystemExit with a
    clear message if the file is missing or required fields are absent."""
    if not _CANDIDATE_PATH.exists():
        sys.exit("ERROR: candidate.yaml not found. Copy candidate.example.yaml and fill it in.")
    with open(_CANDIDATE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _validate(data)
    return data

def get(dotpath: str, default=None):
    """Read a nested key: get("identity.full_name")."""
    ...
```

**`generate_docs.py` changes:**
- `resume_docx_basename()`: name prefix + year from `candidate.get("identity.cv_filename_prefix")` + `datetime.now().year`
- `build_resume()`: name, aka, headline, contact from `candidate.get("identity.*")`
- `set_author()`: default from `candidate.get("identity.full_name")`

**Tests:** existing `test_generate_docs.py` + `test_ats_pdf_roundtrip.py` get a
`monkeypatch` fixture that patches `hunter.candidate.load` with test data.

### Wave 2 — Employers (verdict_refine.py, content_qa.py, apply_api.py, lang_guard.py)

**`hunter/verdict_refine.py`:**
- `_PROTECTED_EMPLOYERS` → `tuple(candidate.get("employers.protected"))`
- `_ALTOROS_FLEXIBLE_PROJECTS` → from `candidate.get("employers.flexible")`
- Stretch prompt block: `{altoros_projects}` + `{protected_employers}` + flexible employer name/period all from config

**`hunter/content_qa.py`:**
- `_EXPECTED_ROLE_COUNT` → `candidate.get("education.expected_role_count", 7)`
- `_PROFILE_TITLES_NORM` → `set(candidate.get("employers.profile_titles", []))`
- `_REAL_COMPANIES` → `set(candidate.get("employers.real_companies", []))`
- `_check_education()` school name → `candidate.get("education.school_keyword")`

**`hunter/apply_api.py`:**
- `_BASE_CV_FILES` → merge defaults with `candidate.get("tracks.base_cv", {})`
- Repair prompt employer order → from `candidate.get("employers.real_companies")`

**`hunter/lang_guard.py`:**
- Employer names in `_TECH_TERMS` → dynamically add from `candidate.get("employers.real_companies")`

### Wave 3 — Location + languages (filter_config.py, filters.py)

**`hunter/filter_config.py`:**
- `FILTER["locations"]` → build from `["remote", "zdalnie", "zdalna"] + candidate.get("location.home_city_aliases")`
- `allow_weekly_hybrid_*` toggles → driven by whether `weekly_hybrid` list is non-empty

**`hunter/filters.py`:**
- `_matches_location()`: no code change needed if `FILTER["locations"]` is correct
- `"wroc"` substring checks in `_assess_foreign_onsite` → `candidate.get("location.home_city").lower()[:4]` or the aliases
- `_GERMAN_REQUIRED_RES` and `_UNSUPPORTED_LANG_REQUIRED_RES` → generalize: build the combined disqualifier regex set from `candidate.get("languages.disqualify_required")`
- `_ANTI_HYBRID_CITIES` / `_WEEKLY_HYBRID_CITIES` → keep the static sets (they are Polish geography, not candidate-specific), but the "wroc" veto becomes the candidate's city
- Work auth (`_WORK_AUTH_RES`) → keep as-is for EU; generalize only if `work_authorization != "EU"` (stretch goal, not wave 3)

**`hunter/apply_shared.py`:**
- `_RESUME_TRANSLATE_SYS` → build language pair from `candidate.get("languages.cv_languages")`

### Wave 4 — Source URLs (sources/pracuj.py, theprotocol.py, jobleads.py)

**3 source files** with hardcoded `/wroclaw;wp` or `location=wroclaw`:
- Read `candidate.get("source_urls.pracuj_location", "wroclaw")` etc.
- Substitute into the `LISTING_URLS` list at class init or module load

**`linkedin_scout/`** — out of scope (it's a standalone desktop tool, will be
split to its own repo; the friend won't use it).

### Wave 5 — Docs + smoke test

- `docs/SETUP_NEW_USER.md` — step-by-step from clone to first `/hunt`:
  1. Clone repo
  2. `pip install -e .` (or just `pip install -r requirements.txt`)
  3. Install LibreOffice (link)
  4. Create Telegram bot via BotFather, get token + chat_id
  5. Copy `.env.example` → `.env`, fill in 3 required vars
  6. Copy `candidate.example.yaml` → `candidate.yaml`, fill in identity
  7. Copy `prompts/*.example.md` → fill in with real experience
  8. `python hunter.py` → `/start` in Telegram
  9. `/hunt justjoin` — first test run
- Update `README.md` Quick Start to reference candidate.yaml
- Update `CLAUDE.md` Repository Layout + Important Rules

**Smoke test:** create a fake `candidate.yaml` with made-up data, run
`pytest tests/` — all tests must pass without touching any source file.
Run `python -c "from hunter.candidate import load; load()"` to verify
validation works.

---

## What is explicitly NOT in scope

- **Multi-tenant** (multiple candidates in one bot instance)
- **LinkedIn Scout** (`linkedin_scout/` — standalone, will be its own repo)
- **Prompt file generation** (the friend writes their own `candidate_profile.md`)
- **Google OAuth setup automation** (the friend runs `tools/gsheets_auth.py` manually)
- **Language gate generalization beyond PL/EN** (the module assumes two CV languages;
  adding a third like DE/RU is a separate future effort)
- **Work authorization generalization** (US-candidate support, visa sponsorship logic)

---

## Verification

After all 5 waves:
```bash
# 1. Syntax + lint
python -m compileall . && ruff check . && ruff format --check .

# 2. Full test suite (no source changes should break existing tests)
pytest tests/

# 3. Smoke: load candidate config
python -c "from hunter.candidate import load; print(load()['identity']['full_name'])"

# 4. grep audit — zero personal data outside candidate loader + example file
grep -rniE "ihar|petrasheuski|altoros" hunter/ generate_docs.py llm_client.py apply_agent.py \
  | grep -v candidate.py | grep -v example
# must return 0 lines
```

---

## Dependency

`pyyaml` — add to `pyproject.toml [project.dependencies]`, regenerate
`requirements.lock`.
