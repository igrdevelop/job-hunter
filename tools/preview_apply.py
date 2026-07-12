"""
tools/preview_apply.py — Run apply pipeline against sample job fixtures.

Outputs go to tests/output/{track}/ instead of Applications/.
Uses CLI mode (Pro subscription) when available, API as fallback.
APPLICATIONS_DIR env var is set so CLI writes to tests/output/{track}/.

Usage:
    python tools/preview_apply.py --track angular
    python tools/preview_apply.py --track react
    python tools/preview_apply.py --track ai
    python tools/preview_apply.py --track fullstack_angular_nest
    python tools/preview_apply.py --track fullstack_react_next
    python tools/preview_apply.py --track all
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
FIXTURES_DIR = PROJECT_DIR / "tests" / "fixtures" / "sample_jobs"
OUTPUT_DIR = PROJECT_DIR / "tests" / "output"

TRACKS = {
    "angular": "angular.txt",
    "react": "react.txt",
    "ai": "ai.txt",
    "fullstack_angular_nest": "fullstack_angular_nest.txt",
    "fullstack_react_next": "fullstack_react_next.txt",
}


def run_track(track: str) -> bool:
    fixture = FIXTURES_DIR / TRACKS[track]
    if not fixture.exists():
        print(f"[preview] ERROR: fixture not found: {fixture}")
        return False

    out_dir = OUTPUT_DIR / track
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"  Track: {track}")
    print(f"  Fixture: {fixture.name}")
    print(f"  Output: tests/output/{track}/")
    print(f"{'=' * 60}\n")

    env = os.environ.copy()
    env["APPLICATIONS_DIR"] = str(out_dir)  # CLI reads this via $APPLICATIONS_DIR in apply.md
    env["TELEGRAM_BOT_TOKEN"] = ""  # suppress Telegram notifications
    env["TELEGRAM_CHAT_ID"] = "0"

    cmd = [
        sys.executable,
        str(PROJECT_DIR / "apply_agent.py"),
        "--paste-file",
        str(fixture),
        "--force",  # skip dedup (same fixture would be skipped otherwise)
    ]

    result = subprocess.run(cmd, env=env, cwd=str(PROJECT_DIR))

    if result.returncode == 0:
        files = sorted(out_dir.rglob("*.*"))
        print(f"\n[preview] Output files in tests/output/{track}/:")
        for f in files:
            rel = f.relative_to(out_dir)
            print(f"  {rel}")
        return True
    else:
        print(f"\n[preview] FAILED (exit code {result.returncode})")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview apply pipeline per track")
    parser.add_argument(
        "--track",
        choices=list(TRACKS) + ["all"],
        required=True,
        help="Track to run (or 'all' for all tracks sequentially)",
    )
    args = parser.parse_args()

    tracks = list(TRACKS) if args.track == "all" else [args.track]
    results = {}

    for track in tracks:
        results[track] = run_track(track)

    print(f"\n{'=' * 60}")
    print("  Summary")
    print(f"{'=' * 60}")
    for track, ok in results.items():
        status = "OK" if ok else "FAILED"
        print(f"  {track:<30} {status}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
