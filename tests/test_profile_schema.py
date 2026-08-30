"""tests/test_profile_schema.py — hunter/profile_schema.py's dataclasses,
tolerant from_dict, to_dict round-trip, and validate().

docs/RESUME_PROFILE_STORE_PLAN.md M1. Nothing consumes this module yet — the
risk of this step is zero; these tests only pin the shape and the tolerance
contract (never raise on garbage input).
"""

from __future__ import annotations

import json
import pathlib

from hunter.profile_schema import (
    Bullet,
    Core,
    Employers,
    FlexibleEmployer,
    Identity,
    Profile,
    Role,
    SkillCategory,
    Variant,
    from_dict,
    to_dict,
    validate,
)


def _filled_profile() -> Profile:
    return Profile(
        schema_version=1,
        core=Core(
            identity=Identity(
                full_name="Jane Doe",
                aka="",
                headline="Senior Frontend Developer",
                contact="jane@example.com",
                cv_filename_prefix="Jane_Doe_CV",
            ),
            employers=Employers(
                protected=["Acme Corp"],
                flexible=FlexibleEmployer(
                    name="Beta Agency", period="2018-2020", projects=["E-commerce"]
                ),
            ),
            roles=[
                Role(
                    id="r1",
                    company="Acme Corp",
                    title="Senior Frontend Developer",
                    period="Jan 2024 - Present",
                    bullets=[Bullet(text="Built a dashboard.", tracks=["angular"])],
                    bullets_by_track={"react": ["Built a dashboard (React)."]},
                    title_by_track={"ai": "AI Tooling Engineer"},
                )
            ],
            skills=[SkillCategory(category="Core", items=["Angular", "TypeScript"])],
        ),
        variants={"angular": Variant(headline="Angular Developer")},
    )


class TestRoundTrip:
    def test_filled_profile_round_trips(self):
        profile = _filled_profile()
        rebuilt = from_dict(to_dict(profile))
        assert rebuilt == profile

    def test_empty_dict_produces_a_default_profile(self):
        profile = from_dict({})
        assert profile == Profile()
        assert profile.schema_version == 1

    def test_none_input_does_not_raise(self):
        profile = from_dict(None)  # type: ignore[arg-type]
        assert profile == Profile()


class TestTolerance:
    def test_unknown_top_level_key_is_ignored_not_raised(self, caplog):
        data = {"core": {"identity": {"full_name": "Jane"}}, "bogus_future_field": 123}
        profile = from_dict(data)
        assert profile.core.identity.full_name == "Jane"

    def test_unknown_nested_key_is_ignored(self):
        data = {"core": {"identity": {"full_name": "Jane", "shoe_size": 42}}}
        profile = from_dict(data)
        assert profile.core.identity.full_name == "Jane"

    def test_wrong_shaped_list_field_falls_back_to_default(self):
        data = {"core": {"location": {"home_city_aliases": "not-a-list"}}}
        profile = from_dict(data)
        assert profile.core.location.home_city_aliases == []

    def test_wrong_shaped_roles_field_falls_back_to_empty_list(self):
        data = {"core": {"roles": "not-a-list"}}
        profile = from_dict(data)
        assert profile.core.roles == []

    def test_bad_schema_version_falls_back_to_default(self):
        data = {"schema_version": "not-a-number"}
        profile = from_dict(data)
        assert profile.schema_version == 1

    def test_role_bullets_by_track_survives_round_trip(self):
        data = {
            "core": {
                "identity": {"full_name": "Jane"},
                "roles": [
                    {
                        "company": "Acme",
                        "bullets_by_track": {"react": ["one", "two"]},
                        "title_by_track": {"ai": "AI Engineer"},
                    }
                ],
            }
        }
        profile = from_dict(data)
        role = profile.core.roles[0]
        assert role.bullets_by_track == {"react": ["one", "two"]}
        assert role.title_by_track == {"ai": "AI Engineer"}

    def test_garbage_nested_dict_values_do_not_raise(self):
        data = {
            "core": {
                "roles": [{"bullets": "not-a-list", "bullets_by_track": "also-not-a-dict"}],
                "skills": [{"items": {"not": "a-list"}}],
            },
            "variants": {"angular": {"skills": "not-a-list"}},
            "leftovers": [{"text": 123}],
            "uploads": "not-a-list",
        }
        profile = from_dict(data)
        assert profile.core.roles[0].bullets == []
        assert profile.core.roles[0].bullets_by_track == {}
        assert profile.core.skills[0].items == []
        assert profile.variants["angular"].skills == []
        assert profile.leftovers[0].text == "123"
        assert profile.uploads == []


