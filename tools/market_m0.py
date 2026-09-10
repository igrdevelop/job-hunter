"""
tools/market_m0.py — market-aggregate M0 measurement: is the local
Applications/ corpus stable enough to build a "what's in demand" report from?

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M3.0. Before building any
`postings_seen` / `demand_profile` infrastructure (M3.1 — explicitly NOT in
scope here), answer one question for $0: if we split the corpus in half
chronologically, does the top-30 term list per (role_family x region) cell
agree between the two halves? If it does not, the corpus is too small/noisy
for a term-share aggregate to mean anything yet.

Method:
  1. Load every job_posting.txt under Applications/** (shadow dual-apply
     subfolders excluded — comparison runs, not distinct postings).
  2. Dedup: exact text hash first, then a TF-IDF cosine >= 0.94 collapse
     (same idea as tools/reuse_calibrate.py's posting_similarity) — a
     re-post / cross-board duplicate must count once.
  3. Cell = role_family (regex over the folder's content.json job_title, or
     the posting's own first line) x region. Region is read from the
     existing loaders — candidate.yaml home-city aliases, hunter.filters'
     Polish/foreign anti-hybrid city sets, and hunter.sources.text_utils
     REMOTE_ANY — never a hardcoded city list of its own.
  4. Terms = hunter.ats_checker.extract_job_keywords (canonical tech/soft
     keywords, no floor) unioned with a TfidfVectorizer(ngram_range=(1,3),
     min_df=3, EN+PL stopwords) vocabulary fitted per half — this widens the
     vocabulary beyond the fixed keyword regex.
  5. For every cell with n >= 30: split chronologically into two halves,
     take each half's top-30 terms by share (share of postings mentioning
     the term), and compare: Jaccard of the two top-30 sets, Spearman rank
     correlation over their union (implemented by hand — no scipy), and
     held-out coverage (mean share of a posting's own terms present in the
     OTHER half's top-30).

Decision rule and bias caveat are printed verbatim from the plan (see
--decision-only to print just that).

Usage (run where the Applications/ corpus lives — the deploy host):
    python tools/market_m0.py
    docker compose exec -T job-hunter python tools/market_m0.py --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_POSTING_CHARS = 200
_MIN_CELL_SIZE = 30
_TOP_N = 30
_COSINE_DEDUP_THRESHOLD = 0.94

_DECISION_RULE = (
    "Jaccard ≥ 0.7 и Spearman ≥ 0.6 хотя "
    "бы для «angular × poland-remote» "
    "→ агрегат стабилен, "
    "строим; иначе укрупнить "
    "ячейки/окно."
)
_BIAS_CAVEAT = "корпус это прошедшие фильтр вакансии одного кандидата, смещение."

# Small, deliberately conservative Polish stopword list — sklearn ships an
# English list but not a Polish one; this is only meant to keep the most
# common function words out of the term-share ranking, not to be exhaustive.
_PL_STOPWORDS = [
    "i",
    "w",
    "z",
    "na",
    "do",
    "dla",
    "oraz",
    "lub",
    "się",
    "sie",
    "jest",
    "są",
    "sa",
    "to",
    "który",
    "ktora",
    "która",
    "które",
    "ktore",
    "praca",
    "pracy",
    "firma",
    "firmy",
    "zespół",
    "zespol",
    "projekt",
    "projektu",
    "osoby",
    "osobą",
    "osoba",
    "naszym",
    "nasz",
    "nasza",
    "nasze",
    "być",
    "byc",
    "masz",
    "mamy",
    "od",
    "po",
    "przez",
    "jako",
    "co",
    "jak",
    "aby",
    "gdzie",
    "tym",
    "tego",
    "tej",
    "te",
]


# ── Pure: role-family + region classification ──────────────────────────────

_ROLE_FAMILY_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("angular", re.compile(r"\bangular\b", re.IGNORECASE)),
    ("react", re.compile(r"\breact\b", re.IGNORECASE)),
    ("fullstack", re.compile(r"\bfull[\s-]?stack\b", re.IGNORECASE)),
    ("frontend-generic", re.compile(r"\bfront[\s-]?end\b", re.IGNORECASE)),
)


def role_family_from_title(title: str) -> str:
    for name, pattern in _ROLE_FAMILY_RULES:
        if pattern.search(title or ""):
            return name
    return "other"


def classify_region(text: str, city_sets: dict[str, frozenset[str]]) -> str:
    """city_sets: {'home', 'poland_other', 'abroad', 'remote'} -> frozenset of
    lowercase tokens, as produced by load_city_sets()."""
    blob = (text or "").lower()
    if any(c in blob for c in city_sets.get("home", ())):
        return "home"
    if any(c in blob for c in city_sets.get("poland_other", ())):
        return "poland-other"
    if any(c in blob for c in city_sets.get("abroad", ())):
        return "abroad"
    if any(tok in blob for tok in city_sets.get("remote", ())):
        return "remote"
    return "unknown"


def load_city_sets() -> dict[str, frozenset[str]]:
    """Region vocabulary from the EXISTING loaders — candidate.yaml home city,
    hunter.filters' Polish/foreign anti-hybrid city sets, and the REMOTE_ANY
    token set. No hardcoded city names live here."""
    from hunter import candidate
    from hunter.filters import FILTER, _PL_ANTI_HYBRID_CITIES, _anti_hybrid_cities
    from hunter.sources.text_utils import REMOTE_ANY

    home_city = str(candidate.get("location.home_city", "") or "").strip().lower()
    home_aliases = {str(a).lower() for a in (candidate.get("location.home_city_aliases", []) or [])}
    if home_city:
        home_aliases.add(home_city)
    home_aliases_fs = frozenset(home_aliases)

    pl_cities = frozenset(_PL_ANTI_HYBRID_CITIES) - home_aliases_fs
    all_anti_hybrid = frozenset(_anti_hybrid_cities(FILTER))
    foreign_cities = all_anti_hybrid - frozenset(_PL_ANTI_HYBRID_CITIES) - home_aliases_fs
    remote_tokens = frozenset(t.lower() for t in REMOTE_ANY)

    return {
        "home": home_aliases_fs,
        "poland_other": pl_cities,
        "abroad": foreign_cities,
        "remote": remote_tokens,
    }


# ── I/O: corpus loading ─────────────────────────────────────────────────────


@dataclass
class PostingEntry:
    path: Path
    day: str
    title: str
    text: str


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_postings(root: Path) -> list[PostingEntry]:
    from hunter.llm_profiles import PROFILES

    profile_names = set(PROFILES)
    entries: list[PostingEntry] = []
    if not root.exists():
        return entries

    for posting_path in sorted(root.rglob("job_posting.txt")):
        folder = posting_path.parent
        if folder.name in profile_names:
            continue
        try:
            text = posting_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text.strip()) < _MIN_POSTING_CHARS:
            continue

        title = ""
        content_path = folder / "content.json"
        if content_path.exists():
            try:
                content = json.loads(content_path.read_text(encoding="utf-8"))
                title = str(content.get("job_title") or "")
            except (OSError, json.JSONDecodeError):
                pass
        if not title:
            title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")

        day = ""
        for parent in folder.parents:
            if _DATE_DIR_RE.match(parent.name):
                day = parent.name
                break
        if not day:
            from datetime import datetime, timezone

            day = datetime.fromtimestamp(posting_path.stat().st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )

        entries.append(PostingEntry(path=folder, day=day, title=title, text=text))

    entries.sort(key=lambda e: (e.day, str(e.path)))
    return entries


def dedup_postings(
    entries: list[PostingEntry], *, cosine_threshold: float = _COSINE_DEDUP_THRESHOLD
) -> list[PostingEntry]:
    """Exact-hash dedup, then a greedy TF-IDF-cosine collapse (kept: earliest
    chronological occurrence of each near-duplicate cluster). `entries` must
    already be chronologically sorted."""
    seen_hash: set[str] = set()
    hash_deduped: list[PostingEntry] = []
    for e in entries:
        h = text_hash(e.text)
        if h in seen_hash:
            continue
        seen_hash.add(h)
        hash_deduped.append(e)

    if len(hash_deduped) < 2:
        return hash_deduped

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    mat = vec.fit_transform([e.text for e in hash_deduped])
    sim = cosine_similarity(mat)

    kept_idx: list[int] = []
    for i in range(len(hash_deduped)):
        if any(sim[i][j] >= cosine_threshold for j in kept_idx):
            continue
        kept_idx.append(i)
    return [hash_deduped[i] for i in kept_idx]


def group_cells(
    entries: list[PostingEntry], city_sets: dict[str, frozenset[str]]
) -> dict[tuple[str, str], list[PostingEntry]]:
    cells: dict[tuple[str, str], list[PostingEntry]] = {}
    for e in entries:
        key = (role_family_from_title(e.title), classify_region(e.text, city_sets))
        cells.setdefault(key, []).append(e)
    for lst in cells.values():
        lst.sort(key=lambda e: (e.day, str(e.path)))
    return cells


# ── Pure: term shares + top-N ───────────────────────────────────────────────


def compute_term_shares(texts: list[str]) -> dict[str, float]:
    """term -> share of `texts` containing it. Union of (a) canonical
    ats_checker.extract_job_keywords terms per document (no floor) and
    (b) a TfidfVectorizer(ngram_range=(1,3), min_df=3) vocabulary fitted over
    `texts` (EN+PL stopwords) — widens coverage beyond the fixed regex.
    Per-document union avoids double counting a term found by both."""
    n = len(texts)
    if n == 0:
        return {}

    from hunter import ats_checker

    doc_terms: list[set[str]] = [
        {kw.lower() for kw in ats_checker.extract_job_keywords(t)} for t in texts
    ]

    try:
        from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

        stop_words = sorted(set(ENGLISH_STOP_WORDS) | set(_PL_STOPWORDS))
        vec = TfidfVectorizer(ngram_range=(1, 3), min_df=3, stop_words=stop_words)
        matrix = vec.fit_transform(texts)
        vocab = vec.get_feature_names_out()
        presence = matrix.toarray() > 0
        for i in range(n):
            row_terms = {vocab[j] for j in range(len(vocab)) if presence[i, j]}
            doc_terms[i] |= row_terms
    except (ImportError, ValueError):
        # ValueError: e.g. empty vocabulary after min_df filtering on a tiny
        # corpus — degrade to keyword-extractor terms only.
        pass

    counts: Counter[str] = Counter()
    for terms in doc_terms:
        counts.update(terms)
    return {term: count / n for term, count in counts.items()}


def top_n_terms(shares: dict[str, float], n: int = _TOP_N) -> list[str]:
    return [t for t, _ in sorted(shares.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def jaccard_index(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def _rank(values: list[float]) -> list[float]:
    """Average rank (1 = smallest), ties get the mean of their tied ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_corr(x: list[float], y: list[float]) -> float:
    """Hand-rolled Spearman rank correlation (no scipy). x/y must be aligned
    (same term order)."""
    n = len(x)
    if n < 2:
        return 1.0
    rx, ry = _rank(x), _rank(y)
    mean_rx, mean_ry = sum(rx) / n, sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry, strict=True))
    var_x = sum((a - mean_rx) ** 2 for a in rx)
    var_y = sum((b - mean_ry) ** 2 for b in ry)
    if var_x == 0 or var_y == 0:
        return 1.0 if var_x == var_y else 0.0
    return cov / math.sqrt(var_x * var_y)


