"""Handoff readiness for generation.yaml (mirrors
tests/test_handoff_readiness.py::test_candidate_example_covers_every_dotpath_used_in_code).

Every ``gen_profile.get("a.b")`` dotpath read by production code must have a
matching key in candidate/generation.example.yaml — an undocumented key is a
setting a new user cannot discover.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCANNED_ROOTS = ("hunter",)
SCANNED_FILES = ("generate_docs.py", "apply_agent.py")


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        files.extend(sorted((PROJECT_ROOT / root).rglob("*.py")))
    for name in SCANNED_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            files.append(path)
    return [f for f in files if "__pycache__" not in f.parts]


def test_generation_example_covers_every_dotpath_used_in_code():
    example = yaml.safe_load(
        (PROJECT_ROOT / "candidate" / "generation.example.yaml").read_text(encoding="utf-8")
    )

    dotpaths: set[str] = set()
    for path in _production_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        dotpaths.update(re.findall(r'gen_profile\.get\(\s*"([a-z_][a-z_.]*)"', text))

    assert dotpaths, "no gen_profile.get() call sites found — scan pattern is stale"

    def resolves(dotpath: str) -> bool:
        node = example
        for part in dotpath.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    missing = sorted(dp for dp in dotpaths if not resolves(dp))
    assert not missing, (
        "candidate/generation.example.yaml does not document these keys that "
        f"the code reads: {missing}"
    )


def test_generation_example_has_no_stale_keys():
    """The inverse check: every key documented in the example file must
    actually be read somewhere — an undocumented-but-unused key would be
    dead config nobody can discover was removed."""
    from hunter.gen_profile import _KEY_SPECS, _SECTION_MERGERS

    example = yaml.safe_load(
        (PROJECT_ROOT / "candidate" / "generation.example.yaml").read_text(encoding="utf-8")
    )

    def leaves(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from leaves(value, f"{prefix}{key}." if prefix else f"{key}.")
        else:
            yield prefix.rstrip(".")

    documented = set(leaves(example))
    known = set(_KEY_SPECS) | {dp for dp in documented if dp.split(".", 1)[0] in _SECTION_MERGERS}
    stale = sorted(documented - known)
    assert not stale, f"generation.example.yaml documents keys gen_profile does not know: {stale}"
