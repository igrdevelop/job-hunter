"""tests/test_profile_preview.py — hunter/profile_preview.py.

docs/PROFILE_PAGE_TABS_WORKORDER.md, the bot-repo work item: a deterministic,
$0, no-LLM "test resume" preview built straight from a structured Profile
document. Covers content assembly (track resolution mirroring
hunter.profile_render's own contract, plus the 'core' special case), track
validation, and the generate_docs.py subprocess boundary (faked exactly like
tests/test_golden_apply_e2e.py's own FakeGenerateDocsRunner, since spawning a
real subprocess + LibreOffice is slow/environment-dependent and already
covered by generate_docs.py's own tests).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hunter.profile_preview import (
    CORE_TRACK,
    PreviewError,
    build_preview_content,
    render_preview,
    validate_track,
)
from hunter.profile_schema import from_dict

# A compact, hand-built profile exercising every track-resolution rule this
# module cares about: a wholesale bullets_by_track override on one role, a
# role hidden from other tracks via Role.tracks, a variant-level skills
# override, and a skill category label that does/doesn't match one of the
# four fixed document sections.
_PROFILE_DATA = {
    "schema_version": 1,
    "core": {
        "identity": {
            "full_name": "Test Candidate",
            "contact": "test@example.com",
            "cv_filename_prefix": "Test_CV",
        },
        "summary": "Core summary.",
        "roles": [
            {
                "id": "role-1",
                "company": "Acme",
                "title": "Senior Developer",
                "period": "2020 - Present",
                "subtitle": "Core subtitle",
                "bullets": [
                    {"text": "Untagged bullet.", "tracks": []},
                    {"text": "Angular-tagged bullet.", "tracks": ["angular"]},
                ],
                "bullets_by_track": {
                    "react": ["React rewrite bullet one.", "React rewrite bullet two."]
                },
                "title_by_track": {"react": "Senior Developer (React)"},
                "tracks": [],
            },
            {
                "id": "role-2",
                "company": "Beta",
                "title": "Frontend Developer",
                "period": "2018 - 2020",
                "bullets": [{"text": "Beta bullet.", "tracks": []}],
                "tracks": ["angular"],
            },
        ],
        "skills": [
            {"category": "Frontend", "items": ["Angular", "TypeScript"], "tracks": []},
            {"category": "Core Stack", "items": ["Should not appear"], "tracks": []},
        ],
        "education": {"entries": [{"text": "Example University", "origin": "parsed"}]},
    },
    "variants": {
        "angular": {"summary": "Angular-flavored summary."},
        "react": {"skills": [{"category": "Frontend", "items": ["React", "Redux"], "tracks": []}]},
    },
}


def _profile():
    return from_dict(_PROFILE_DATA)


class TestValidateTrack:
    @pytest.mark.parametrize("track", ["core", "angular", "react", "fullstack_x", "a1"])
    def test_accepts_safe_slugs(self, track):
        assert validate_track(track) == track

    @pytest.mark.parametrize(
        "track", ["", "../etc", "a/b", "a b", "Core", "CORE", "a..b", "a\\b", " ", None]
    )
    def test_rejects_unsafe_strings(self, track):
        with pytest.raises(PreviewError):
            validate_track(track)


class TestBuildPreviewContentCore:
    def test_core_includes_every_role_regardless_of_tracks(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        companies = [job["company"] for job in content["resume_en"]["experience"]]
        assert companies == ["Acme", "Beta"]  # role-2 (tracks=["angular"]) still visible

    def test_core_includes_the_full_bullet_superset_untagged_and_tagged(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        acme = content["resume_en"]["experience"][0]
        assert acme["bullets"] == ["Untagged bullet.", "Angular-tagged bullet."]

    def test_core_uses_core_summary_and_base_role_title(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        assert content["resume_en"]["summary"] == "Core summary."
        assert content["resume_en"]["experience"][0]["title"] == "Senior Developer"

    def test_core_stack_label_is_general(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        assert content["stack"] == "General"


class TestBuildPreviewContentTrack:
    def test_bullets_by_track_wins_wholesale_over_tag_filtering(self):
        """M0b: a per-track bullets_by_track entry REPLACES the rendered
        bullet list, it does not add to the tag-filtered subset."""
        content = build_preview_content(_profile(), "react")
        acme = content["resume_en"]["experience"][0]
        assert acme["bullets"] == ["React rewrite bullet one.", "React rewrite bullet two."]

    def test_title_by_track_overrides_base_title(self):
        content = build_preview_content(_profile(), "react")
        acme = content["resume_en"]["experience"][0]
        assert acme["title"] == "Senior Developer (React)"

    def test_role_tracks_hides_role_from_other_tracks(self):
        content = build_preview_content(_profile(), "react")
        companies = [job["company"] for job in content["resume_en"]["experience"]]
        assert companies == ["Acme"]  # role-2 is angular-only

    def test_role_tracks_keeps_role_visible_on_its_own_track(self):
        content = build_preview_content(_profile(), "angular")
        companies = [job["company"] for job in content["resume_en"]["experience"]]
        assert companies == ["Acme", "Beta"]

    def test_angular_track_without_bullets_by_track_falls_back_to_tag_filter(self):
        """role-1 has no 'angular' key in bullets_by_track, so the angular
        base CV falls back to filtering the plain `bullets` list by tag —
        the untagged bullet stays (shared), the angular-tagged one stays too."""
        content = build_preview_content(_profile(), "angular")
        acme = content["resume_en"]["experience"][0]
        assert acme["bullets"] == ["Untagged bullet.", "Angular-tagged bullet."]

    def test_variant_summary_overrides_core_summary(self):
        content = build_preview_content(_profile(), "angular")
        assert content["resume_en"]["summary"] == "Angular-flavored summary."

    def test_track_without_a_variant_falls_back_to_core_summary(self):
        content = build_preview_content(_profile(), "fullstack_x")
        assert content["resume_en"]["summary"] == "Core summary."

    def test_stack_label_capitalizes_the_track(self):
        content = build_preview_content(_profile(), "angular")
        assert content["stack"] == "Angular"


class TestBuildPreviewContentSkills:
    def test_matches_category_by_case_insensitive_label(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        assert content["resume_en"]["skills"]["frontend"] == "Angular, TypeScript"

    def test_unmatched_category_label_is_dropped_not_crashed(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        assert "Should not appear" not in json.dumps(content["resume_en"]["skills"])

    def test_variant_skills_override_core_wholesale(self):
        content = build_preview_content(_profile(), "react")
        assert content["resume_en"]["skills"]["frontend"] == "React, Redux"

    def test_experience_entries_always_have_required_keys(self):
        content = build_preview_content(_profile(), CORE_TRACK)
        for job in content["resume_en"]["experience"]:
            assert {"title", "company", "period"}.issubset(job)


class TestBuildPreviewContentEmptyProfile:
    def test_blank_profile_produces_empty_but_valid_shape(self):
        content = build_preview_content(from_dict({}), CORE_TRACK)
        assert content["resume_en"]["summary"] == ""
        assert content["resume_en"]["skills"] == {}
        assert content["resume_en"]["experience"] == []


# ── render_preview: generate_docs.py subprocess boundary ────────────────────


def _fake_generate_docs_run(*, exit_code: int = 0, extra_files: tuple[str, ...] = ()):
    """Stand-in for `subprocess.run([python, generate_docs.py, content.json,
    "--no-tracker"])` — mirrors tests/test_golden_apply_e2e.py's
    FakeGenerateDocsRunner without spawning a real subprocess or LibreOffice."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        content_json_path = Path(cmd[2])
        content = json.loads(content_json_path.read_text(encoding="utf-8"))
        output_folder = Path(content["output_folder"])
        output_folder.mkdir(parents=True, exist_ok=True)
        if exit_code == 0:
            (output_folder / "Test_CV_EN.pdf").write_bytes(b"%PDF-1.4 fake")
            for name in extra_files:
                (output_folder / name).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, exit_code, stdout="", stderr="boom" if exit_code else ""
        )

    _run.calls = calls
    return _run


