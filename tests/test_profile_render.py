"""tests/test_profile_render.py — hunter/profile_render.py::render_candidate_yaml.

docs/RESUME_PROFILE_STORE_PLAN.md M2, step 2a. Proves the rendered
candidate.yaml (a) resolves every M0a dotpath, (b) passes
candidate.require_identity(), (c) is deterministic, and (d) stays compatible
with the wave-2 employment-facts renderer in hunter/gen_prompt.py — a
regression here would silently degrade the generation prompt to its generic
no-history fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from hunter import candidate, gen_prompt
from hunter.profile_render import render_candidate_yaml, render_profile_md
from hunter.profile_schema import from_dict

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "candidate" / "profile.example.json"
GOLDEN_PROFILE_MD_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "profile_render" / "candidate_profile.golden.md"
)

# Same 24 M0a dotpaths as tests/test_profile_example.py, now checked against
# the actual RENDERED candidate.yaml (loaded back with yaml.safe_load)
# instead of the Profile object.
M0A_DOTPATHS = [
    "identity.full_name",
    "identity.aka",
    "identity.headline",
    "identity.contact",
    "identity.cv_filename_prefix",
    "location.home_city",
    "location.home_city_aliases",
    "languages.cv_languages",
    "languages.disqualify_required",
    "employers.protected",
    "employers.flexible.name",
    "employers.flexible.period",
    "employers.flexible.projects",
    "employers.real_companies",
    "employers.profile_titles",
    "employers.history",
    "education.school_keyword",
    "education.expected_role_count",
    "experience.years_label",
    "experience.since_year",
    "tracks.base_cv",
    "source_urls.pracuj_location",
    "source_urls.theprotocol_location",
    "source_urls.jobleads_location",
]


def _resolves(node: dict, dotpath: str) -> bool:
    """Same shape as tests/test_handoff_readiness.py's own `resolves()` — key
    presence only. `identity.aka` is legitimately an empty string by design
    (an optional CV subtitle), so this must not also require a truthy value."""
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _load_example_profile():
    data = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    return from_dict(data)


class TestExampleRoundTrip:
    def test_every_m0a_dotpath_resolves_in_rendered_yaml(self):
        profile = _load_example_profile()
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        missing = [dp for dp in M0A_DOTPATHS if not _resolves(rendered, dp)]
        assert not missing, f"rendered candidate.yaml does not resolve: {missing}"

    def test_rendered_yaml_passes_require_identity(self, tmp_path):
        profile = _load_example_profile()
        out_path = tmp_path / "candidate.yaml"
        out_path.write_text(render_candidate_yaml(profile), encoding="utf-8")
        # Must not raise.
        candidate.require_identity(path=out_path)

    def test_render_is_deterministic(self):
        profile = _load_example_profile()
        first = render_candidate_yaml(profile)
        second = render_candidate_yaml(profile)
        assert first == second

    def test_render_is_valid_yaml(self):
        profile = _load_example_profile()
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        assert isinstance(rendered, dict)


class TestDerivedFields:
    def test_real_companies_is_lowercase_of_protected_plus_flexible(self):
        profile = _load_example_profile()
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        protected = profile.core.employers.protected
        flexible_name = profile.core.employers.flexible.name
        expected = {c.lower() for c in [*protected, flexible_name] if c}
        assert set(rendered["employers"]["real_companies"]) == expected

    def test_profile_titles_is_normalized_unique_role_titles(self):
        profile = _load_example_profile()
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        expected = {r.title.strip().lower() for r in profile.core.roles if r.title.strip()}
        assert set(rendered["employers"]["profile_titles"]) == expected

    def test_history_projects_roles_without_description_or_bullets(self):
        profile = _load_example_profile()
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        history = rendered["employers"]["history"]
        assert len(history) == len(profile.core.roles)
        for entry in history:
            assert "description" not in entry
            assert "bullets" not in entry
            assert "bullets_by_track" not in entry
        # role-acme in the example has title_by_track — verify it survives.
        acme_entry = next(e for e in history if e["company"] == "Acme Corp")
        assert acme_entry["title_by_track"] == {"ai": "AI Tooling Engineer"}

    def test_tracks_base_cv_is_derived_from_variant_keys(self):
        profile = _load_example_profile()
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        assert rendered["tracks"]["base_cv"] == {
            track: f"base_cv_{track}.md" for track in profile.variants
        }

    def test_protected_defaults_to_role_companies_minus_flexible(self):
        """When core.employers.protected is empty, the renderer falls back to
        every role's company except the flexible employer (M0b)."""
        data = {
            "core": {
                "identity": {"full_name": "X", "contact": "y", "cv_filename_prefix": "z"},
                "employers": {"flexible": {"name": "Flexi Co"}},
                "roles": [
                    {"company": "Acme", "title": "Dev", "period": "2020"},
                    {"company": "Flexi Co", "title": "Dev", "period": "2019"},
                ],
            }
        }
        profile = from_dict(data)
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        assert rendered["employers"]["protected"] == ["Acme"]

    def test_explicit_protected_is_not_overridden(self):
        data = {
            "core": {
                "identity": {"full_name": "X", "contact": "y", "cv_filename_prefix": "z"},
                "employers": {"protected": ["Explicit Co"]},
                "roles": [{"company": "Acme", "title": "Dev", "period": "2020"}],
            }
        }
        profile = from_dict(data)
        rendered = yaml.safe_load(render_candidate_yaml(profile))
        assert rendered["employers"]["protected"] == ["Explicit Co"]


