"""
tools/pii_inventory.py — enumerate every place a user id appears, before
building an erasure operation.

docs/improvement-2026-09/07-COMPLIANCE_PLAN.md M0 / finding #1:
`admin.deleteUser` (job-hunter-api) today cleans `users`, `profiles`,
`profile_revisions`, `profile_jobs` and the `users/{uid}/` tree — and
nothing else. This tool enumerates what "nothing else" actually is, so the
decision "is an erasure milestone mandatory before the first paying client"
is made from a real count, not a guess.

Checks, all read-only:
  - every table in tracker.db that HAS a user_id column (discovered via
    PRAGMA table_info — not hardcoded, so a future table with a user_id
    column is picked up automatically), row count for --user
  - if --app-sqlite PATH is given: every table in the API's own sqlite DB
    with a user-referencing column (heuristic: any column name that
    normalizes to "userid", plus `users.id` itself)
  - the users/{uid}/ tree: file count + total bytes
  - logs/ and backups/: number of files whose content mentions the uid
    (best-effort substring search; a compressed/binary file, e.g. an .xlsx
    backup, may hide the id from a plain search)

Always read-only. `--dry-run` is accepted for symmetry with a future
`hunter/erasure.py::erase_user()` (this tool never writes regardless of the
flag).

Usage:
    docker compose exec -T job-hunter python tools/pii_inventory.py --user <uid>
    docker compose exec -T job-hunter python tools/pii_inventory.py --user <uid> \\
        --app-sqlite /path/to/job-hunter-api/app.sqlite
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 output on Windows (console defaults to cp1252 -> emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# What admin.deleteUser already cleans today (docs/improvement-2026-09/
# 07-COMPLIANCE_PLAN.md finding #1's inventory table) — excluded from the
# "extra places" tally so the decision rule reflects only real gaps.
_ALREADY_CLEANED_TRACKER_TABLES = {"profile_jobs"}
_ALREADY_CLEANED_APP_SQLITE_TABLES = {"users", "profiles", "profile_revisions"}


def _safe_ident(name: str) -> bool:
    return bool(_IDENT_RE.match(name))


# ── tracker.db ───────────────────────────────────────────────────────────────


def discover_user_id_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table in `conn` that has a user_id column."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    out = []
    for t in tables:
        if not _safe_ident(t):
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]  # noqa: S608 — t is a sqlite_master table name, not user input
        if "user_id" in cols:
            out.append(t)
    return sorted(out)


def count_rows_by_user_id(conn: sqlite3.Connection, table: str, uid: str) -> int:
    if not _safe_ident(table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()  # noqa: S608
    return int(row[0]) if row else 0


def tracker_inventory(db_path: Path, uid: str) -> dict[str, int]:
    if not db_path.exists():
        return {}
    from hunter.db import get_db

    with get_db(db_path) as conn:
        tables = discover_user_id_tables(conn)
        return {t: count_rows_by_user_id(conn, t, uid) for t in tables}


# ── app.sqlite (job-hunter-api's own DB) ────────────────────────────────────


def _user_ref_columns(cols: list[str]) -> list[str]:
    """Columns whose name normalizes to 'userid' — covers both snake_case
    (user_id) and TypeORM's camelCase (userId) without hardcoding a schema
    this repo doesn't own."""
    return [c for c in cols if c.replace("_", "").lower() == "userid"]


def app_sqlite_inventory(path: Path, uid: str) -> list[dict[str, Any]]:
    """Generic, read-only scan of the API's sqlite DB for rows referencing
    `uid`. Schema is job-hunter-api's own (TypeORM entities) — this repo
    doesn't own it, so detection is heuristic rather than hardcoded to
    named tables/columns; see `_user_ref_columns`."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for t in tables:
            if not _safe_ident(t):
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]  # noqa: S608
            check_cols = list(_user_ref_columns(cols))
            if t.lower() == "users":
                check_cols += [c for c in cols if c.lower() == "id"]
            for c in check_cols:
                if not _safe_ident(c):
                    continue
                n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c}=?", (uid,)).fetchone()[0]  # noqa: S608
                if n:
                    out.append({"table": t, "column": c, "count": int(n)})
    finally:
        conn.close()
    return out


# ── users/{uid}/ tree ────────────────────────────────────────────────────────


def dir_stats(root: Path) -> dict[str, int]:
    if not root.exists():
        return {"files": 0, "bytes": 0}
    n = 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            n += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return {"files": n, "bytes": total}


# ── logs / backups grep ─────────────────────────────────────────────────────


def grep_file_count(root: Path, needle: str) -> int:
    """Count files under `root` whose bytes contain `needle` — best-effort:
    a binary/compressed file (e.g. a .xlsx backup) may hide the id from a
    plain substring search. Read-only sanity check, not a guarantee."""
    if not root.exists() or not needle:
        return 0
    needle_bytes = needle.encode("utf-8")
    n = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if needle_bytes in data:
            n += 1
    return n


# ── Report ────────────────────────────────────────────────────────────────────

DECISION_RULE = """
Decision rule (docs/improvement-2026-09/07-COMPLIANCE_PLAN.md, M0):
  - If this tool finds the uid in more places than admin.deleteUser already
    cleans (users, profiles, profile_revisions, profile_jobs, users/{uid}/)
    -> the erasure milestone (M1: hunter/erasure.py::erase_user) is
    mandatory before the first paying client.