class TestRenderPreview:
    def test_writes_content_json_and_returns_pdf_first(self, tmp_path, monkeypatch):
        fake = _fake_generate_docs_run(extra_files=("Test_CV_EN.docx",))
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        out_dir = tmp_path / "preview" / "core" / "20260101T000000Z"
        written = render_preview(_profile(), CORE_TRACK, out_dir)

        assert (out_dir / "content.json").exists()
        assert written[0].suffix == ".pdf"
        assert all(p.name != "content.json" for p in written)

    def test_output_folder_is_set_on_content_json(self, tmp_path, monkeypatch):
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        out_dir = tmp_path / "preview" / "core" / "run1"
        render_preview(_profile(), CORE_TRACK, out_dir)

        content = json.loads((out_dir / "content.json").read_text(encoding="utf-8"))
        assert content["output_folder"] == str(out_dir)

    def test_no_tracker_flag_and_no_llm_env_are_passed(self, tmp_path, monkeypatch):
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        render_preview(_profile(), CORE_TRACK, tmp_path / "out")

        cmd = fake.calls[0]
        assert "--no-tracker" in cmd
        assert "--force" not in cmd
        assert "--full" not in cmd

    def test_disables_llm_about_me_via_env(self, tmp_path, monkeypatch):
        captured_env = {}

        def _run(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            content = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
            Path(content["output_folder"]).mkdir(parents=True, exist_ok=True)
            (Path(content["output_folder"]) / "x.pdf").write_bytes(b"%PDF")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("hunter.profile_preview.subprocess.run", _run)
        render_preview(_profile(), CORE_TRACK, tmp_path / "out")

        assert captured_env.get("GENERATE_ABOUT_ME_PL") == "false"

    def test_subprocess_failure_raises_preview_error(self, tmp_path, monkeypatch):
        fake = _fake_generate_docs_run(exit_code=1)
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        with pytest.raises(PreviewError):
            render_preview(_profile(), CORE_TRACK, tmp_path / "out")

    def test_unsafe_track_raises_before_any_write(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "hunter.profile_preview.subprocess.run", lambda *a, **k: calls.append(1)
        )
        out_dir = tmp_path / "preview" / "bad" / "run1"

        with pytest.raises(PreviewError):
            render_preview(_profile(), "../escape", out_dir)

        assert calls == []
        assert not out_dir.exists()

    def test_extra_env_overlays_subprocess_environment(self, tmp_path, monkeypatch):
        captured_env = {}

        def _run(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            content = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
            Path(content["output_folder"]).mkdir(parents=True, exist_ok=True)
            (Path(content["output_folder"]) / "x.pdf").write_bytes(b"%PDF")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("hunter.profile_preview.subprocess.run", _run)
        render_preview(
            _profile(), CORE_TRACK, tmp_path / "out", extra_env={"CANDIDATE_YAML_PATH": "/x/y.yaml"}
        )

        assert captured_env.get("CANDIDATE_YAML_PATH") == "/x/y.yaml"