class TestRoleTracksAndVariantNotes:
    """docs/RESUME_PROFILE_STORE_PLAN.md M2, step 2d."""

    def test_role_tracks_and_variant_notes_round_trip(self):
        data = {
            "core": {
                "identity": {"full_name": "Jane"},
                "roles": [{"company": "Acme", "tracks": ["angular"]}],
            },
            "variants": {"angular": {"notes": "Lead with Angular achievements."}},
        }
        profile = from_dict(data)
        assert profile.core.roles[0].tracks == ["angular"]
        assert profile.variants["angular"].notes == "Lead with Angular achievements."
        assert from_dict(to_dict(profile)) == profile

    def test_role_tracks_defaults_to_empty_list(self):
        profile = from_dict({"core": {"roles": [{"company": "Acme"}]}})
        assert profile.core.roles[0].tracks == []

    def test_variant_notes_defaults_to_empty_string(self):
        profile = from_dict({"variants": {"angular": {}}})
        assert profile.variants["angular"].notes == ""

    def test_role_tracks_wrong_shape_falls_back_to_empty_list(self):
        data = {"core": {"roles": [{"company": "Acme", "tracks": "not-a-list"}]}}
        profile = from_dict(data)
        assert profile.core.roles[0].tracks == []

    def test_variant_notes_non_string_value_does_not_raise(self):
        """Matches _coerce_field's existing str-field contract (str(raw)) —
        garbage input is stringified, never raised on."""
        data = {"variants": {"angular": {"notes": {"not": "a-string"}}}}
        profile = from_dict(data)
        assert isinstance(profile.variants["angular"].notes, str)


class TestValidate:
    def test_empty_profile_reports_missing_identity(self):
        problems = validate(Profile())
        assert "core.identity.full_name is required" in problems
        assert "core.identity.contact is required" in problems
        assert "core.identity.cv_filename_prefix is required" in problems

    def test_filled_identity_passes(self):
        profile = _filled_profile()
        problems = validate(profile)
        assert problems == []

    def test_whitespace_only_name_still_flagged(self):
        profile = Profile(
            core=Core(identity=Identity(full_name="   ", contact="x", cv_filename_prefix="x"))
        )
        assert "core.identity.full_name is required" in validate(profile)


class TestExampleFile:
    """candidate/profile.example.json must parse without errors and validate cleanly."""

    def _load(self) -> dict:
        path = pathlib.Path(__file__).parent.parent / "candidate" / "profile.example.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_example_json_is_valid_json(self):
        data = self._load()
        assert isinstance(data, dict)

    def test_example_parses_without_raising(self):
        profile = from_dict(self._load())
        assert isinstance(profile, Profile)

    def test_example_validates_cleanly(self):
        profile = from_dict(self._load())
        assert validate(profile) == []

    def test_example_has_at_least_one_role(self):
        profile = from_dict(self._load())
        assert len(profile.core.roles) >= 1

    def test_example_covers_m0a_identity_dotpaths(self):
        profile = from_dict(self._load())
        identity = profile.core.identity
        assert identity.full_name
        assert identity.contact
        assert identity.cv_filename_prefix

    def test_example_covers_m0a_location_dotpaths(self):
        profile = from_dict(self._load())
        loc = profile.core.location
        assert loc.home_city
        assert loc.home_city_aliases

    def test_example_covers_m0a_employers_dotpaths(self):
        profile = from_dict(self._load())
        employers = profile.core.employers
        assert isinstance(employers.protected, list)
        assert employers.flexible.name
        assert employers.flexible.period
        assert isinstance(employers.flexible.projects, list)

    def test_example_covers_m0a_experience_dotpaths(self):
        profile = from_dict(self._load())
        exp = profile.core.experience
        assert exp.years_label
        assert exp.since_year > 0

    def test_example_covers_m0a_education_dotpaths(self):
        profile = from_dict(self._load())
        edu = profile.core.education
        assert edu.school_keyword
        assert edu.expected_role_count > 0

    def test_example_round_trips(self):
        data = self._load()
        profile = from_dict(data)
        assert from_dict(to_dict(profile)) == profile
