"""
Calibrate the "reuse a past generated CV for a new vacancy" idea — read-only,
zero LLM calls, zero network. Answers ONE empirical question before any reuse
machinery is built (owner's standing rule: measure first, no speculative
layers): how often does a new vacancy have a past application whose already-
generated resume would score well against the NEW posting?

Method (offline replay over the local ``Applications/**`` corpus):

  1. Load every application folder that has both ``job_posting.txt`` and
     ``content.json`` (dual-apply shadow subfolders — a directory named after
     an ``llm_profiles`` profile — are excluded; they are comparison runs, not
     delivered CVs). Order chronologically by date folder + mtime.
  2. For each vacancy, look only at STRICTLY EARLIER applications (no
     lookahead — this simulates what a reuse gate would actually have seen on
     that day) and rank them by TF-IDF cosine similarity of the two postings.
  3. Take the top-K most similar donors and score each donor's resume against
     the NEW posting with the exact same deterministic checker production
     uses (``hunter.ats_checker.check(run_llm_review=False)`` — keyword 75% +
     TF-IDF 25%, the same numbers the ATS loop iterates on). Keep the best.
  4. Compare that donor score with the vacancy's ACTUAL resume re-scored the
     same way (recomputed, not read from content.json, so both sides use one
     yardstick — the stored score may predate the verdict-refine loop).

Report: best-donor similarity distribution, and for several similarity
thresholds the hit rate + how often the donor CV would have been "good
enough" (score >= actual-2, and score >= 85). Plus a clearly-labelled cost
projection from tracker.db's ``cost_usd`` column when the DB is present.

Interpretation guide (this is the decision input, not a decision):
  - High hit rate at sim >= 0.5 AND high donor-adequacy -> a warm-start /
    reuse gate is worth building; the report's threshold table says where to
    put the production cutoff.
  - Low hit rate or donors consistently scoring below the actual CVs -> the
    vacancy stream is too diverse, close the idea with numbers on hand.

Never writes to tracker.db, the Sheet, or any application folder. The only
optional write is ``--json PATH`` (per-vacancy rows for further analysis).

Usage (run where the corpus lives — the deploy host / container):
    python tools/reuse_calibrate.py
    docker compose exec job-hunter python tools/reuse_calibrate.py
    python tools/reuse_calibrate.py --min-donor-verdict 85 --json rows.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter import ats_checker  # noqa: E402
from hunter.llm_profiles import PROFILES  # noqa: E402

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_POSTING_CHARS = 200
_SIM_THRESHOLDS = (0.35, 0.50, 0.65, 0.80)
_SIM_BANDS = ((0.0, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 1.01))
# "Donor is good enough" = within this many points of the actually-generated
# CV's deterministic score. 2 pts is below the checker's own sensitivity to
# harmless wording differences.
_ADEQUATE_TOLERANCE = 2.0
_ADEQUATE_FLOOR = 85.0
# Assumed fraction of a vacancy's LLM cost a reuse hit saves (generation +
# most refine rounds; judge/verdict Haiku calls still run). Projection only.
_ASSUMED_SAVING_FRACTION = 0.6


@dataclass
class AppEntry:
    folder: Path
    company: str
    title: str
    day: str  # YYYY-MM-DD (folder name, or mtime-derived fallback)
    mtime: float
    posting: str
    resume_text: str
    verdict: float | None  # independent PDF verdict stored in content.json


@dataclass
class RowResult:
    entry: AppEntry
    donor: AppEntry
    similarity: float
    donor_score: float
    actual_score: float

    @property
    def adequate(self) -> bool:
        return (
            self.donor_score >= self.actual_score - _ADEQUATE_TOLERANCE
            or self.donor_score >= _ADEQUATE_FLOOR
        )


def _strip_header(text: str) -> str:
    """Drop the ``URL:`` / ``Post:`` header lines job_posting.txt starts with —
    production scored the fetched text, not the file header."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith(("URL:", "Post:")) or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:]).strip()


def _resume_text(content: dict) -> str:
    """Flatten resume_en exactly the way the production ATS loop does."""
    resume_en = content.get("resume_en", "")
    if not resume_en:
        return ""
    if isinstance(resume_en, dict):
        return json.dumps(resume_en, ensure_ascii=False)
    return str(resume_en)


