#!/usr/bin/env python3
"""mypy regression ratchet.

Runs mypy over the same targets CI checks, counts errors per file, and
compares the count against a committed baseline (`mypy_baseline.json`). The
gate is a RATCHET, not a fixed threshold: it fails only when a file's error
count goes UP relative to the baseline, or when a file with errors is not in
the baseline at all (a brand-new file that was never audited). A file whose
count goes DOWN is reported but never fails the build — that is exactly the
improvement the ratchet exists to protect once it lands.

This lets `typecheck` be a real, blocking CI gate (see
docs/improvement-2026-09/04-ENGINEERING_PLAN.md M2) without requiring the
whole 218-error baseline to be fixed first — a plain `mypy ...` with no
`continue-on-error` would block on day one.

Usage:
    python scripts/mypy_ratchet.py            # check against the baseline, exit 1 on regression
    python scripts/mypy_ratchet.py --update    # rewrite mypy_baseline.json from the current run

`--update` is a deliberate, separate action (its own commit) — never run
automatically by CI.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# Force UTF-8 output on Windows (console defaults to cp1252 -> em-dash crash
# risk), matching the same guard in tools/parse_resume.py / preview_judge.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "mypy_baseline.json"

# Same targets as .github/workflows/deploy.yml's `typecheck` job.
MYPY_TARGETS = ["hunter/", "llm_client.py", "generate_docs.py", "apply_agent.py"]

# Matches a mypy error line, e.g.:
#   hunter/schedules/hunt.py:17: error: Item "object" ... [union-attr]
# Non-greedy filename group so a Windows drive-letter path (C:\...) still
# resolves correctly — the engine backtracks past the drive-letter colon
# because nothing follows it with `\d+:`.
_ERROR_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): error: (?P<message>.*)$")


def _normalize_path(raw: str) -> str:
    """Normalize a mypy-reported path to forward slashes, repo-relative.

    mypy on Windows reports paths with backslashes (`hunter\\x.py`); CI runs
    on Linux and reports forward slashes (`hunter/x.py`). The baseline file
    must be OS-independent so it means the same thing in both places.
    """
    return raw.replace("\\", "/")


def run_mypy() -> tuple[int, str]:
    """Run mypy over MYPY_TARGETS, return (exit_code, combined stdout+stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", *MYPY_TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def parse_error_counts(mypy_output: str) -> Counter[str]:
    """Parse mypy's text output into a per-file error count."""
    counts: Counter[str] = Counter()
    for line in mypy_output.splitlines():
        m = _ERROR_RE.match(line)
        if not m:
            continue
        counts[_normalize_path(m.group("file"))] += 1
    return counts


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {"generated": "", "total": 0, "files": {}}
    with BASELINE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def write_baseline(counts: Counter[str]) -> dict:
    files = {path: n for path, n in sorted(counts.items()) if n > 0}
    baseline = {
        "generated": date.today().isoformat(),
        "total": sum(files.values()),
        "files": files,
    }
    with BASELINE_PATH.open("w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return baseline


def compare(current: Counter[str], baseline: dict) -> tuple[bool, list[str]]:
    """Compare current counts to the baseline.

    Returns (ok, report_lines). ok is False iff there is at least one
    regression (a file's count increased, or a new file has errors not
    present in the baseline at all).
    """
    baseline_files: dict[str, int] = baseline.get("files", {})
    all_paths = sorted(set(current) | set(baseline_files))

    new_file_lines: list[str] = []
    regressed_lines: list[str] = []
    improved_lines: list[str] = []
    has_regression = False

    for path in all_paths:
        cur = current.get(path, 0)
        base = baseline_files.get(path, 0)
        if cur == base:
            continue
        if path not in baseline_files and cur > 0:
            new_file_lines.append(f"  {path}: {cur} (NEW FILE, not in baseline)")
            has_regression = True
        elif cur > base:
            regressed_lines.append(f"  {path}: {base} -> {cur} (+{cur - base}) REGRESSION")
            has_regression = True
        elif cur < base:
            improved_lines.append(f"  {path}: {base} -> {cur} ({cur - base})")

    report: list[str] = ["mypy ratchet report", "=" * 60]

    if new_file_lines:
        report.append("New files with errors (not in baseline):")
        report.extend(new_file_lines)
        report.append("")

    if regressed_lines:
        report.append("Regressed files (error count increased):")
        report.extend(regressed_lines)
        report.append("")

    if improved_lines:
        report.append("Improved files (error count decreased) — report only, never fails:")
        report.extend(improved_lines)
        report.append("")

    cur_total = sum(current.values())
    base_total = baseline.get("total", 0)
    report.append(f"Total errors: baseline={base_total} current={cur_total}")

    return not has_regression, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite mypy_baseline.json from the current mypy run instead of checking it.",
    )
    args = parser.parse_args()

    print(f"Running: mypy {' '.join(MYPY_TARGETS)}")
    _mypy_exit, output = run_mypy()
    print(output)

    current = parse_error_counts(output)

    if args.update:
        baseline = write_baseline(current)
        print(
            f"Updated {BASELINE_PATH.name}: {baseline['total']} errors in {len(baseline['files'])} files"
        )
        return 0

    baseline = load_baseline()
    ok, report_lines = compare(current, baseline)
    print("\n".join(report_lines))

    if not ok:
        print(
            "\nmypy ratchet FAILED: at least one file has more errors than the "
            "committed baseline, or a new file has errors that were never "
            "baselined. Fix the regression, or if the increase is deliberate "
            "and reviewed, run `python scripts/mypy_ratchet.py --update` in "
            "its own commit.",
            file=sys.stderr,
        )
        return 1

    print("\nmypy ratchet OK — no regressions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
