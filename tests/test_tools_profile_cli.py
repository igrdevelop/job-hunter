"""tests/test_tools_profile_cli.py — subprocess tests for the M4 CLI seam.

docs/RESUME_PROFILE_STORE_PLAN.md M4: tools/parse_resume.py and
tools/render_profile.py are thin argparse wrappers over hunter.profile_parse
/ hunter.profile_render. There is no existing precedent in this repo for
testing a tools/ CLI, so — per the executor prompt's own fallback — each
tool is run as a real subprocess against a fixture, exactly like the
site/API integration eventually will.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hunter import candidate

PROJECT_DIR = Path(__file__).resolve().parent.parent
JANE_TXT = PROJECT_DIR / "tests" / "fixtures" / "resumes" / "jane.txt"
EXAMPLE_PROFILE = PROJECT_DIR / "candidate" / "profile.example.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    # `errors="replace"`: a dev checkout with no real candidate/candidate.yaml
    # makes hunter.candidate log an em-dash-bearing warning at import time
    # (unrelated to this CLI), and Python's logging "handler of last resort"
    # writes it to stderr using the platform's legacy default encoding on
    # Windows — decoding that as strict UTF-8 crashes the reader thread. This
    # test only checks exit codes and JSON on stdout, so a lossy decode of an
    # unrelated stderr line is harmless.
    return subprocess.run(
        [sys.executable, *args],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


class TestParseResumeCli:
    def test_no_llm_prints_profile_json(self) -> None:
        result = _run("tools/parse_resume.py", str(JANE_TXT), "--no-llm")
        assert result.returncode == 0, result.stderr

        data = json.loads(result.stdout)
        assert "jane.doe@example.com" in data["core"]["identity"]["contact"]
        assert len(data["leftovers"]) == 1

    def test_upload_id_is_stamped_on_leftovers(self) -> None:
        result = _run("tools/parse_resume.py", str(JANE_TXT), "--no-llm", "--upload-id", "upload-1")
        assert result.returncode == 0, result.stderr

        data = json.loads(result.stdout)
        assert data["leftovers"][0]["source_upload_id"] == "upload-1"

    def test_unsupported_extension_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "resume.xyz"
        bad.write_text("whatever", encoding="utf-8")

        result = _run("tools/parse_resume.py", str(bad), "--no-llm")

        assert result.returncode == 1
        assert result.stderr.strip()
        assert result.stdout == ""

    def test_missing_file_exits_1(self, tmp_path: Path) -> None:
        result = _run("tools/parse_resume.py", str(tmp_path / "missing.pdf"), "--no-llm")

        assert result.returncode == 1
        assert result.stderr.strip()


class TestRenderProfileCli:
    def test_renders_example_profile(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"

        result = _run("tools/render_profile.py", str(EXAMPLE_PROFILE), str(out_dir))

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        written_names = {Path(p).name for p in payload["written"]}
        assert written_names == {"candidate.yaml", "candidate_profile.md", "base_cv_angular.md"}
        for name in written_names:
            assert (out_dir / name).exists()

    def test_missing_profile_exits_1(self, tmp_path: Path) -> None:
        result = _run(
            "tools/render_profile.py", str(tmp_path / "missing.json"), str(tmp_path / "out")
        )

        assert result.returncode == 1
        assert result.stderr.strip()

    def test_malformed_json_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")

        result = _run("tools/render_profile.py", str(bad), str(tmp_path / "out"))

        assert result.returncode == 1
        assert result.stderr.strip()


class TestParseThenRenderChain:
    """The exact manual chain from the executor prompt's Definition of Done:
    parse (--no-llm) -> render -> candidate.get() reads real profile data."""

    def test_no_llm_parse_then_render_produces_a_readable_candidate_yaml(
        self, tmp_path: Path
    ) -> None:
        parse_result = _run("tools/parse_resume.py", str(JANE_TXT), "--no-llm")
        assert parse_result.returncode == 0, parse_result.stderr
        profile_json = tmp_path / "p.json"
        profile_json.write_text(parse_result.stdout, encoding="utf-8")

        out_dir = tmp_path / "out"
        render_result = _run("tools/render_profile.py", str(profile_json), str(out_dir))
        assert render_result.returncode == 0, render_result.stderr

        candidate_yaml = out_dir / "candidate.yaml"
        assert candidate_yaml.exists()
        contact = candidate.get("identity.contact", "", path=candidate_yaml)
        assert "jane.doe@example.com" in contact