""".strip()


def build_report(
    uid: str,
    *,
    db_path: Path,
    app_sqlite_path: Path | None,
    users_root: Path,
    logs_dir: Path,
    backups_dir: Path,
) -> dict[str, Any]:
    tracker_counts = tracker_inventory(db_path, uid)
    app_hits = app_sqlite_inventory(app_sqlite_path, uid) if app_sqlite_path else None
    user_tree = users_root / uid
    tree_stats = dir_stats(user_tree)
    logs_hits = grep_file_count(logs_dir, uid)
    backups_hits = grep_file_count(backups_dir, uid)

    extra_places: list[str] = []
    for t, n in sorted(tracker_counts.items()):
        if t in _ALREADY_CLEANED_TRACKER_TABLES or not n:
            continue
        extra_places.append(f"tracker.db.{t} ({n} row(s))")
    if app_hits:
        for h in app_hits:
            if h["table"] in _ALREADY_CLEANED_APP_SQLITE_TABLES:
                continue
            extra_places.append(f"app.sqlite.{h['table']}.{h['column']} ({h['count']} row(s))")
    if logs_hits:
        extra_places.append(f"logs/ ({logs_hits} file(s))")
    if backups_hits:
        extra_places.append(f"backups/ ({backups_hits} file(s))")

    return {
        "user_id": uid,
        "tracker_db": {"path": str(db_path), "tables": tracker_counts},
        "app_sqlite": (
            {"path": str(app_sqlite_path), "hits": app_hits}
            if app_sqlite_path
            else {"path": None, "note": "pass --app-sqlite PATH to include the API's own DB"}
        ),
        "users_tree": {"path": str(user_tree), **tree_stats},
        "logs": {"path": str(logs_dir), "files_mentioning_uid": logs_hits},
        "backups": {"path": str(backups_dir), "files_mentioning_uid": backups_hits},
        "extra_places_beyond_admin_delete_user": extra_places,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [f"[pii_inventory] user_id = {report['user_id']}", ""]

    lines.append(f"tracker.db ({report['tracker_db']['path']}):")
    if report["tracker_db"]["tables"]:
        for t, n in sorted(report["tracker_db"]["tables"].items()):
            cleaned = (
                " (already cleaned by admin.deleteUser)"
                if t in _ALREADY_CLEANED_TRACKER_TABLES
                else ""
            )
            lines.append(f"  {t}: {n} row(s){cleaned}")
    else:
        lines.append("  (no user_id-bearing table found, or tracker.db missing)")

    app = report["app_sqlite"]
    lines.append("")
    if app["path"]:
        lines.append(f"app.sqlite ({app['path']}):")
        if app["hits"]:
            for h in app["hits"]:
                cleaned = (
                    " (already cleaned by admin.deleteUser)"
                    if h["table"] in _ALREADY_CLEANED_APP_SQLITE_TABLES
                    else ""
                )
                lines.append(f"  {h['table']}.{h['column']}: {h['count']} row(s){cleaned}")
        else:
            lines.append("  (no hits)")
    else:
        lines.append(f"app.sqlite: {app['note']}")

    ut = report["users_tree"]
    lines.append("")
    lines.append(
        f"users tree ({ut['path']}): {ut['files']} file(s), {ut['bytes']} byte(s) "
        "(already cleaned by admin.deleteUser)"
    )

    lines.append("")
    lines.append(
        f"logs/ ({report['logs']['path']}): "
        f"{report['logs']['files_mentioning_uid']} file(s) mention the uid"
    )
    lines.append(
        f"backups/ ({report['backups']['path']}): "
        f"{report['backups']['files_mentioning_uid']} file(s) mention the uid"
    )

    lines.append("")
    extra = report["extra_places_beyond_admin_delete_user"]
    if extra:
        lines.append(f"{len(extra)} place(s) beyond admin.deleteUser's current cleanup:")
        for place in extra:
            lines.append(f"  - {place}")
    else:
        lines.append(
            "No place found beyond admin.deleteUser's current cleanup (among the checks this tool runs)."
        )

    lines.append("")
    lines.append(DECISION_RULE)
    if extra:
        lines.append(
            f"\n[pii_inventory] -> {len(extra)} extra place(s) found: the erasure milestone (M1) is mandatory."
        )
    else:
        lines.append("\n[pii_inventory] -> no extra places found in this scan.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--user", required=True, help="user id to inventory")
    parser.add_argument("--db", type=Path, default=None, help="tracker.db path override")
    parser.add_argument(
        "--app-sqlite", type=Path, default=None, help="path to job-hunter-api's app.sqlite"
    )
    parser.add_argument("--users-root", type=Path, default=None, help="USERS_ROOT override")
    parser.add_argument("--logs-dir", type=Path, default=None, help="logs/ dir override")
    parser.add_argument("--backups-dir", type=Path, default=None, help="backups/ dir override")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="no-op — this tool never writes; kept for symmetry with a future erasure tool",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    from hunter.config import PROJECT_DIR as CFG_PROJECT_DIR
    from hunter.config import TRACKER_BACKUP_DIR, TRACKER_DB_PATH, USERS_ROOT

    report = build_report(
        args.user,
        db_path=args.db or TRACKER_DB_PATH,
        app_sqlite_path=args.app_sqlite,
        users_root=args.users_root or USERS_ROOT,
        logs_dir=args.logs_dir or (CFG_PROJECT_DIR / "logs"),
        backups_dir=args.backups_dir or TRACKER_BACKUP_DIR,
    )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