class TestWave2Compatibility:
    """A rendered candidate.yaml must feed hunter/gen_prompt.py's employment-
    facts renderer a REAL facts table, not its no-history degrade paragraph."""

    def test_rendered_yaml_produces_real_facts_table(self, tmp_path):
        profile = _load_example_profile()
        out_path = tmp_path / "candidate.yaml"
        out_path.write_text(render_candidate_yaml(profile), encoding="utf-8")

        candidate._set_path(out_path)
        try:
            facts = gen_prompt.render_employment_facts()
        finally:
            candidate._set_path(None)

        assert "No fixed employment history is configured" not in facts
        assert "Acme Corp" in facts
        assert "Beta Solutions" in facts

    def test_rendered_yaml_produces_real_ground_truth(self, tmp_path):
        profile = _load_example_profile()
        out_path = tmp_path / "candidate.yaml"
        out_path.write_text(render_candidate_yaml(profile), encoding="utf-8")

        candidate._set_path(out_path)
        try:
            ground_truth = gen_prompt.render_ground_truth()
        finally:
            candidate._set_path(None)

        assert "No fixed employer list is configured" not in ground_truth
        assert "Acme Corp" in ground_truth


class TestRenderProfileMd:
    """docs/RESUME_PROFILE_STORE_PLAN.md M2, step 2b."""

    def test_matches_golden_snapshot(self):
        profile = _load_example_profile()
        rendered = render_profile_md(profile)
        expected = GOLDEN_PROFILE_MD_PATH.read_text(encoding="utf-8")
        assert rendered == expected

    def test_empty_profile_renders_without_raising(self):
        from hunter.profile_schema import Profile

        rendered = render_profile_md(Profile())
        assert isinstance(rendered, str)
        assert rendered.startswith("## Candidate Profile")
        # No roles/education/languages/extras — only the bare heading survives.
        assert "Work Experience" not in rendered
        assert "Education" not in rendered

    def test_role_bullets_are_never_track_filtered(self):
        """candidate_profile.md is the narrative superset — a bullet tagged
        for only one track must still appear here (base_cv is where track
        filtering happens, step 2c)."""
        profile = _load_example_profile()
        rendered = render_profile_md(profile)
        assert "Introduced NgRx state management" in rendered  # tagged ["angular"]

    def test_render_is_deterministic(self):
        profile = _load_example_profile()
        assert render_profile_md(profile) == render_profile_md(profile)
