"""
tools/judge_stats.py — aggregate claim-judge findings into prompt-tuning
feedback (docs/LLM_COST_REDUCTION_PLAN.md M6).

The claim judge (hunter/claim_judge.py) and the deterministic scrubs
(hunter.apply_shared: compliance/prestige/gloss) catch overlapping classes of
violation — every one caught downstream is a repair-round-trip that could
have been avoided by a tighter generation_rules.md RED LINE. Findings are
persisted per-vacancy at Applications/**/judge_report.json but never
aggregated, so patterns across many CVs are invisible.

Read-only: scans judge_report.json files, prints the most frequent violation
classes with example quotes, and a plain-text "rule candidate" draft per
frequent class. Does NOT edit generation_rules.md — the owner decides what
(if anything) to add.

Usage:
    python tools/judge_stats.py [--dir Applications] [--top 20]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

_ARRAY_INDEX_RE = re.compile(r"\[\d+\]")
_WS_RE = re.compile(r"\s+")


def normalize_field(field_path: str) -> str:
    """Collapse array indices so 'resume_en.experience[2].bullets[1]' and
    'resume_en.experience[5].bullets[0]' count as the same field class."""
    return _ARRAY_INDEX_RE.sub("[]", field_path or "")


def normalize_reason(reason: str) -> str:
    """Lowercase + collapse whitespace so near-identical judge phrasing
    groups into one bucket."""
    return _WS_RE.sub(" ", (reason or "").strip().lower())


@dataclass
class ClassStats:
    key: tuple[str, str, str]  # (severity, field_class, normalized_reason)
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, quote: str) -> None:
        self.count += 1
        if len(self.examples) < 3 and quote and quote not in self.examples:
            self.examples.append(quote)


def find_judge_reports(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("judge_report.json"))


def load_violations(paths: list[Path]) -> list[dict]:
    """Read every judge_report.json, return the flat list of violation dicts.
    A single unreadable/malformed file is skipped, not fatal."""
    out: list[dict] = []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[judge_stats] skipping unreadable {p}: {e}")
            continue
        for v in data.get("violations") or []:
            if isinstance(v, dict):
                out.append(v)
    return out


def aggregate(violations: list[dict]) -> dict[tuple[str, str, str], ClassStats]:
    buckets: dict[tuple[str, str, str], ClassStats] = {}
    for v in violations:
        severity = str(v.get("severity") or "?").strip().lower()
        field_class = normalize_field(str(v.get("field") or ""))
        reason_class = normalize_reason(str(v.get("reason") or ""))
        key = (severity, field_class, reason_class)
        stats = buckets.setdefault(key, ClassStats(key=key))
        stats.add(str(v.get("quote") or ""))
    return buckets


def severity_breakdown(violations: list[dict]) -> Counter:
    return Counter(str(v.get("severity") or "?").strip().lower() for v in violations)


def format_top(buckets: dict[tuple[str, str, str], ClassStats], top: int) -> str:
    ranked = sorted(buckets.values(), key=lambda s: -s.count)[:top]
    lines = []
    for i, s in enumerate(ranked, 1):
        severity, field_class, reason_class = s.key
        lines.append(f"{i}. [{severity}] {field_class} x{s.count}")
        lines.append(f"   reason: {reason_class}")
        for ex in s.examples:
            lines.append(f"   e.g. {ex!r}")
    return "\n".join(lines) if lines else "(no violations found)"


def suggest_rule_candidates(
    buckets: dict[tuple[str, str, str], ClassStats], min_count: int = 2
) -> str:
    """One draft RED LINE line per frequent class — owner decides what (if
    anything) to actually add to generation_rules.md."""
    frequent = sorted(
        (s for s in buckets.values() if s.count >= min_count),
        key=lambda s: -s.count,
    )
    if not frequent:
        return "(no class occurred >= {} times — nothing to suggest)".format(min_count)
    lines = []
    for s in frequent:
        severity, field_class, reason_class = s.key
        lines.append(
            f"- [{severity}, seen {s.count}x in {field_class}] RED LINE candidate: "
            f'never write claims matching "{reason_class}" — see e.g. {s.examples[:1]!r}'
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="Applications", help="applications root directory")
    parser.add_argument("--top", type=int, default=20, help="how many top classes to print")
    args = parser.parse_args()

    root = Path(args.dir)
    paths = find_judge_reports(root)
    violations = load_violations(paths)
    print(f"[judge_stats] {len(paths)} judge_report.json file(s), {len(violations)} violation(s)")

    if not violations:
        return

    breakdown = severity_breakdown(violations)
    total = sum(breakdown.values())
    print("\nSeverity breakdown:")
    for severity, count in breakdown.most_common():
        print(f"  {severity}: {count} ({100 * count / total:.1f}%)")

    buckets = aggregate(violations)
    print(f"\nTop {args.top} violation classes:")
    print(format_top(buckets, args.top))

    print("\n## Suggested rule candidates")
    print(suggest_rule_candidates(buckets))


if __name__ == "__main__":
    main()
