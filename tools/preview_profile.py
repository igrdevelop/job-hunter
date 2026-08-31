"""
tools/preview_profile.py — CLI seam for the profile "test resume" preview
(docs/PROFILE_PAGE_TABS_WORKORDER.md, the bot-repo work item). A thin
argparse wrapper over hunter.profile_preview.render_preview: read a Profile
JSON document, build a generic no-vacancy content.json for the requested
track, render it via generate_docs.py, print which files were written. No
logic of its own lives here — see hunter/profile_preview.py.

    python tools/preview_profile.py <profile.json> <out_dir> [--track angular]

Deterministic, $0, no LLM call. Runs against the CURRENT process's
candidate.yaml resolution (CANDIDATE_YAML_PATH env / repo-local
candidate/candidate.yaml) — same single-user assumption as
tools/render_profile.py; a multi-user run is what
hunter/schedules/profile_jobs.py's 'preview' job kind is for.

Exit 0 with `{"written": [...]}` JSON on stdout on success, exit 1 with a
message on stderr when the input file is missing/not valid JSON, the track
is not a safe slug, or generate_docs.py itself fails (e.g. no configured
candidate identity — see hunter.candidate.require_identity).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.profile_preview import CORE_TRACK, PreviewError, render_preview  # noqa: E402
from hunter.profile_schema import from_dict  # noqa: E402

# Force UTF-8 stdout/stderr on Windows (console defaults to cp1252, which
# can't encode a non-ASCII path/profile field and would crash the print
# instead of reporting the actual error — see tools/preview_judge.py for the
# same guard on stdout; here stderr needs it too, since every error path
# below prints untrusted path/exception text there).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a generic (no-vacancy) test-resume PDF from a Profile JSON document."
    )
    parser.add_argument("profile", type=Path, help="Path to the Profile JSON document.")
    parser.add_argument("out_dir", type=Path, help="Directory to write the rendered files into.")
    parser.add_argument(
        "--track",
        default=CORE_TRACK,
        help="Variant key to preview (e.g. angular/react), or 'core' (default).",
    )
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

    try:
        written = render_preview(profile, args.track, args.out_dir)
    except PreviewError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(json.dumps({"written": [str(p) for p in written]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
