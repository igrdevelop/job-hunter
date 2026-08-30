"""
tools/render_profile.py — CLI seam for the profile renderer (docs/
RESUME_PROFILE_STORE_PLAN.md M4). A thin argparse wrapper over
hunter.profile_render.render_all: read a Profile JSON document, write
candidate.yaml / candidate_profile.md / base_cv_<track>.md (+ optional
generation_rules.local.md) into an output directory, print which files
were written. No logic of its own lives here — see hunter/profile_render.py.

    python tools/render_profile.py <profile.json> <out_dir>

Exit 0 with `{"written": [...]}` JSON on stdout on success, exit 1 with a
message on stderr when the input file is missing, not valid JSON, or the
output directory can't be written to. A full overwrite every time, never a
merge — same contract as render_all itself. A profile that fails
profile_schema.validate() (e.g. no identity.full_name) still renders — this
CLI transforms data, it doesn't gate content quality, that's the site's
confirmation screen's job — but the problems are printed as a warning so a
caller doesn't mistake a blank candidate.yaml for a clean success.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.profile_render import render_all  # noqa: E402 — after sys.path setup
from hunter.profile_schema import from_dict, validate  # noqa: E402

# Force UTF-8 stdout/stderr on Windows (console defaults to cp1252, which
# can't encode a non-ASCII path/profile field and would crash the print
# instead of reporting the actual error — see tools/preview_judge.py for
# the same guard on stdout; here stderr needs it too, since every error
# path below prints untrusted path/exception text there).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a Profile JSON document into candidate.yaml/candidate_profile.md/base_cv_*.md."
    )
    parser.add_argument("profile", type=Path, help="Path to the Profile JSON document.")
    parser.add_argument("out_dir", type=Path, help="Directory to write the rendered files into.")
    args = parser.parse_args(argv)

    try:
        # utf-8-sig strips a leading BOM if present (e.g. a file saved by a
        # Windows tool) and is otherwise identical to plain utf-8.
        data = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    except OSError as e:
        print(f"ERROR: could not read {args.profile}: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"ERROR: {args.profile} is not valid JSON: {e}", file=sys.stderr)
        return 1

    profile = from_dict(data)
    problems = validate(profile)
    if problems:
        print(f"WARNING: profile has unresolved problems: {'; '.join(problems)}", file=sys.stderr)

    try:
        written = render_all(profile, args.out_dir)
    except OSError as e:
        print(f"ERROR: could not write to {args.out_dir}: {e}", file=sys.stderr)
        return 1

    print(json.dumps({"written": [str(p) for p in written]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
