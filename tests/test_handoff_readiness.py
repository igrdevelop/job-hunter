"""Handoff readiness — keep one person's personal data out of shared code.

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
the strings it forbids.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Production code only: the packages that ship in the Docker image and run
# against real vacancies. tools/ is developer-local and .claude/ is agent
# tooling, both out of scope.
SCANNED_ROOTS = ("hunter",)
SCANNED_FILES = ("generate_docs.py", "apply_agent.py", "llm_client.py", "hunter.py")

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


@pytest.mark.parametrize("label,pattern", FORBIDDEN, ids=[label for label, _ in FORBIDDEN])
def test_no_personal_data_in_production_code(label: str, pattern: re.Pattern[str]) -> None:
    hits: list[str] = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(PROJECT_ROOT).as_posix()
                hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not hits, (
        f"Personal data ({label}) hardcoded in production code.\n"
        "Move it to candidate/candidate.yaml and read it via "
        "candidate.get(dotpath, <neutral default>):\n  " + "\n  ".join(hits)
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
