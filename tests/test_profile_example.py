"""tests/test_profile_example.py — candidate/profile.example.json loads and
validates, and every M0a dotpath has a source in the schema.

docs/RESUME_PROFILE_STORE_PLAN.md M0a re-run (2026-08-30, master @ c233b87)
found 24 `candidate.get()` dotpaths in production code. The renderer
(hunter/profile_render.py, built in a later step) is what actually turns a
Profile into a candidate.yaml satisfying all 24 — this test only proves the
INPUT exists: for each dotpath, either a direct Profile field, or one of the
documented render-time derivations (employers.real_companies/profile_titles/
history from roles+employers, tracks.base_cv from variants keys,
source_urls.* from location.home_city — see hunter/profile_render.py's own
docstring once it exists, and CLAUDE.md's Repository Layout entry for it).
"""

from __future__ import annotations

import json
from pathlib import Path

from hunter.profile_schema import Profile, from_dict, validate

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "candidate" / "profile.example.json"

# Every M0a dotpath -> a predicate over the loaded Profile proving its
# render-source exists. Direct fields resolve from Core; the four commented
# ones are render-time DERIVATIONS with no field of their own (M0b) — proven
# here by checking the inputs the derivation reads are non-empty.
DOTPATH_CHECKS = {
    "identity.full_name": lambda p: bool(p.core.identity.full_name.strip()),
    "identity.aka": lambda p: p.core.identity.aka is not None,
    "identity.headline": lambda p: bool(p.core.identity.headline.strip()),
    "identity.contact": lambda p: bool(p.core.identity.contact.strip()),
    "identity.cv_filename_prefix": lambda p: bool(p.core.identity.cv_filename_prefix.strip()),
    "location.home_city": lambda p: bool(p.core.location.home_city.strip()),
    "location.home_city_aliases": lambda p: len(p.core.location.home_city_aliases) > 0,
    "languages.cv_languages": lambda p: len(p.core.languages.cv_languages) > 0,
    "languages.disqualify_required": lambda p: p.core.languages.disqualify_required is not None,
    "employers.protected": lambda p: len(p.core.employers.protected) > 0,
    "employers.flexible.name": lambda p: bool(p.core.employers.flexible.name.strip()),
    "employers.flexible.period": lambda p: bool(p.core.employers.flexible.period.strip()),
    "employers.flexible.projects": lambda p: len(p.core.employers.flexible.projects) > 0,
    # derived: real_companies = lowercase(protected + [flexible.name])
    "employers.real_companies": lambda p: (
        len(p.core.employers.protected) > 0 or bool(p.core.employers.flexible.name)
    ),
    # derived: profile_titles = normalized unique role titles
    "employers.profile_titles": lambda p: len(p.core.roles) > 0,
    # derived: history = roles projected to company/title/period/(+wave-2 fields)
    "employers.history": lambda p: (
        len(p.core.roles) > 0 and all(r.company and r.title and r.period for r in p.core.roles)
    ),
    "education.school_keyword": lambda p: bool(p.core.education.school_keyword.strip()),
    "education.expected_role_count": lambda p: p.core.education.expected_role_count > 0,
    "experience.years_label": lambda p: bool(p.core.experience.years_label.strip()),
    "experience.since_year": lambda p: p.core.experience.since_year > 0,
    # derived: tracks.base_cv = {track: f"base_cv_{track}.md" for track in variants}
    "tracks.base_cv": lambda p: len(p.variants) > 0,
    # derived: source_urls.*_location = lowercase(location.home_city)
    "source_urls.pracuj_location": lambda p: bool(p.core.location.home_city.strip()),
    "source_urls.theprotocol_location": lambda p: bool(p.core.location.home_city.strip()),
    "source_urls.jobleads_location": lambda p: bool(p.core.location.home_city.strip()),
}

# Sanity: this must be exactly the 24-dotpath set M0a's re-run found.
assert len(DOTPATH_CHECKS) == 24, f"expected 24 M0a dotpaths, have {len(DOTPATH_CHECKS)}"


def _load_example() -> Profile:
    data = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    return from_dict(data)


def test_example_file_is_valid_json():
    json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def test_example_loads_and_validates_clean():
    profile = _load_example()
    assert validate(profile) == []


def test_example_declares_schema_version_1():
    profile = _load_example()
    assert profile.schema_version == 1


def test_example_has_two_roles_and_one_variant():
    profile = _load_example()
    assert len(profile.core.roles) == 2
    assert set(profile.variants) == {"angular"}


def test_every_m0a_dotpath_has_a_render_source():
    profile = _load_example()
    failed = [dotpath for dotpath, check in DOTPATH_CHECKS.items() if not check(profile)]
    assert not failed, f"profile.example.json cannot feed these M0a dotpaths: {failed}"


def test_example_data_is_neutral_placeholder():
    """Never the project owner's real data — mirrors
    tests/test_handoff_readiness.py's forbidden-strings check."""
    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    for marker in ("Ihar", "Petrasheuski", "Fairmarkit", "Atruvia", "SolbegSoft", "Altoros"):
        assert marker not in text, f"owner data leaked into profile.example.json: {marker!r}"
