"""
tools/erase_user.py — CLI seam for hunter.erasure.erase_user (docs/
improvement-2026-09/07-COMPLIANCE_PLAN.md M1, docs/ERASURE_CONTRACT.md). A
thin argparse wrapper — no logic of its own lives here, see
hunter/erasure.py.

    python tools/erase_user.py --user <uid> --yes
    python tools/erase_user.py --user <uid> --dry-run

Prints the erasure report as JSON on stdout, exit 0. Destructive by default,
so a real run additionally requires --yes; --dry-run needs no confirmation
since it changes nothing. Exit 1 + "ERROR: ..." on stderr for a missing
--yes, an unsafe/empty --user, an owner refusal (pass --force-owner to
override — dangerous, off by default), or a filesystem error from the
removal step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.erasure import erase_user  # noqa: E402 — after sys.path setup

# Force UTF-8 stdout/stderr on Windows (console defaults to cp1252, which
# can't encode a non-ASCII uid/path in an error message) — same guard as
# tools/render_profile.py / tools/preview_judge.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Erase one user's tracker.db rows + their users/{uid}/ filesystem tree."
    )
    parser.add_argument("--user", required=True, help="user_id to erase.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be erased; changes nothing. No --yes needed.",
    )
    parser.add_argument(
        "--force-owner",
        action="store_true",
        help="Allow erasing DEFAULT_USER_ID (the owner account). Dangerous — off by default.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive run. Required unless --dry-run is also given.",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.yes:
        print(
            "ERROR: this is destructive — pass --yes to confirm, or --dry-run to preview",
            file=sys.stderr,
        )
        return 1

    try:
        report = erase_user(args.user, dry_run=args.dry_run, force_owner=args.force_owner)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ERROR: filesystem cleanup failed: {e}", file=sys.stderr)
        return 1

    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