def held_out_coverage(texts: list[str], other_half_top: list[str]) -> list[float]:
    """Per-posting share of ITS OWN keyword-extractor terms present in the
    other half's top-N. Returns the raw list (caller averages/weights)."""
    from hunter import ats_checker

    other_set = set(other_half_top)
    out = []
    for t in texts:
        terms = {kw.lower() for kw in ats_checker.extract_job_keywords(t)}
        if not terms:
            continue
        out.append(len(terms & other_set) / len(terms))
    return out


def split_halves(entries: list[PostingEntry]) -> tuple[list[PostingEntry], list[PostingEntry]]:
    mid = len(entries) // 2
    return entries[:mid], entries[mid:]


def analyze_cell(entries: list[PostingEntry]) -> dict:
    """Pure over already-loaded entries."""
    half1, half2 = split_halves(entries)
    texts1 = [e.text for e in half1]
    texts2 = [e.text for e in half2]

    shares1 = compute_term_shares(texts1)
    shares2 = compute_term_shares(texts2)
    top1 = top_n_terms(shares1)
    top2 = top_n_terms(shares2)

    jac = jaccard_index(set(top1), set(top2))
    union_terms = sorted(set(top1) | set(top2))
    x = [shares1.get(t, 0.0) for t in union_terms]
    y = [shares2.get(t, 0.0) for t in union_terms]
    spear = spearman_corr(x, y)

    cov1 = held_out_coverage(texts1, top2)
    cov2 = held_out_coverage(texts2, top1)
    all_cov = cov1 + cov2
    coverage = sum(all_cov) / len(all_cov) if all_cov else 0.0

    return {
        "n": len(entries),
        "n_half1": len(half1),
        "n_half2": len(half2),
        "top1": top1,
        "top2": top2,
        "jaccard": jac,
        "spearman": spear,
        "held_out_coverage": coverage,
        "stable": jac >= 0.7 and spear >= 0.6,
    }


