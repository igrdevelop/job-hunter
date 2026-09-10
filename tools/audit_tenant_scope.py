"""
tools/audit_tenant_scope.py — static scan for `applications` SQL statements
missing a user_id predicate.

docs/improvement-2026-09/05-SECURITY_PLAN.md M0 / finding #2: tenant
isolation on `hunter/tracker.py`'s ~25-odd `conn.execute(...)` calls is only
as good as every one of them actually filtering by user_id — a
`WHERE url_norm=?` write (`set_ats_verdict`/`set_cost`/`set_to_learn`/
`claim_pending`/...) touches the matching row for EVERY tenant that ever
applied to that exact URL. This is a static AST scan, not a runtime probe —
zero LLM calls, zero DB access, zero writes.

Method: parse every .py file under `hunter/` that contains the substring
`execute(` (the plan's own "grep execute(" pre-filter, done here in Python
so the AST pass only runs where it can possibly find something), walk each
module's AST tracking the innermost enclosing function, and for every
`.execute(...)`/`.executescript(...)` call whose first argument is a SQL
string (recovered from a plain string constant, a multi-line/triple-quoted
string — `ast` folds these into ONE `Constant` regardless of how the source
wraps them, so multi-line SQL needs no special-casing here — an f-string, or
a `+`-concatenation) that mentions the `applications` table, record whether
the string also mentions `user_id`.

Every WRITE (INSERT/UPDATE/DELETE) on `applications` without a `user_id`
predicate is a tenant-isolation gap; SELECTs are reported too (read-only
cross-tenant leaks matter for privacy, just not for the exit-code gate the
plan proposes). Exits 1 when any WRITE is missing user_id, so this can
become a CI gate later without further changes.

Usage:
    docker compose exec -T job-hunter python tools/audit_tenant_scope.py
    docker compose exec -T job-hunter python tools/audit_tenant_scope.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Force UTF-8 output on Windows (console defaults to cp1252 -> emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

_STMT_RE = re.compile(r"(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|PRAGMA)\b", re.I)
_TABLE_RE = re.compile(r"\bapplications\b", re.I)
_USER_ID_RE = re.compile(r"\buser_id\b", re.I)
_WRITE_TYPES = ("INSERT", "UPDATE", "DELETE")


@dataclass
class SqlCall:
    file: str
    function: str
    lineno: int
    stmt_type: str
    has_user_id: bool
    snippet: str


# ── SQL text recovery from AST (pure) ───────────────────────────────────────


def sql_text_from_node(node: ast.AST) -> str | None:
    """Best-effort reconstruction of the literal SQL text of a call argument.

    Handles a plain string constant (multi-line/triple-quoted collapses to
    ONE Constant regardless of source formatting — this is what makes the
    scan robust to multi-line SQL without any special-casing), an f-string
    (JoinedStr — interpolated parts become a '?' placeholder, since only the
    literal SQL matters here), and `+` string concatenation. Returns None for
    anything else (a bare variable, a function call building the string
    elsewhere) — those calls are simply not reported, not mis-flagged.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("?")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = sql_text_from_node(node.left)
        right = sql_text_from_node(node.right)
        if left is not None and right is not None:
            return left + right
    return None


class _SqlCallVisitor(ast.NodeVisitor):
    """Walks a module, tracking the innermost enclosing function so every
    `.execute()`/`.executescript()` call can be attributed to it."""

    def __init__(self, filename: str):
        self.filename = filename
        self._func_stack: list[str] = []
        self.calls: list[SqlCall] = []

    def _current_function(self) -> str:
        return self._func_stack[-1] if self._func_stack else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("execute", "executescript")
            and node.args
        ):
            sql = sql_text_from_node(node.args[0])
            if sql is not None and _TABLE_RE.search(sql):
                m = _STMT_RE.search(sql.lstrip())
                stmt_type = m.group(1).upper() if m else "OTHER"
                self.calls.append(
                    SqlCall(
                        file=self.filename,
                        function=self._current_function(),
                        lineno=node.lineno,
                        stmt_type=stmt_type,
                        has_user_id=bool(_USER_ID_RE.search(sql)),
                        snippet=" ".join(sql.split())[:100],
                    )
                )
        self.generic_visit(node)


def scan_file(path: Path) -> list[SqlCall]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    visitor = _SqlCallVisitor(str(path))
    visitor.visit(tree)
    return visitor.calls


