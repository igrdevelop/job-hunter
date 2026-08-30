"""
tools/render_profile.py — CLI seam for the profile renderer (docs/
RESUME_PROFILE_STORE_PLAN.md M4). A thin argparse wrapper over
hunter.profile_render.render_all: read a Profile JSON document, write
candidate.yaml / candidate_profile.md / base_cv_<track>.md (+ optional
generation_rules.local.md) into an output directory, print which files
were written. No logic of its own lives here — see hunter/profile_render.py.

    python tools/render_profile.py <profile.json> <out_dir>

Exit 0 with `{"written": [...]}` JSON on stdout on success, exit 1 with a
message on stderr when the input file is missing or not valid JSON. A full
overwrite every time, never a merge — same contract as render_all itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.profile_render import render_all  # noqa: E402 — after sys.path setup
from hunter.profile_schema import from_dict  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a Profile JSON document into candidate.yaml/candidate_profile.md/base_cv_*.md."
    )
    parser.add_argument("profile", type=Path, help="Path to the Profile JSON document.")
    parser.add_argument("out_dir", type=Path, help="Directory to write the rendered files into.")
    args = parser.parse_args(argv)

    if not args.profile.exists():
        print(f"ERROR: {args.profile} not found", file=sys.stderr)
        return 1

    try:
        data = json.loads(args.profile.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"ERROR: could not read {args.profile}: {e}", file=sys.stderr)
        return 1

    profile = from_dict(data)
    written = render_all(profile, args.out_dir)
    print(json.dumps({"written": [str(p) for p in written]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
