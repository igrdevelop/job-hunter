"""
tools/parse_resume.py — CLI seam for the resume parser (docs/
RESUME_PROFILE_STORE_PLAN.md M4). A thin argparse wrapper over
hunter.profile_parse: extract text from an uploaded resume, parse it into a
structured Profile, print the profile as JSON on stdout. No logic of its
own lives here — see hunter/profile_parse.py.

    python tools/parse_resume.py <resume-file>              # LLM-assisted parse
    python tools/parse_resume.py <resume-file> --no-llm      # $0, leftovers-only

Contract (folds into the SAAS-stage HTTP service later, not a parallel
transport): exit 0 with the Profile JSON document on stdout on success,
exit 1 with a message on stderr when the file itself can't be read at all
(unknown extension, corrupt file). A parse that degrades to leftovers-only
is still exit 0 — that distinction is exactly what the site's confirmation
screen exists to show the user, not something this CLI decides.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.profile_parse import (  # noqa: E402 — after sys.path setup
    LLMCallable,
    ProfileParseError,
    extract_resume_text,
    parse_resume_text,
)
from hunter.profile_schema import to_dict  # noqa: E402

# Force UTF-8 stdout/stderr on Windows (console defaults to cp1252, which
# can't encode a non-ASCII resume filename or profile field and would crash
# the print instead of reporting the actual error — see tools/preview_judge.py
# for the same guard on stdout; here stderr needs it too, since the error
# path below prints an untrusted exception message there).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse a resume file (.docx/.pdf/.txt/.md) into a Profile JSON document."
    )
    parser.add_argument("resume", type=Path, help="Path to the resume file.")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the LLM call — the whole text lands in leftovers (a $0 dry run).",
    )
    parser.add_argument(
        "--upload-id",
        default="",
        help="Optional upload id stamped onto every leftover produced.",
    )
    args = parser.parse_args(argv)

    try:
        text = extract_resume_text(args.resume)
    except ProfileParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    llm: LLMCallable | None = None
    if not args.no_llm:
        from llm_client import call_llm

        llm = call_llm

    profile = parse_resume_text(text, llm=llm, source_upload_id=args.upload_id)
    print(json.dumps(to_dict(profile), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