def discover_candidate_files(root: Path) -> list[Path]:
    """Every .py under `root` whose raw text contains 'execute(' — the
    plan's own grep pre-filter, cheap enough to run before the AST parse."""
    out = []
    for p in sorted(root.rglob("*.py")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if "execute(" in text:
            out.append(p)
    return out


def scan_files(paths: list[Path]) -> list[SqlCall]:
    calls: list[SqlCall] = []
    for p in paths:
        calls.extend(scan_file(p))
    return calls


# ── Report ────────────────────────────────────────────────────────────────────

DECISION_RULE = """
Decision rule (docs/improvement-2026-09/05-SECURITY_PLAN.md, M0):
  - At least one WRITE (INSERT/UPDATE/DELETE) statement on `applications`
    lacking a user_id predicate, reachable from a non-owner path (the known
    example: url_message.py -> apply subprocess -> set_ats_verdict /
    set_cost / set_to_learn) -> M2 (tenant-scoped tracker queries) is
    mandatory before the first paying client.
  - `--dangerously-skip-permissions` present in the CLI apply path -> M1
    (isolate the CLI agent) ships first, immediately, ahead of M2.
""".strip()


def _relpath(file: str) -> str:
    try:
        return os.path.relpath(file, PROJECT_DIR)
    except ValueError:
        return file


def grep_dangerous_cli_flags(hunter_dir: Path, dockerfile: Path) -> list[str]:
    """Secondary M0 check (the plan also asks for this in the same pass):
    where does `--dangerously-skip-permissions` / `IS_SANDBOX` show up?"""
    hits: list[str] = []
    targets = [*sorted(hunter_dir.rglob("*.py"))]
    if dockerfile.exists():
        targets.append(dockerfile)
    for p in targets:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "dangerously-skip-permissions" in line or "IS_SANDBOX" in line:
                hits.append(f"{_relpath(str(p))}:{lineno}: {line.strip()}")
    return hits


def build_report(hunter_dir: Path, dockerfile: Path) -> dict:
    files = discover_candidate_files(hunter_dir)
    calls = scan_files(files)
    missing = [c for c in calls if not c.has_user_id]
    missing_writes = [c for c in missing if c.stmt_type in _WRITE_TYPES]
    dangerous_cli = grep_dangerous_cli_flags(hunter_dir, dockerfile)
    return {
        "files_scanned": len(files),
        "calls_on_applications": len(calls),
        "missing_user_id": len(missing),
        "missing_user_id_writes": len(missing_writes),
        "findings": [
            {
                "file": _relpath(c.file),
                "function": c.function,
                "line": c.lineno,
                "type": c.stmt_type,
                "sql": c.snippet,
            }
            for c in sorted(missing, key=lambda c: (c.file, c.lineno))
        ],
        "dangerous_cli_flags": dangerous_cli,
    }


def format_report(report: dict) -> str:
    lines = [
        f"[audit_tenant_scope] scanned {report['files_scanned']} file(s) containing 'execute('",
        f"[audit_tenant_scope] {report['calls_on_applications']} statement(s) touch `applications`",
        f"[audit_tenant_scope] {report['missing_user_id']} of those lack a user_id predicate "
        f"({report['missing_user_id_writes']} are WRITEs)",
        "",
    ]
    if report["findings"]:
        header = f"{'File':<30} {'Function':<28} {'Line':>5} {'Type':<8} SQL"
        lines.append(header)
        lines.append("-" * len(header))
        for f in report["findings"]:
            lines.append(
                f"{f['file']:<30} {f['function']:<28} {f['line']:>5} {f['type']:<8} {f['sql']}"
            )
    else:
        lines.append("(no statement on `applications` is missing a user_id predicate)")

    lines.append("")
    if report["dangerous_cli_flags"]:
        lines.append("dangerously-skip-permissions / IS_SANDBOX hits:")
        for h in report["dangerous_cli_flags"]:
            lines.append(f"  {h}")
    else:
        lines.append("(no dangerously-skip-permissions / IS_SANDBOX hits found)")

    lines.append("")
    lines.append(DECISION_RULE)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--hunter-dir", type=Path, default=None, help="root to scan (default: hunter/)"
    )
    parser.add_argument(
        "--dockerfile", type=Path, default=None, help="Dockerfile to grep (default: ./Dockerfile)"
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    hunter_dir = args.hunter_dir or (PROJECT_DIR / "hunter")
    dockerfile = args.dockerfile or (PROJECT_DIR / "Dockerfile")

    report = build_report(hunter_dir, dockerfile)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))

    if report["missing_user_id_writes"]:
        print(
            f"\n[audit_tenant_scope] FAIL: {report['missing_user_id_writes']} "
            "WRITE(s) on `applications` without a user_id predicate.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\n[audit_tenant_scope] OK: no WRITE on `applications` is missing user_id.")


if __name__ == "__main__":
    main()
