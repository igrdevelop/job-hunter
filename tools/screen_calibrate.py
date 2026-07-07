"""
Calibrate the doomed-vacancy gate (hunter.filters.assess_job_text) against real
job postings — see docs/DOOMED_GATE_PLAN.md, milestone M4.

Two data sources, read-only, combined into one report:

  1. Offline corpus (no network): every ``Applications/**/job_posting.txt`` on
     this machine. Company name comes from the parent folder; the URL (when the
     saved file has a ``URL: ...`` header line) is normalised and cross-checked
     against the live Sheet's ``Sent`` column for ground truth.

  2. Live spot-check (``--live``): a sample of non-LinkedIn URLs from the
     Google Sheets tracker with a real ``Sent`` date in the last ``--days``
     days (default 45), fetched fresh via ``hunter.sources.fetch_job_text``.
     Tolerant of dead links / anti-bot blocks — failures are counted and
     skipped, never raised. A ``--delay`` pause runs between fetches.

For every posting this prints one line per finding
(``company | rule | severity | evidence | owner_note``) plus a summary, and —
this is the acceptance bar from the plan — calls out every HARD finding on a
row the owner actually sent (Sent = a real date): each one is a false positive
that must be fixed by narrowing the offending regex before the PR is ready.

Never writes to the Sheet, the local tracker, or any job_posting.txt.

Usage:
    python tools/screen_calibrate.py                    # offline corpus only
    python tools/screen_calibrate.py --live              # + live Sheet sample
    python tools/screen_calibrate.py --live --limit 20 --days 45
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter.expired_check import is_job_expired  # noqa: E402
from hunter.filters import GateFinding, assess_job_text  # noqa: E402
from hunter.sent_parse import classify, parse_sent_date  # noqa: E402
from hunter.tracker import normalize_url  # noqa: E402

# Hosts that hard-block anonymous/no-session fetches locally (429/Cloudflare) —
# skip in the live sample rather than burn the run on guaranteed failures.
_SKIP_LIVE_HOSTS = ("linkedin.com",)

# Rules whose HARD hits on an old Sent row are pre-existing, deliberate policy
# — not a regex-precision bug to narrow — for two different reasons:
#
#   - is_ai_training_or_mill / ai_mill_body: an exact-name lookup against the
#     owner-curated exclude_companies blocklist (PR #110, 2026-06-30; ai_mill_body
#     is the same blocklist re-checked against the FULL body text, PR #118,
#     2026-07-07 — company field is blank for Gmail-alert stubs). A hit on a
#     Sent row predating that decision (e.g. Micro1, 2026-05-13/2026-06-19) is
#     stale ground truth, not a false positive.
#   - is_unwanted_fullstack / title_exclude_pattern: title-DEPENDENT rules that
#     silently never fired for any non-JobLeads job before this same PR
#     (docs/DOOMED_GATE_PASTE_PLAN.md) — apply_service.py only ever passed
#     --company/--title for jobleads.com URLs, so run_doomed_gate always saw
#     title="" for every other source and both rules were dead code in
#     production. Now that every auto-hunt job carries its real title, they
#     correctly fire for the first time on old Sent rows that only slipped
#     through via that plumbing gap (Unide "Senior Fullstack Developer (Node)"
#     — no Angular, matches the existing fullstack policy exactly; BCFSoftware
#     "... (Tech Lead) M/K" — matches the existing \btech\s+lead\b exclude
#     pattern). Both are the EXISTING, already-owner-approved policy working
#     correctly, not new imprecision — narrowing them would only re-open the
#     gap. New findings from these rules should still be scrutinized normally;
#     this bucket exists only to not re-litigate old, now-unreachable rows.
_PRE_EXISTING_POLICY_RULES = frozenset({
    "is_ai_training_or_mill", "ai_mill_body", "is_unwanted_fullstack", "title_exclude_pattern",
})


@dataclass
class Posting:
    source: str  # "offline" | "live"
    company: str
    title: str
    url: str
    owner_note: str  # Sheet's Sent value, "" if unknown
    text: str
    stale: bool = False  # re-fetched live text now shows the posting as expired —
    # in production expired_check.is_job_expired() already skips this BEFORE the
    # doomed gate runs, so a hard finding here is a re-fetch artifact, not a real
    # false positive (see BigbearAI/Megaport calibration notes in the PR).


@dataclass
class ReportRow:
    company: str
    rule: str
    severity: str
    evidence: str
    owner_note: str
    source: str
    url: str
    stale: bool = False


# ---------------------------------------------------------------------------
# Sheet ground truth (best-effort — calibration still runs offline without it)
# ---------------------------------------------------------------------------

def _load_sheet_rows() -> list[dict]:
    try:
        from hunter import gsheets_sync
        from hunter.gsheets_client import read_all

        service = gsheets_sync._get_service()
        if service is None:
            print("[calibrate] Sheets unavailable (GSHEETS_ENABLED/token) — no owner-note cross-check")
            return []
        gsheets_sync._state = gsheets_sync._read_state()
        sheet_id = gsheets_sync._sheet_id()
        if not sheet_id:
            print("[calibrate] No spreadsheet id configured — no owner-note cross-check")
            return []
        rows = [row for _idx, row in read_all(service, sheet_id, tab="Tracker")]
        print(f"[calibrate] Loaded {len(rows)} rows from the Sheet for ground-truth cross-check")
        return rows
    except Exception as e:  # noqa: BLE001 — calibration must degrade gracefully
        print(f"[calibrate] Could not read Sheet ({e}) — no owner-note cross-check")
        return []


def _sent_index(sheet_rows: list[dict]) -> dict[str, str]:
    """url_norm -> Sent value, for the offline-corpus cross-check."""
    idx: dict[str, str] = {}
    for row in sheet_rows:
        url = (row.get("URL") or "").strip()
        if not url:
            continue
        idx[normalize_url(url)] = row.get("Sent", "")
    return idx


def _title_index(sheet_rows: list[dict]) -> dict[str, str]:
    """url_norm -> Job Title, for the offline-corpus cross-check.

    Mirrors what production now does after the apply_service.py fix
    (docs/DOOMED_GATE_PASTE_PLAN.md): every auto-hunt job passes its real
    title into the doomed gate, so offline replay should too when the Sheet
    has one — only a genuinely title-less row (no Sheet match, i.e. the same
    situation as a real manual paste) should exercise the title-guessing
    fallback. Without this, EVERY offline corpus file looks like an unknown-
    title paste, which is not what production actually does.
    """
    idx: dict[str, str] = {}
    for row in sheet_rows:
        url = (row.get("URL") or "").strip()
        title = (row.get("Job Title") or "").strip()
        if not url or not title:
            continue
        idx[normalize_url(url)] = title
    return idx


# ---------------------------------------------------------------------------
# 1. Offline corpus
# ---------------------------------------------------------------------------

def _extract_url(text: str) -> str:
    for line in text.splitlines()[:3]:
        line = line.strip()
        if line.startswith("URL:"):
            return line[len("URL:"):].strip()
    return ""


def load_offline_corpus(
    sent_index: dict[str, str], title_index: dict[str, str], base_dirs: list[Path],
) -> list[Posting]:
    postings: list[Posting] = []
    for base_dir in base_dirs:
        if not base_dir.exists():
            print(f"[calibrate] {base_dir} does not exist — skipping")
            continue
        found = 0
        for path in sorted(base_dir.rglob("job_posting.txt")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"[calibrate] could not read {path}: {e}")
                continue
            company = path.parent.name
            url = _extract_url(text)
            url_norm = normalize_url(url) if url else ""
            owner_note = sent_index.get(url_norm, "") if url else ""
            title = title_index.get(url_norm, "") if url else ""
            postings.append(Posting(
                source="offline", company=company, title=title, url=url,
                owner_note=owner_note, text=text, stale=is_job_expired(text),
            ))
            found += 1
        print(f"[calibrate] Offline corpus: {found} job_posting.txt file(s) under {base_dir}")
    return postings


# ---------------------------------------------------------------------------
# 2. Live Sheet spot-check
# ---------------------------------------------------------------------------

def _candidate_live_rows(sheet_rows: list[dict], days: int) -> list[dict]:
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=days)
    seen_urls: set[str] = set()
    candidates: list[dict] = []
    for row in sheet_rows:
        sent = row.get("Sent", "")
        if classify(sent) != "applied":
            continue
        sent_date = parse_sent_date(sent)
        if sent_date is None or sent_date < cutoff:
            continue
        url = (row.get("URL") or "").strip()
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if any(h in host for h in _SKIP_LIVE_HOSTS):
            continue
        norm = normalize_url(url)
        if norm in seen_urls:
            continue
        seen_urls.add(norm)
        candidates.append(row)
    return candidates


def load_live_sample(sheet_rows: list[dict], *, days: int, limit: int, delay: float) -> list[Posting]:
    from hunter.sources import fetch_job_text

    candidates = _candidate_live_rows(sheet_rows, days)
    sample = candidates[:limit]
    print(
        f"[calibrate] Live spot-check: {len(candidates)} eligible non-LinkedIn "
        f"Sent rows in the last {days} days, sampling {len(sample)}"
    )
    postings: list[Posting] = []
    fetch_errors = 0
    for i, row in enumerate(sample, start=1):
        url = row["URL"].strip()
        company = row.get("Company", "")
        title = row.get("Job Title", "")
        try:
            text = fetch_job_text(url)
            stale = is_job_expired(text)
            postings.append(Posting(
                source="live", company=company or url, title=title, url=url,
                owner_note=row.get("Sent", ""), text=text, stale=stale,
            ))
            stale_tag = " [STALE/EXPIRED on re-fetch]" if stale else ""
            print(f"[calibrate]   ({i}/{len(sample)}) OK — {company} ({len(text)} chars){stale_tag}")
        except Exception as e:  # noqa: BLE001 — tolerant of dead/blocked links
            fetch_errors += 1
            print(f"[calibrate]   ({i}/{len(sample)}) FETCH FAILED — {company}: {e}")
        if i < len(sample):
            time.sleep(delay)
    print(f"[calibrate] Live fetch: {len(postings)} ok, {fetch_errors} failed/skipped")
    return postings


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def run_gate(postings: list[Posting]) -> list[ReportRow]:
    rows: list[ReportRow] = []
    for p in postings:
        findings: list[GateFinding] = assess_job_text(p.text, title=p.title, company=p.company)
        for f in findings:
            rows.append(ReportRow(
                company=p.company, rule=f.rule, severity=f.severity,
                evidence=f.evidence, owner_note=p.owner_note,
                source=p.source, url=p.url, stale=p.stale,
            ))
    return rows


def print_report(postings: list[Posting], rows: list[ReportRow]) -> int:
    """Print the full report; returns the count of blocking false positives."""
    print("\n=== Doomed gate calibration report ===")
    print(f"{'company':<28} {'rule':<32} {'sev':<5} owner_note")
    print("-" * 100)
    for r in sorted(rows, key=lambda r: (r.severity, r.company)):
        stale_tag = " [STALE]" if r.stale else ""
        print(f"{r.company[:28]:<28} {r.rule[:32]:<32} {r.severity:<5} "
              f"{r.evidence[:60]!r} note={r.owner_note!r}{stale_tag}")

    n_hard = sum(1 for r in rows if r.severity == "hard")
    n_soft = sum(1 for r in rows if r.severity == "soft")
    n_clean = len(postings) - len({(r.source, r.company, r.url) for r in rows})

    print("\n--- Summary ---")
    print(f"Postings scanned: {len(postings)}")
    print(f"Findings: {n_hard} hard, {n_soft} soft")
    print(f"Clean postings (no finding): {n_clean}")

    def _sent_true(note: str) -> bool:
        return classify(note) == "applied"

    sent_hard = [r for r in rows if r.severity == "hard" and _sent_true(r.owner_note)]
    policy_excluded = [r for r in sent_hard if not r.stale and r.rule in _PRE_EXISTING_POLICY_RULES]
    false_positives = [r for r in sent_hard if not r.stale and r.rule not in _PRE_EXISTING_POLICY_RULES]
    stale_excluded = [r for r in sent_hard if r.stale]
    print(f"\nHARD findings on rows the owner actually SENT (must be zero): {len(false_positives)}")
    for r in false_positives:
        print(f"  \u26d4 {r.company} — {r.rule}: {r.evidence!r} (sent {r.owner_note!r}, {r.url})")
    if stale_excluded:
        print(
            f"\n({len(stale_excluded)} additional HARD hit(s) on sent rows excluded — the "
            "re-fetched page now reports the posting as expired, so expired_check."
            "is_job_expired() would already skip it in production before the gate runs):"
        )
        for r in stale_excluded:
            print(f"  (stale) {r.company} — {r.rule}: {r.evidence!r} (sent {r.owner_note!r}, {r.url})")
    if policy_excluded:
        print(
            f"\n({len(policy_excluded)} additional HARD hit(s) on sent rows excluded — "
            "pre-existing, already-owner-approved policy (exact-name blocklist, or a "
            "title-dependent rule that only just started reaching the gate this PR), "
            "not a gate false positive — see _PRE_EXISTING_POLICY_RULES):"
        )
        for r in policy_excluded:
            print(f"  (pre-policy) {r.company} — {r.rule}: {r.evidence!r} (sent {r.owner_note!r}, {r.url})")

    bigbear = [r for r in rows if "bigbear" in r.company.lower() and r.severity == "hard"]
    megaport = [r for r in rows if "megaport" in r.company.lower() and r.severity == "soft"]
    bigbear_note = "yes" if bigbear else "no (not in this run's corpus)"
    megaport_note = "yes" if megaport else "no (not in this run's corpus)"
    print(f"\nBigbearAI caught by a HARD rule: {bigbear_note}")
    print(f"Megaport caught by a SOFT rule: {megaport_note}")

    return len(false_positives)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also spot-check live Sheet URLs (network).")
    parser.add_argument("--days", type=int, default=45, help="Live sample window in days (default 45).")
    parser.add_argument("--limit", type=int, default=20, help="Max live URLs to fetch (default 20).")
    parser.add_argument("--delay", type=float, default=2.5, help="Seconds between live fetches (default 2.5).")
    parser.add_argument(
        "--dir", action="append", default=None,
        help="Offline corpus root; repeatable (default: Applications/ + any "
        "Applications_*/ folder present, e.g. the DeepSeek comparison runs).",
    )
    args = parser.parse_args()

    if args.dir:
        base_dirs = [ROOT / d for d in args.dir]
    else:
        base_dirs = [ROOT / "Applications"] + sorted(ROOT.glob("Applications_*"))

    sheet_rows = _load_sheet_rows()
    sent_index = _sent_index(sheet_rows)
    title_index = _title_index(sheet_rows)

    postings = load_offline_corpus(sent_index, title_index, base_dirs)
    if args.live:
        if not sheet_rows:
            print("[calibrate] --live requested but no Sheet rows available — skipping")
        else:
            postings += load_live_sample(sheet_rows, days=args.days, limit=args.limit, delay=args.delay)

    rows = run_gate(postings)
    n_false_positives = print_report(postings, rows)
    return 1 if n_false_positives else 0


if __name__ == "__main__":
    sys.exit(main())
