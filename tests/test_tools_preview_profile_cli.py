"""tests/test_tools_preview_profile_cli.py — subprocess tests for the
"test resume" preview CLI seam (docs/PROFILE_PAGE_TABS_WORKORDER.md, the
bot-repo work item).

Same real-subprocess convention as tests/test_tools_profile_cli.py (there is
no mocking boundary at the CLI level — this repo's precedent for a tools/
script is to run it for real). The one wrinkle vs. that file: this CLI's
happy path shells out a SECOND time to generate_docs.py, whose PDF step
needs a real LibreOffice binary that may not be present on every dev/CI box
(the render logic itself — track resolution, the generate_docs.py argv,
the "--no-tracker"/no-LLM env — is already covered against a controllable
fake in tests/test_profile_preview.py and tests/test_schedules_profile_jobs.py).
So the happy-path assertions here stay tolerant of a missing LibreOffice
(generate_docs.py itself only WARNS and keeps the .docx when the PDF
conversion step fails — see generate_docs.py's convert_all_to_pdf) and check
content.json's resolved content instead of insisting on a specific file
extension.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_PROFILE = PROJECT_DIR / "candidate" / "profile.example.json"

_CANDIDATE_YAML = """\
identity:
  full_name: Test Candidate
  contact: test@example.com
  cv_filename_prefix: Test_CV
"""


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, *args],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
        timeout=60,
    )


def _with_candidate_yaml(tmp_path: Path) -> dict[str, str]:
    candidate_yaml = tmp_path / "candidate.yaml"
    candidate_yaml.write_text(_CANDIDATE_YAML, encoding="utf-8")
    return {"CANDIDATE_YAML_PATH": str(candidate_yaml)}


class TestPreviewProfileCliHappyPath:
    def test_core_track_writes_content_json_and_at_least_one_document(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        env = _with_candidate_yaml(tmp_path)

        result = _run("tools/preview_profile.py", str(EXAMPLE_PROFILE), str(out_dir), env=env)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["written"], "expected at least one rendered document"

        content = json.loads((out_dir / "content.json").read_text(encoding="utf-8"))
        assert content["stack"] == "General"
        assert content["resume_en"]["summary"]
        assert content["output_folder"] == str(out_dir)

    def test_track_flag_selects_the_variant(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        env = _with_candidate_yaml(tmp_path)

        result = _run(
            "tools/preview_profile.py",
            str(EXAMPLE_PROFILE),
            str(out_dir),
            "--track",
            "angular",
            env=env,
        )

        assert result.returncode == 0, result.stderr
        content = json.loads((out_dir / "content.json").read_text(encoding="utf-8"))
        assert content["stack"] == "Angular"
        # candidate/profile.example.json's "angular" variant has its own summary.
        assert "Angular engineer" in content["resume_en"]["summary"]

    def test_no_tracker_flag_reaches_generate_docs(self, tmp_path: Path) -> None:
        """No direct way to observe the child argv from here — but a bug
        that dropped --no-tracker would attempt a real tracker write with no
        apply_url/company_name in content.json and generate_docs.py would
        exit non-zero (KeyError in record_successful_apply), so exit 0 here
        is itself evidence --no-tracker made it through."""
        out_dir = tmp_path / "out"
        env = _with_candidate_yaml(tmp_path)

        result = _run("tools/preview_profile.py", str(EXAMPLE_PROFILE), str(out_dir), env=env)

        assert result.returncode == 0, result.stderr


class TestPreviewProfileCliErrors:
    def test_missing_profile_exits_1(self, tmp_path: Path) -> None:
        result = _run(
            "tools/preview_profile.py", str(tmp_path / "missing.json"), str(tmp_path / "out")
        )

        assert result.returncode == 1
        assert result.stderr.strip()

    def test_malformed_json_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")

        result = _run("tools/preview_profile.py", str(bad), str(tmp_path / "out"))

        assert result.returncode == 1
        assert result.stderr.strip()

    def test_unsafe_track_exits_1(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        env = _with_candidate_yaml(tmp_path)

        result = _run(
            "tools/preview_profile.py",
            str(EXAMPLE_PROFILE),
            str(out_dir),
            "--track",
            "../escape",
            env=env,
        )

        assert result.returncode == 1
        assert result.stderr.strip().startswith("ERROR:")
        assert not out_dir.exists()

    def test_missing_candidate_identity_exits_1_not_half_rendered(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        # Point CANDIDATE_YAML_PATH at a file that doesn't exist so
        # generate_docs.py's identity gate fails deterministically,
        # regardless of the ambient dev checkout's own candidate/candidate.yaml.
        env = {"CANDIDATE_YAML_PATH": str(tmp_path / "nope.yaml")}

        result = _run("tools/preview_profile.py", str(EXAMPLE_PROFILE), str(out_dir), env=env)

        assert result.returncode == 1
        # Importing hunter.config along the way also logs its own unrelated
        # "candidate.yaml not found" warning (same footgun documented in
        # tests/test_tools_profile_cli.py's _run() docstring) — this CLI's
        # own error line still lands on stderr, just not necessarily first.
        assert "ERROR:" in result.stderr
        assert "Traceback" not in result.stderr
