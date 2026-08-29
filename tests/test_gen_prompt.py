"""tests/test_gen_prompt.py — golden tests for hunter/gen_prompt.py.

docs/GENERATION_ARCHITECTURE_ANALYSIS.md §6 wave 2: prompts/generation_rules.md
and prompts/judge_rules.md must be candidate-agnostic, with the active
candidate's own employment facts rendered in at call time from
candidate.yaml. These tests prove three things:

1. Given a FICTIONAL candidate.yaml, the assembled prompt matches a checked-in
   golden fixture byte-for-byte — proves the renderer is deterministic and
   its output is exactly what a reviewer already read and approved.
2. None of the project owner's real employers/identity ever appear in the
   output for a second user's data (the "second user" smoke test the PR
   description asks for).
3. A candidate.yaml with no employers.history/experience.years_label (or no
   file at all) still produces a working prompt via the generic fallback
   text, instead of raising or producing broken/empty instructions.
"""

from __future__ import annotations

from pathlib import Path

from hunter import candidate, gen_prompt

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gen_prompt"
FIXTURE_YAML = FIXTURES_DIR / "candidate.yaml"

# Real owner data hunter/gen_prompt.py must never leak for a second user's
# candidate.yaml — mirrors tests/test_handoff_readiness.py's FORBIDDEN list.
OWNER_MARKERS = (
    "Ihar",
    "Petrasheuski",
    "Fairmarkit",
    "Venture Labs",
    "SolbegSoft",
    "Altoros",
    "Staronka",
    "Alten Poland",
    "Atruvia",
    "belarusian state technological",
)


def test_generation_prompt_matches_golden_fixture():
    candidate._set_path(FIXTURE_YAML)
    try:
        rendered = gen_prompt.build_generation_prompt(cand_dir=FIXTURES_DIR)
    finally:
        candidate._set_path(None)

    expected = (FIXTURES_DIR / "expected_generation_prompt.md").read_text(encoding="utf-8")
    assert rendered == expected


def test_judge_prompt_matches_golden_fixture():
    candidate._set_path(FIXTURE_YAML)
    try:
        rendered = gen_prompt.build_judge_prompt()
    finally:
        candidate._set_path(None)

    expected = (FIXTURES_DIR / "expected_judge_prompt.md").read_text(encoding="utf-8")
    assert rendered == expected


def test_second_user_prompt_carries_no_owner_data():
    """Smoke test: a fictional candidate.yaml must never leak the project
    owner's real employers/identity into the assembled prompt."""
    candidate._set_path(FIXTURE_YAML)
    try:
        generation = gen_prompt.build_generation_prompt(cand_dir=FIXTURES_DIR)
        judge = gen_prompt.build_judge_prompt()
    finally:
        candidate._set_path(None)

    for marker in OWNER_MARKERS:
        assert marker not in generation, f"owner data leaked into generation prompt: {marker!r}"
        assert marker not in judge, f"owner data leaked into judge prompt: {marker!r}"

    # And the fixture's OWN fictional data must actually be present — proves
    # the assertion above isn't vacuously true because rendering failed.
    assert "Acme Corp" in generation
    assert "Acme Corp" in judge


def test_generation_prompt_without_candidate_yaml_uses_fallback(tmp_path):
    """No candidate.yaml at all (a bare checkout) must still produce a
    working prompt — via the generic fallback paragraph — not raise."""
    candidate._set_path(tmp_path / "does-not-exist.yaml")
    try:
        rendered = gen_prompt.build_generation_prompt(cand_dir=tmp_path)
    finally:
        candidate._set_path(None)

    assert "No fixed employment history is configured" in rendered
    for marker in OWNER_MARKERS:
        assert marker not in rendered


def test_judge_prompt_without_candidate_yaml_uses_fallback(tmp_path):
    candidate._set_path(tmp_path / "does-not-exist.yaml")
    try:
        rendered = gen_prompt.build_judge_prompt()
    finally:
        candidate._set_path(None)

    assert "No fixed employer list is configured" in rendered


def test_local_tail_is_appended_when_present(tmp_path):
    """An optional {cand_dir}/generation_rules.local.md is appended verbatim
    after the rendered prompt (candidate/README.md's documented mechanism
    for free-text narrative that doesn't fit candidate.yaml's structure)."""
    (tmp_path / gen_prompt.LOCAL_TAIL_FILENAME).write_text(
        "**Story bank**: my own narrative goes here.\n", encoding="utf-8"
    )
    candidate._set_path(FIXTURE_YAML)
    try:
        rendered = gen_prompt.build_generation_prompt(cand_dir=tmp_path)
    finally:
        candidate._set_path(None)

    assert rendered.rstrip().endswith("my own narrative goes here.")


def test_local_tail_absent_by_default(tmp_path):
    candidate._set_path(FIXTURE_YAML)
    try:
        rendered = gen_prompt.build_generation_prompt(cand_dir=tmp_path)
    finally:
        candidate._set_path(None)

    assert "Story bank" not in rendered


def test_base_cv_map_includes_defaults_and_override(tmp_path):
    override = tmp_path / "candidate.yaml"
    override.write_text(
        "tracks:\n  base_cv:\n    angular: custom_angular.md\n",
        encoding="utf-8",
    )
    candidate._set_path(override)
    try:
        mapping = gen_prompt.base_cv_files()
    finally:
        candidate._set_path(None)

    assert mapping["angular"] == "custom_angular.md"
    assert mapping["react"] == "base_cv_react.md"  # untouched default survives


def test_cli_main_generation_prints_full_prompt(capsys):
    candidate._set_path(FIXTURE_YAML)
    try:
        exit_code = gen_prompt.main([])
    finally:
        candidate._set_path(None)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Candidate Employment Facts" in out


def test_cli_main_base_cv_map_prints_lines(capsys):
    candidate._set_path(FIXTURE_YAML)
    try:
        exit_code = gen_prompt.main(["base-cv-map"])
    finally:
        candidate._set_path(None)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "angular=base_cv_angular.md" in out


def test_cli_main_unknown_subcommand_errors(capsys):
    exit_code = gen_prompt.main(["bogus"])
    assert exit_code == 1