def _stored_verdict(content: dict) -> float | None:
    v = content.get("ats_verdict")
    if isinstance(v, dict):
        v = v.get("score")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_corpus(apps_dir: Path) -> tuple[list[AppEntry], dict[str, int]]:
    """All primary application folders, chronological. Returns (entries, skip
    counters). Shadow subfolders and incomplete/garbage folders are skipped."""
    skipped = {"shadow": 0, "no_posting": 0, "no_resume": 0, "short_posting": 0, "unreadable": 0}
    entries: list[AppEntry] = []
    profile_names = set(PROFILES)

    for content_path in sorted(apps_dir.rglob("content.json")):
        folder = content_path.parent
        if folder.name in profile_names:
            skipped["shadow"] += 1
            continue
        posting_path = folder / "job_posting.txt"
        if not posting_path.exists():
            skipped["no_posting"] += 1
            continue
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
            posting = _strip_header(posting_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            skipped["unreadable"] += 1
            continue
        resume_text = _resume_text(content)
        if not resume_text:
            skipped["no_resume"] += 1
            continue
        if len(posting) < _MIN_POSTING_CHARS:
            skipped["short_posting"] += 1
            continue

        mtime = content_path.stat().st_mtime
        day = ""
        for parent in folder.parents:
            if _DATE_DIR_RE.match(parent.name):
                day = parent.name
                break
        if not day:
            from datetime import datetime, timezone

            day = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")

        entries.append(
            AppEntry(
                folder=folder,
                company=str(content.get("company_name") or folder.name),
                title=str(content.get("job_title") or "?"),
                day=day,
                mtime=mtime,
                posting=posting,
                resume_text=resume_text,
                verdict=_stored_verdict(content),
            )
        )

    entries.sort(key=lambda e: (e.day, e.mtime))
    return entries, skipped


def posting_similarity(entries: list[AppEntry]):
    """Pairwise TF-IDF cosine similarity over all postings (one fit).
    Returns an NxN array-like; sim[i][j] in [0, 1]."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    mat = vec.fit_transform([e.posting for e in entries])
    return cosine_similarity(mat)


def _det_score(job_text: str, resume_text: str) -> float:
    return ats_checker.check(job_text=job_text, resume_text=resume_text, run_llm_review=False).score


def evaluate(
    entries: list[AppEntry],
    sim,
    *,
    donor_k: int,
    min_donor_verdict: float,
) -> tuple[list[RowResult], int]:
    """For each vacancy: best of the top-K most similar EARLIER donors.
    Returns (rows, n_without_donor)."""
    rows: list[RowResult] = []
    no_donor = 0
    for i in range(1, len(entries)):
        donor_idx = [
            j
            for j in range(i)
            if min_donor_verdict <= 0
            or (entries[j].verdict is not None and entries[j].verdict >= min_donor_verdict)
        ]
        if not donor_idx:
            no_donor += 1
            continue
        donor_idx.sort(key=lambda j: -sim[i][j])
        best: RowResult | None = None
        for j in donor_idx[:donor_k]:
            score = _det_score(entries[i].posting, entries[j].resume_text)
            if best is None or score > best.donor_score:
                best = RowResult(
                    entry=entries[i],
                    donor=entries[j],
                    similarity=float(sim[i][j]),
                    donor_score=score,
                    actual_score=0.0,  # filled below, once per vacancy
                )
        if best is None:  # unreachable (donor_idx non-empty), but keep ruff S101 out
            continue
        best.actual_score = _det_score(entries[i].posting, entries[i].resume_text)
        rows.append(best)
    return rows, no_donor


def _cost_stats() -> tuple[int, float] | None:
    """(rows_with_cost, median_cost_usd) from tracker.db — best-effort."""
    try:
        from hunter.db import get_db

        from hunter.config import TRACKER_DB_PATH

        # sqlite3.connect CREATES a missing file — check first so a run on a
        # machine without the DB stays genuinely read-only.
        if not Path(TRACKER_DB_PATH).exists():
            print("[reuse] tracker.db not found - no cost projection")
            return None
        with get_db() as conn:
            costs = [
                float(r["cost_usd"])
                for r in conn.execute(
                    "SELECT cost_usd FROM applications WHERE cost_usd > 0"
                ).fetchall()
            ]
        if not costs:
            return None
        return len(costs), statistics.median(costs)
    except Exception as e:  # noqa: BLE001 — the report must degrade gracefully
        print(f"[reuse] tracker.db unavailable ({e}) - no cost projection")
        return None


def print_report(rows: list[RowResult], no_donor: int, skipped: dict[str, int]) -> None:
    n = len(rows)
    print("\n=== CV reuse calibration ===")
    print(f"vacancies evaluated : {n} (+{no_donor} with no eligible earlier donor)")
    print(f"skipped folders     : {skipped}")
    if not n:
        print("Nothing to evaluate - corpus too small or donor filter too strict.")
        return

    print("\n--- best-donor similarity distribution ---")
    for lo, hi in _SIM_BANDS:
        band = [r for r in rows if lo <= r.similarity < hi]
        bar = "#" * round(40 * len(band) / n)
        print(
            f"  sim {lo:.2f}-{min(hi, 1.0):.2f} : {len(band):4d} ({100 * len(band) / n:5.1f}%) {bar}"
        )

    print("\n--- reuse viability by similarity threshold ---")
    print("  threshold |  hits  | hit% | donor avg | actual avg | donor>=85 | donor adequate")
    for th in _SIM_THRESHOLDS:
        hits = [r for r in rows if r.similarity >= th]
        if not hits:
            print(f"    >={th:.2f}   |     0  |  0.0%|     -     |     -      |     -     |     -")
            continue
        d_avg = statistics.mean(r.donor_score for r in hits)
        a_avg = statistics.mean(r.actual_score for r in hits)
        ge85 = 100 * sum(r.donor_score >= _ADEQUATE_FLOOR for r in hits) / len(hits)
        adeq = 100 * sum(r.adequate for r in hits) / len(hits)
        print(
            f"    >={th:.2f}   | {len(hits):5d}  |{100 * len(hits) / n:5.1f}%|"
            f"   {d_avg:5.1f}   |   {a_avg:5.1f}    |   {ge85:5.1f}%  |   {adeq:5.1f}%"
        )
    print(
        f"\n  ('adequate' = donor score >= actual-{_ADEQUATE_TOLERANCE:.0f}"
        f" or >= {_ADEQUATE_FLOOR:.0f}; scores are the deterministic checker"
        " - keyword 75% + TF-IDF 25% - NOT the Haiku PDF verdict)"
    )

    print("\n--- top matches (highest donor score) ---")
    for r in sorted(rows, key=lambda r: -r.donor_score)[:10]:
        print(
            f"  [{r.entry.day}] {r.entry.company[:24]:24s} <- {r.donor.company[:24]:24s}"
            f" sim={r.similarity:.2f} donor={r.donor_score:5.1f} actual={r.actual_score:5.1f}"
        )

    cost = _cost_stats()
    if cost:
        n_cost, median = cost
        hits50 = [r for r in rows if r.similarity >= 0.50 and r.adequate]
        rate = len(hits50) / n
        print("\n--- cost projection (PROJECTION, stated assumptions) ---")
        print(f"  median cost/vacancy (tracker.db, {n_cost} priced rows): ${median:.2f}")
        print(
            f"  adequate-donor rate at sim>=0.50: {100 * rate:.1f}% -> projected saving"
            f" ~${rate * median * _ASSUMED_SAVING_FRACTION:.2f}/vacancy"
            f" (assumes a reuse hit saves {int(100 * _ASSUMED_SAVING_FRACTION)}% of the run)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apps-dir",
        default=None,
        help="Applications corpus root (default: hunter.config APPLICATIONS_DIR)",
    )
    parser.add_argument(
        "--donor-k",
        type=int,
        default=3,
        help="score the K most similar earlier donors, keep the best (default 3)",
    )
    parser.add_argument(
        "--min-donor-verdict",
        type=float,
        default=0.0,
        help="only accept donors whose stored PDF verdict is >= this (0 = any donor; "
        "older content.json may have no verdict and would be excluded)",
    )
    parser.add_argument("--json", default=None, help="also dump per-vacancy rows to this path")
    args = parser.parse_args()

    if args.apps_dir:
        apps_dir = Path(args.apps_dir)
    else:
        from hunter.config import APPLICATIONS_DIR

        apps_dir = Path(APPLICATIONS_DIR)
    if not apps_dir.is_dir():
        print(f"[reuse] corpus dir not found: {apps_dir}")
        return 1

    print(f"[reuse] loading corpus from {apps_dir} ...")
    entries, skipped = load_corpus(apps_dir)
    print(f"[reuse] {len(entries)} applications loaded")
    if len(entries) < 2:
        print("[reuse] need at least 2 applications to calibrate - nothing to do")
        return 1

    sim = posting_similarity(entries)
    rows, no_donor = evaluate(
        entries, sim, donor_k=args.donor_k, min_donor_verdict=args.min_donor_verdict
    )
    print_report(rows, no_donor, skipped)

    if args.json:
        payload = [
            {
                "day": r.entry.day,
                "company": r.entry.company,
                "title": r.entry.title,
                "folder": str(r.entry.folder),
                "donor_company": r.donor.company,
                "donor_folder": str(r.donor.folder),
                "similarity": round(r.similarity, 4),
                "donor_score": r.donor_score,
                "actual_score": r.actual_score,
                "adequate": r.adequate,
            }
            for r in rows
        ]
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[reuse] per-vacancy rows written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