# ── Report ───────────────────────────────────────────────────────────────────


def print_report(
    cells: dict[tuple[str, str], list[PostingEntry]], results: dict[tuple[str, str], dict]
) -> None:
    print("\n=== Market-aggregate M0 (corpus stability probe) ===")
    total = sum(len(v) for v in cells.values())
    print(f"deduped postings: {total} across {len(cells)} cell(s)")

    too_small = {k: len(v) for k, v in cells.items() if len(v) < _MIN_CELL_SIZE}
    if too_small:
        print(f"\n{len(too_small)} cell(s) below n={_MIN_CELL_SIZE} (skipped):")
        for k, n in sorted(too_small.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {k}: n={n}")

    if not results:
        print("\nNo cell reached the n>=30 floor — nothing to compare.")
    else:
        print(f"\n--- cells with n >= {_MIN_CELL_SIZE} ---")
        for key, r in sorted(results.items(), key=lambda kv: -kv[1]["n"]):
            role_family, region = key
            status = "STABLE" if r["stable"] else "unstable"
            print(
                f"  {role_family:18s} x {region:14s}  n={r['n']:4d} "
                f"(halves {r['n_half1']}/{r['n_half2']})  "
                f"jaccard={r['jaccard']:.2f} spearman={r['spearman']:.2f} "
                f"coverage={100 * r['held_out_coverage']:.1f}%  [{status}]"
            )

    print("\n--- decision rule (verbatim, docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M3.0) ---")
    print(_DECISION_RULE)
    print("\n--- bias caveat (verbatim) ---")
    print(_BIAS_CAVEAT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root",
        default=None,
        help="Applications corpus root (default: hunter.config APPLICATIONS_DIR)",
    )
    parser.add_argument(
        "--cosine-threshold",
        type=float,
        default=_COSINE_DEDUP_THRESHOLD,
        help=f"near-duplicate collapse threshold (default {_COSINE_DEDUP_THRESHOLD})",
    )
    parser.add_argument("--json", default=None, help="also dump per-cell results to this path")
    args = parser.parse_args()

    if args.root:
        root = Path(args.root)
    else:
        from hunter.config import APPLICATIONS_DIR

        root = Path(APPLICATIONS_DIR)
    if not root.is_dir():
        print(f"[market-m0] corpus dir not found: {root}")
        return 1

    print(f"[market-m0] loading corpus from {root} ...")
    entries = load_postings(root)
    print(f"[market-m0] {len(entries)} posting(s) loaded")
    if len(entries) < 2:
        print("[market-m0] need at least 2 postings — nothing to do")
        return 1

    deduped = dedup_postings(entries, cosine_threshold=args.cosine_threshold)
    print(
        f"[market-m0] {len(deduped)} posting(s) after dedup (hash + cosine>={args.cosine_threshold})"
    )

    city_sets = load_city_sets()
    cells = group_cells(deduped, city_sets)
    results = {
        key: analyze_cell(ents) for key, ents in cells.items() if len(ents) >= _MIN_CELL_SIZE
    }

    print_report(cells, results)

    if args.json:
        payload = {
            "cells": {f"{k[0]}|{k[1]}": len(v) for k, v in cells.items()},
            "results": {f"{k[0]}|{k[1]}": v for k, v in results.items()},
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[market-m0] results written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
