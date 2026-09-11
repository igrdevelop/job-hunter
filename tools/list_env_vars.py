"""Collect every environment-variable NAME the codebase actually reads.

Used by ``tests/test_handoff_readiness.py`` (check d) to gate ``.env.example``
completeness, and runnable standalone for a human audit:

    python tools/list_env_vars.py

Scans the same production surface as the personal-data check in that same
test file (``hunter/`` + the four root entry scripts) for three read forms:

    os.getenv("NAME", ...)
    os.environ.get("NAME", ...)
    os.environ["NAME"]

A handful of call sites don't pass the literal name inline — they hold it in
a module-level constant first (e.g. ``llm_client.py``'s
``CLI_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"`` then
``os.environ.get(CLI_TOKEN_ENV, ...)``, or ``hunter/sources/linkedin.py``'s
``_STORAGE_STATE_ENV``). This collector resolves those too: it first records
every ``UPPER_CASE = "SOME_ENV_NAME"`` assignment per file, then substitutes
that mapping into a bare-identifier read.

Regexes, not an AST, on purpose — this only needs to be conservative enough
to gate a "did you forget to document it" test, and a regex-based scan is
much easier to eyeball-verify against the file text than a walker.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Same production surface as tests/test_handoff_readiness.py's personal-data
# scan: the package that ships in the Docker image + the four root entry
# scripts. tools/ itself is developer-local and deliberately excluded — a
# name read only by a dev CLI tool (e.g. tools/preview_judge.py's JUDGE_MODE,
# already documented) doesn't need a fresh .env.example entry to stay usable.
SCANNED_ROOTS = ("hunter",)
SCANNED_FILES = ("generate_docs.py", "apply_agent.py", "llm_client.py", "hunter.py")

# Three literal-name read forms.
_LITERAL_PATTERNS = [
    re.compile(r'os\.getenv\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']', re.MULTILINE),
    re.compile(r'os\.environ\.get\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']', re.MULTILINE),
    re.compile(r'os\.environ\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]', re.MULTILINE),
    # hunter/gen_profile.py's `"dotpath.key": ("ENV_NAME", caster)` override
    # table — the actual os.environ.get(env_name) call is one level removed
    # (env_name is a loop variable resolved from this dict at call time), so
    # the three direct patterns above never see the literal. Scoped to a
    # dict-value tuple whose first element is an ALL_CAPS identifier, which
    # only this table's shape matches in the scanned surface today.
    re.compile(r':\s*\(\s*["\']([A-Z_][A-Z0-9_]*)["\']\s*,', re.MULTILINE),
]

# Indirect read forms: the first positional arg is a bare identifier
# (a module-level constant) instead of a string literal.
_INDIRECT_PATTERNS = [
    re.compile(r"os\.getenv\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]", re.MULTILINE),
    re.compile(r"os\.environ\.get\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]", re.MULTILINE),
    re.compile(r"os\.environ\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]", re.MULTILINE),
]

# `SOME_CONST = "ENV_VAR_NAME"` — module-level (or class-level) string
# constant assignment, candidate for resolving an indirect read above.
_CONST_ASSIGN = re.compile(
    r'^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']', re.MULTILINE
)


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        files.extend(sorted((PROJECT_ROOT / root).rglob("*.py")))
    for name in SCANNED_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            files.append(path)
    return [f for f in files if "__pycache__" not in f.parts]


def collect_env_var_names(files: list[Path] | None = None) -> set[str]:
    """Every env-var NAME read (directly or via a same-file constant alias)
    across the given files (default: the production surface above)."""
    if files is None:
        files = _production_files()

    names: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")

        for pattern in _LITERAL_PATTERNS:
            names.update(pattern.findall(text))

        # Resolve indirect reads against this file's own constant assignments.
        const_map = dict(_CONST_ASSIGN.findall(text))
        for pattern in _INDIRECT_PATTERNS:
            for ident in pattern.findall(text):
                resolved = const_map.get(ident)
                if resolved:
                    names.add(resolved)

    return names


if __name__ == "__main__":
    for name in sorted(collect_env_var_names()):
        print(name)
