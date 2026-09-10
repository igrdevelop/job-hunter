"""
tools/backfill_runs.py — backfill hunter.metrics.generation_runs from the
existing Applications/**/content.json corpus (docs/improvement-2026-09/
08-DATA_EVAL_PLAN.md M1).

`hunter/metrics.py` only started recording generation_runs going forward —
every application generated before that lands in this file with no
generation_runs row. This tool walks the on-disk corpus and derives what it
can from each content.json (ATS scores, verdict history, cost, posting
language, the reused-donor marker) into one generation_runs row per folder,
with `pipeline='backfill'` and `started_at=NULL` so a backfilled row is never
mistaken for one the live pipeline actually timed.

    python tools/backfill_runs.py [--root Applications] [--db tracker.db] [--dry-run]

Idempotent: the run_id for a folder is derived deterministically from its
path (sha1 hash), so re-running the tool skips folders already backfilled
instead of duplicating or re-writing their row. `--dry-run` reports what
would be written without touching the database.

Shadow subfolders (`Applications/<date>/<Company>/<profile-name>/`, one of
`hunter.llm_profiles.PROFILES`) are excluded — they are dual-apply A/B
comparisons, not applications, and never had their own tracker row either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter import metrics  # noqa: E402 — after sys.path setup
from hunter.db import get_db  # noqa: E402
from hunter.tracker import normalize_url  # noqa: E402

# Force UTF-8 stdout/stderr on Windows (console defaults to cp1252) — same
# guard as tools/render_profile.py / tools/parse_resume.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _run_id_for_folder(folder: Path) -> str:
    """Deterministic run_id from the folder path — makes re-running this
    tool idempotent without a separate "already backfilled" ledger."""
    digest = hashlib.sha1(str(folder.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"bf_{digest[:16]}"


def _shadow_profile_names() -> set[str]:
    try:
        from hunter.llm_profiles import PROFILES

        return set(PROFILES)
    except Exception:  # noqa: BLE001 — a broken import must not crash the scan
        return set()


def iter_content_json_folders(root: Path) -> list[Path]:
    """Every non-shadow Applications/<date>/<Company>[/...] folder holding a
    content.json, sorted for deterministic output."""
    if not root.exists():
        return []
    shadow_names = _shadow_profile_names()
    found: list[Path] = []
    for content_path in root.rglob("content.json"):
        folder = content_path.parent
        if folder.name in shadow_names:
            continue  # dual-apply shadow comparison, not an application
        found.append(folder)
    return sorted(found)


def _round_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def derive_run_fields(content: dict, folder: Path) -> dict:
    """Map one content.json's fields onto the generation_runs column set
    understood by hunter.metrics.ALLOWED_RUN_FIELDS."""
    ats_check = content.get("ats_check") or {}
    ats_check_pdf = content.get("ats_check_pdf") or {}
    ats_verdict = content.get("ats_verdict") or {}
    verdict_history = content.get("verdict_history") or []
    cost = content.get("cost") or {}

    verdict_final = _round_or_none(ats_verdict.get("score"))
    if verdict_history:
        verdict_first = _round_or_none(verdict_history[0].get("score_before"))
    else:
        verdict_first = verdict_final

    accepted_rounds = [h for h in verdict_history if h.get("outcome") == "accepted"]
    best_round_kind = accepted_rounds[-1].get("kind") if accepted_rounds else None

    apply_url = content.get("apply_url") or ""
    url_norm = normalize_url(apply_url) if apply_url else ""

    has_docs = any(folder.glob("*.pdf")) or any(folder.glob("*.docx"))
    reused_donor = content.get("reused_from")
    if reused_donor:
        outcome = "reused_repost"
    elif has_docs:
        outcome = "ok"
    else:
        outcome = "no_docs"

    fields: dict = {
        "url_norm": url_norm,
        "pipeline": "backfill",
        "posting_lang": content.get("primary_lang") or "",
        "ats_pre_score": _round_or_none(ats_check.get("score")),
        "ats_pre_keyword": _round_or_none(ats_check.get("keyword_score")),
        "ats_pdf_score": _round_or_none(ats_check_pdf.get("score")),
        "verdict_first": verdict_first,
        "verdict_final": verdict_final,
        "refine_rounds": len(verdict_history) or None,
        "refine_accepted": len(accepted_rounds) if verdict_history else None,
        "best_round_kind": best_round_kind,
        "reused_donor": str(reused_donor) if reused_donor else None,
        "cost_usd": _round_or_none(cost.get("total_usd")),
        "outcome": outcome,
    }
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def _match_applications_row(db_path: Path, url_norm: str, folder: Path) -> tuple[str, str] | None:
    """Best-effort lookup of (row_id, user_id) in the applications table by
    url_norm first, falling back to an exact folder-path match (both store
    the full on-disk path, per hunter/tracker.py's schema doc)."""
    try:
        with get_db(db_path) as conn:
            row = None
            if url_norm:
                row = conn.execute(
                    "SELECT id, user_id FROM applications WHERE url_norm = ? LIMIT 1",
                    (url_norm,),
                ).fetchone()
            if row is None:
                folder_str = str(folder).replace("\\", "/")
                row = conn.execute(
                    "SELECT id, user_id FROM applications WHERE folder = ? LIMIT 1",
                    (folder_str,),
                ).fetchone()
            if row is None:
                return None
            return row["id"], row["user_id"] or ""
    except Exception:  # noqa: BLE001 — matching is a bonus, never fatal
        return None


def _existing_run_ids(db_path: Path) -> set[str]:
    """run_ids already in generation_runs, or an empty set if the table
    doesn't exist yet (a bare/never-touched tracker.db) — the table is
    created lazily by the first metrics.start_run() call below."""
    try:
        with get_db(db_path) as conn:
            return {r["run_id"] for r in conn.execute("SELECT run_id FROM generation_runs")}
    except Exception:  # noqa: BLE001 — treat an unreadable table as "nothing backfilled yet"
        return set()


def backfill(root: Path, db_path: Path, *, dry_run: bool = False) -> dict:
    """Walk `root` and write one generation_runs row per new folder found.

    Returns a summary dict: {"scanned", "skipped_existing", "written", "errors"}.
    """
    # hunter.metrics reads its module-level DB_PATH on every call (mirrors
    # hunter.source_health / hunter.best_effort's own DB_PATH constant) — set
    # it once so start_run()/update_run() below land in the requested --db.
    metrics.DB_PATH = db_path

    folders = iter_content_json_folders(root)
    existing = _existing_run_ids(db_path)

    scanned = 0
    skipped_existing = 0
    written = 0
    errors: list[str] = []

    for folder in folders:
        scanned += 1
        run_id = _run_id_for_folder(folder)
        if run_id in existing:
            skipped_existing += 1
            continue

        content_path = folder / "content.json"
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            errors.append(f"{content_path}: {e}")
            continue

        fields = derive_run_fields(content, folder)
        match = _match_applications_row(db_path, fields.get("url_norm", ""), folder)
        if match:
            fields["row_id"], fields["user_id"] = match

        if dry_run:
            written += 1
            continue

        # start_run() always stamps started_at with "now" (there is no
        # "unknown" sentinel in its signature — every LIVE caller knows when
        # its run started). A backfilled row does NOT know its real start
        # time, so immediately null it back out via update_run() — which,
        # unlike start_run(), takes started_at at face value.
        metrics.start_run(run_id=run_id, pipeline="backfill")
        update_fields = {k: v for k, v in fields.items() if k != "pipeline"}
        update_fields["started_at"] = None
        metrics.update_run(run_id, **update_fields)
        written += 1

    return {
        "scanned": scanned,
        "skipped_existing": skipped_existing,
        "written": written,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    from hunter.config import APPLICATIONS_DIR, TRACKER_DB_PATH

    parser = argparse.ArgumentParser(
        description="Backfill hunter.metrics.generation_runs from Applications/**/content.json."
    )
    parser.add_argument(
        "--root", type=Path, default=APPLICATIONS_DIR, help="Applications/ root to scan."
    )
    parser.add_argument("--db", type=Path, default=TRACKER_DB_PATH, help="tracker.db path.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be written; write nothing."
    )
    args = parser.parse_args(argv)

    result = backfill(args.root, args.db, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
