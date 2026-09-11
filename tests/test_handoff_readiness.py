"""Handoff readiness — keep one person's personal data out of shared code, and
keep the handoff docs (.env.example, candidate.yaml.example) truthful.

Checks in this file: (a) no personal data as a literal in production code
(the bulk of the file); (b) a short hand-picked list of .env.example vars a
new user is explicitly told to set (docs/SETUP_NEW_USER.md); (c) every
`candidate.get()` dotpath used in code is documented in
candidate/candidate.yaml.example; (d) EVERY env var the code reads anywhere
(not just the (b) shortlist) is documented in .env.example — see
tools/list_env_vars.py and docs/improvement-2026-09/06-OPS_PLAN.md M6.

Why this file exists
--------------------
The repo-readiness audit was run three times over three weeks and found new
owner-specific defaults every time. It was not the same defect resurfacing:
`hunter/candidate.py`'s documented pattern is "the fallback reproduces the
original hardcoded behavior", so every feature touching identity, location or
employment history LEGITIMATELY added one more line carrying the owner's real
data. A manual audit cannot win against a source that keeps producing — so
the audit is a test now. A new owner default fails CI the day it is written
instead of a week later during a hand-over review.

What counts as a violation: the owner's name, phone, email, LinkedIn handle,
or a real employer/university name appearing as a literal in production code.
Personal data belongs in candidate/candidate.yaml (gitignored) and reaches the
code only through `candidate.get(dotpath, <neutral default>)`.

Scope note: docs/ and tests/ are excluded. docs/AGENT_LOG.md is a historical
record that legitimately quotes past incidents, and this very file has to name
the strings it forbids. `prompts/*.md` and `.claude/commands/*.md` ARE in
scope for the personal-data check below (unlike the rest of `.claude/`, which
is agent tooling): both are the live LLM prompt for the generation pipeline
(`.claude/commands/apply.md` is what `claude -p` actually runs — see CLAUDE.md
"Repository Layout"), so personal data there reaches the LLM and the generated
documents exactly like personal data in `hunter/*.py` would.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# tools/list_env_vars.py is the env-var collector shared by check (d) below and
# the standalone `python tools/list_env_vars.py` audit — tools/ isn't a package,
# so load it the same way tests/test_dedup_sheet.py loads tools/dedup_sheet.py.
_list_env_vars_spec = importlib.util.spec_from_file_location(
    "list_env_vars", PROJECT_ROOT / "tools" / "list_env_vars.py"
)
list_env_vars = importlib.util.module_from_spec(_list_env_vars_spec)
_list_env_vars_spec.loader.exec_module(list_env_vars)

# Production code only: the packages that ship in the Docker image and run
# against real vacancies. tools/ is developer-local, both out of scope.
SCANNED_ROOTS = ("hunter",)
SCANNED_FILES = ("generate_docs.py", "apply_agent.py", "llm_client.py", "hunter.py")

# The live prompt sources read by both apply pipelines / the CLI skill.
SCANNED_MD_GLOBS = ("prompts/*.md", ".claude/commands/*.md")

# docs/GENERATION_ARCHITECTURE_ANALYSIS.md §5.2-§5.3 found personal data in
# prompts/generation_rules.md and prompts/judge_rules.md (a 7-employer table
# with exact periods, per-employer backend rules, university, course list,
# real client names) — the readiness test above never saw them because it
# scanned only *.py. Wave 2 of the analysis document's §6 renders the
# personal facts into both prompts from candidate.yaml at runtime
# (hunter/gen_prompt.py), the pattern hunter/verdict_refine.py:60-67 already
# used for its own smaller prompt blocks — so both tracked files are now
# clean and no allowlist is needed. See
# test_legacy_allowlist_does_not_hide_new_offender below for a regression
# test of the allowlist MECHANISM itself, kept in case a future prompt file
# needs one again.

# Patterns that must never appear as literals in production code. Each entry is
# (label, compiled regex). Kept deliberately narrow — this test must not fire on
# unrelated words (e.g. "intel" inside "intelligent", handled by \b anchors and
# the more specific multi-word forms).
FORBIDDEN = [
    ("owner full name", re.compile(r"\bIhar\b|\bPetrasheuski\b|\bPietraszewski\b", re.I)),
    ("owner phone", re.compile(r"\+48\s?571\s?525\s?110")),
    ("owner email", re.compile(r"igrflex@|igr\.develop@")),
    ("owner linkedin handle", re.compile(r"linkedin\.com/in/ijerweb")),
    (
        "owner employer",
        re.compile(
            r"\bAtruvia\b|\bFairmarkit\b|\bSolbegSoft\b|\bAltoros\b|\bStaronka\b"
            r"|\bAlten Poland\b|\bVenture Labs\b",
            re.I,
        ),
    ),
    ("owner university", re.compile(r"belarusian state technological", re.I)),
    ("VPS address", re.compile(r"\b178\.105\.131\.107\b")),
]


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        files.extend(sorted((PROJECT_ROOT / root).rglob("*.py")))
    for name in SCANNED_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            files.append(path)
    return [f for f in files if "__pycache__" not in f.parts]


def _prompt_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCANNED_MD_GLOBS:
        files.extend(sorted(PROJECT_ROOT.glob(pattern)))
    return files


def _hits_for_pattern(
    paths: list[Path], pattern: re.Pattern[str], root: Path, allowlist: set[str]
) -> list[str]:
    """Pure scan used by both the real readiness check and its own regression
    test below — kept file-system-root-agnostic so the regression test can
    exercise it against synthetic tmp_path files instead of the real repo."""
    hits: list[str] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if rel in allowlist:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    return hits


@pytest.mark.parametrize("label,pattern", FORBIDDEN, ids=[label for label, _ in FORBIDDEN])
def test_no_personal_data_in_production_code(label: str, pattern: re.Pattern[str]) -> None:
    hits = _hits_for_pattern(_production_files() + _prompt_files(), pattern, PROJECT_ROOT, set())
    assert not hits, (
        f"Personal data ({label}) hardcoded in production code.\n"
        "Move it to candidate/candidate.yaml and read it via "
        "candidate.get(dotpath, <neutral default>):\n  " + "\n  ".join(hits)
    )


def test_legacy_allowlist_does_not_hide_new_offender(tmp_path: Path) -> None:
    """LEGACY_PERSONAL_DATA_ALLOWLIST must be scoped to exact paths, not to the
    whole prompts/ or .claude/commands/ directory. Prove a NEW file carrying
    the same personal data is still caught, while the two allowlisted legacy
    files are not — otherwise the allowlist could silently widen over time
    instead of shrinking towards empty as docs/GENERATION_ARCHITECTURE_ANALYSIS.md
    wave 2 lands."""
    owner_employer_pattern = dict(FORBIDDEN)["owner employer"]

    (tmp_path / "prompts").mkdir()
    new_offender = tmp_path / "prompts" / "some_new_prompt.md"
    new_offender.write_text("Worked at Fairmarkit for two years.\n", encoding="utf-8")
    legacy_offender = tmp_path / "prompts" / "generation_rules.md"
    legacy_offender.write_text("Worked at Fairmarkit for two years.\n", encoding="utf-8")

    hits = _hits_for_pattern(
        [new_offender, legacy_offender],
        owner_employer_pattern,
        tmp_path,
        {"prompts/generation_rules.md"},
    )

    assert any("some_new_prompt.md" in h for h in hits), (
        "a new prompts/ file with personal data must still fail the readiness check"
    )
    assert not any("generation_rules.md" in h for h in hits), (
        "the allowlisted legacy file must stay skipped"
    )


def test_env_example_documents_every_required_setup_var():
    """docs/SETUP_NEW_USER.md tells a new user to set these in .env — they must
    actually be present in .env.example, or the instruction dead-ends."""
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    required = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SOFFICE_PATH",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "APPLY_USE_CLI",
    ]
    missing = [v for v in required if not re.search(rf"^#?{v}=", env_example, re.M)]
    assert not missing, f".env.example is missing documented variables: {missing}"


def test_candidate_example_covers_every_dotpath_used_in_code():
    """Every candidate.get("a.b") in production code must have a matching key in
    candidate.yaml.example — otherwise a new user cannot discover the setting."""
    import yaml

    example = yaml.safe_load(
        (PROJECT_ROOT / "candidate" / "candidate.yaml.example").read_text(encoding="utf-8")
    )

    dotpaths: set[str] = set()
    for path in _production_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        dotpaths.update(re.findall(r'candidate\.get\(\s*"([a-z_][a-z_.]*)"', text))

    def resolves(dotpath: str) -> bool:
        node = example
        for part in dotpath.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    missing = sorted(dp for dp in dotpaths if not resolves(dp))
    assert not missing, (
        "candidate/candidate.yaml.example does not document these keys that the "
        f"code reads: {missing}"
    )


def test_dockerignore_excludes_sensitive_paths():
    """A local `docker build` (Dockerfile's `COPY . .`) must never bake secrets
    or per-tenant runtime data into the image — docs/improvement-2026-09/
    05-SECURITY_PLAN.md finding #10 / M1 / M7: `.dockerignore` didn't exclude
    gsheets_token.json / gsheets_credentials.json / .claude-cli/ / candidate/ /
    users/ / db/ / logs/ at all, so any of those present in the build context
    (the normal state on the deploy host, which builds from the same checkout
    it runs from) shipped straight into the image."""
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    required = [
        "gsheets_token.json",
        "gsheets_credentials.json",
        ".claude-cli/",
        "candidate/",
        "users/",
        "db/",
        "logs/",
    ]
    missing = [p for p in required if p not in dockerignore]
    assert not missing, f".dockerignore is missing entries for: {missing}"


def test_env_example_documents_every_env_var_the_code_reads():
    """Every environment variable actually read anywhere in production code must
    be documented in .env.example (as NAME= or a commented-out #NAME=), or a new
    user/operator has no way to discover it exists.

    docs/improvement-2026-09/06-OPS_PLAN.md's SRE audit found ~55 `os.getenv`
    names in hunter/config.py alone missing from .env.example (e.g.
    APPLY_QUEUE_ENABLED, DEFAULT_USER_ID, USERS_ROOT, TRACKER_DB_PATH,
    GDRIVE_ENABLED, every *_ENABLED source toggle) — a stale reference doc issue
    check (b) above can't catch, since it only asserts a short hand-picked list
    IS present, not that the list is EXHAUSTIVE. tools/list_env_vars.py does the
    collection (regex-based, same production surface as the personal-data scan
    above); this test just gates it.
    """
    names = list_env_vars.collect_env_var_names()
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    missing = sorted(
        n for n in names if not re.search(rf"^#*\s*{re.escape(n)}=", env_example, re.M)
    )
    assert not missing, (
        ".env.example is missing these env vars the code reads (add them, "
        "commented, with a one-line comment + the code default):\n  " + "\n  ".join(missing)
    )
