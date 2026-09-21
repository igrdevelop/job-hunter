"""Tests for hunter/apply_cli.py's CLI invocation policy.

docs/improvement-2026-09/05-SECURITY_PLAN.md M1: the CLI apply agent used to
run `claude -p --dangerously-skip-permissions /apply <input>` -- full tool
access, no confirmation, for an agent fed scraped job-posting text. These
tests pin the replacement: an explicit --allowedTools/--disallowedTools
policy by default, with an env-gated escape hatch (APPLY_CLI_LEGACY_PERMS)
back to the old flag for one release if the allowlist turns out to be
missing something the skill needs.

See tests/test_golden_apply_cli_e2e.py::TestPostingTextNeverRidesInlineInCliArgv
for the end-to-end version (posting text staged to a file, never inlined).
"""

from __future__ import annotations

from pathlib import Path

from hunter.apply_cli import (
    _CLI_ALLOWED_TOOLS,
    _CLI_DISALLOWED_TOOLS,
    _build_cli_command,
    _posting_file_prompt_block,
    _write_staging_posting,
)


class TestBuildCliCommandDefault:
    def test_no_dangerously_skip_permissions_flag(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", False)
        cmd = _build_cli_command("some prompt")
        assert not any("dangerously" in str(part) for part in cmd)

    def test_carries_disallowed_tools_with_webfetch_and_websearch(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", False)
        cmd = _build_cli_command("some prompt")
        assert "--disallowedTools" in cmd
        disallowed = cmd[cmd.index("--disallowedTools") + 1]
        assert "WebFetch" in disallowed
        assert "WebSearch" in disallowed

    def test_carries_an_explicit_allowed_tools_list(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", False)
        cmd = _build_cli_command("some prompt")
        assert "--allowedTools" in cmd
        allowed = cmd[cmd.index("--allowedTools") + 1]
        assert "Read" in allowed
        assert "Write" in allowed
        assert "WebFetch" not in allowed
        assert "WebSearch" not in allowed

    def test_prompt_precedes_the_variadic_tool_flags(self, monkeypatch) -> None:
        # --allowedTools / --disallowedTools are variadic in the claude CLI:
        # a positional after them is swallowed as more tool rules. The prompt
        # used to be the final argv element, which made claude read every word
        # of it as a deny rule and exit 1 with no prompt (prod, 2026-09-21).
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", False)
        cmd = _build_cli_command("URL: https://example.com/job\n\nJob posting file: /tmp/x.txt")
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        assert cmd[2] == "/apply URL: https://example.com/job\n\nJob posting file: /tmp/x.txt"
        first_flag = min(cmd.index("--allowedTools"), cmd.index("--disallowedTools"))
        # Nothing but the two flag values may follow the variadic flags.
        assert cmd[first_flag:] == [
            "--allowedTools",
            _CLI_ALLOWED_TOOLS,
            "--disallowedTools",
            _CLI_DISALLOWED_TOOLS,
        ]

    def test_module_constants_are_consistent_with_the_built_command(self, monkeypatch) -> None:
        # Guards against the flags and the module-level policy strings drifting
        # apart if one is edited without the other.
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", False)
        cmd = _build_cli_command("x")
        assert cmd[cmd.index("--allowedTools") + 1] == _CLI_ALLOWED_TOOLS
        assert cmd[cmd.index("--disallowedTools") + 1] == _CLI_DISALLOWED_TOOLS


class TestBuildCliCommandLegacyEscapeHatch:
    def test_legacy_perms_restores_the_old_flag(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLY_CLI_LEGACY_PERMS", True)
        cmd = _build_cli_command("some prompt")
        assert "--dangerously-skip-permissions" in cmd
        assert "--allowedTools" not in cmd
        assert "--disallowedTools" not in cmd
        assert cmd == ["claude", "-p", "--dangerously-skip-permissions", "/apply some prompt"]


class TestWriteStagingPosting:
    def test_writes_the_full_text_to_a_file_under_applications_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)
        text = "Job Title: Senior Angular Developer\n\nLots of posting text here."

        path = _write_staging_posting(text)

        assert path.exists()
        assert path.read_text(encoding="utf-8") == text
        assert path.parent == tmp_path / ".cli_staging"

    def test_each_call_gets_its_own_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", tmp_path)

        first = _write_staging_posting("posting one")
        second = _write_staging_posting("posting two")

        assert first != second
        assert first.read_text(encoding="utf-8") == "posting one"
        assert second.read_text(encoding="utf-8") == "posting two"


class TestPostingFilePromptBlock:
    def test_frames_the_file_as_data_not_instructions(self) -> None:
        posting_path = Path("/app/Applications/.cli_staging/posting_x.txt")
        block = _posting_file_prompt_block(posting_path)
        assert str(posting_path) in block
        assert "Job posting file:" in block
        assert "DATA" in block
        assert "never instructions" in block.lower()
