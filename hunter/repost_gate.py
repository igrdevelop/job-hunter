"""
hunter/repost_gate.py — same-vacancy re-post gate (Step 1.5g in both pipelines).

A vacancy the owner already applied to routinely comes back with a NEW URL:
the company re-posts it after expiry, posts it on a second job board, or an
agency re-lists it under a slightly different company name ("ITDS" vs "ITDS
Polska", "DHC" vs "DHC Business Solutions"). URL dedup can't see it and the
company+title listing dedup misses the name/title variations — so the bot
regenerated a near-identical CV at full LLM cost. Calibration over the real
Applications corpus (675 applies, tools/reuse_calibrate.py, 2026-07-20) found
~14% of all generations were exactly this case.

The gate runs AFTER fetch + doomed gate and BEFORE the first LLM call:
compare the fetched posting text against the job_posting.txt of recent
applied rows (tracker rows with a docs folder, last REPOST_WINDOW_DAYS days).
The PRIMARY key is posting-text TF-IDF similarity — a re-post is a
near-verbatim text regardless of how the company name is spelled; fuzzy
company-name agreement only lowers the required similarity, it is never the
key itself. Decision matrix (thresholds calibrated on the same corpus —
same-company re-posts cluster at sim>=0.95 while agency-boilerplate false
positives (Hays/UST/emagine posting DIFFERENT roles in near-identical words)
live in the 0.85-0.90 band, so that band only ever warns):

    sim >= 0.97  and both texts >= 1500 chars      -> re-post (any company)
    sim >= 0.90  and fuzzy company-name agreement  -> re-post
    sim >= 0.85  otherwise                          -> warn only, generate

On a detected re-post the gate REUSES the existing CV (owner decision
2026-07-20): copy the donor folder's rendered docs + content.json into a
fresh Applications/{today}/{Company}/ folder, write a normal applied tracker
row for the NEW url with the Re-application flag and cost $0, stamp the
donor's ATS verdict, notify Telegram with the files — a full apply outcome
with zero LLM calls. The dual-apply shadow is skipped (the pipeline returns
None: comparing models on a copied CV is meaningless). Sheets/Drive delivery
needs no wiring: the parent process's normal post-apply hooks fire on exit 0
and find the new tracker row.

Short texts are never compared (the April theprotocol.it anti-bot stub pages
were byte-identical across companies — sim 1.0 garbage), `/force` bypasses
the gate entirely (an explicit "generate this one anyway"), and every failure
path degrades to "continue with normal generation" — the gate can only ever
save money, never lose an apply.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

# ── Decision thresholds (calibrated 2026-07-20, tools/reuse_calibrate.py) ─────

MIN_TEXT_CHARS = 1500  # below this, block pages / stubs produce garbage sims
SIM_HARD = 0.97  # re-post regardless of company-name agreement
SIM_COMPANY = 0.90  # re-post when the fuzzy company names also agree
SIM_WARN = 0.85  # below SIM_COMPANY (or no name agreement): warn only

# Generic tokens stripped from company names before comparison — legal forms
# and filler words that make "DHC Business Solutions" != "DHC". Deliberately
# NOT stripping short brand-ish words that carry identity (e.g. "group" IS
# stripped: "GN Group" vs "Jabra" still won't match on the name — that pair
# only ever matches through the SIM_HARD text branch).
_GENERIC_TOKENS = frozenset(
    {
        "sp",
        "z",
        "o",
        "oo",
        "zoo",
        "sa",
        "gmbh",
        "ag",
        "ltd",
        "llc",
        "inc",
        "bv",
        "sro",
        "company",
        "co",
        "group",
        "holding",
        "solutions",
        "solution",
        "services",
        "service",
        "software",
        "technology",
        "technologies",
        "tech",
        "consulting",
        "recruiting",
        "recruitment",
        "erecruiting",
        "polska",
        "poland",
        "europe",
        "global",
        "international",
        "digital",
        "labs",
        "lab",
        "studio",
        "team",
    }
)

_MATCH_RATIO = 0.85  # difflib similarity floor for near-equal core names


def normalize_company(name: str) -> str:
    """Lowercase, drop punctuation, strip legal/generic filler tokens.

    "DHC Business Solutions Sp. z o.o." -> "dhcbusiness"
    "ITDS Polska"                        -> "itds"
    """
    tokens = re.split(r"[^a-z0-9]+", (name or "").lower())
    core = [t for t in tokens if t and t not in _GENERIC_TOKENS]
    return "".join(core)


def companies_match(a: str, b: str) -> bool:
    """Fuzzy company-name agreement: equal, containment, or near-equal core.

    Containment covers "ITDS" in "ITDSPolska"-style expansions; the difflib
    ratio covers one-word insertions ("HI Technology Innovation" vs "HI
    Technology And Innovation"). Empty names never match — an unknown company
    must go through the stricter SIM_HARD text branch.
    """
    na, nb = normalize_company(a), normalize_company(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _MATCH_RATIO


# ── Donor lookup ──────────────────────────────────────────────────────────────


@dataclass
class RepostMatch:
    action: str  # "reuse" | "warn"
    similarity: float
    donor_url: str
    donor_company: str
    donor_date: str
    donor_folder: Path


def _strip_posting_header(text: str) -> str:
    """Drop the URL:/Post: header lines job_posting.txt files start with."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith(("URL:", "Post:")) or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:]).strip()


def _load_donors(window_days: int) -> list[dict]:
    """Recent applied tracker rows that have a docs folder on disk, newest
    first, each with its job_posting.txt text loaded (header-stripped)."""
    from hunter.tracker import get_recent_applied_for_repost

    donors = []
    for row in get_recent_applied_for_repost(window_days):
        folder = Path(row["folder"])
        posting_path = folder / "job_posting.txt"
        content_path = folder / "content.json"
        if not (posting_path.exists() and content_path.exists()):
            continue
        try:
            text = _strip_posting_header(posting_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if len(text) < MIN_TEXT_CHARS:
            continue
        donors.append({**row, "folder_path": folder, "posting": text})
    return donors


def _similarities(job_text: str, donor_texts: list[str]) -> list[float]:
    """TF-IDF cosine similarity of job_text vs each donor text (one fit)."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    mat = vec.fit_transform([job_text, *donor_texts])
    return [float(s) for s in cosine_similarity(mat[0:1], mat[1:])[0]]


def find_repost(job_text: str, company: str, *, window_days: int) -> RepostMatch | None:
    """Best re-post candidate among recent applied rows, or None.

    Returns a RepostMatch with action="reuse" when the decision matrix says
    this is the same vacancy, action="warn" for the ambiguous band. The
    caller owns notify/abort semantics.
    """
    job_text = (job_text or "").strip()
    if len(job_text) < MIN_TEXT_CHARS:
        return None
    donors = _load_donors(window_days)
    if not donors:
        return None

    sims = _similarities(job_text, [d["posting"] for d in donors])
    best_i = max(range(len(donors)), key=lambda i: sims[i])
    best, sim = donors[best_i], sims[best_i]
    if sim < SIM_WARN:
        return None

    if sim >= SIM_HARD or (sim >= SIM_COMPANY and companies_match(company, best["company"])):
        action = "reuse"
    else:
        action = "warn"
    return RepostMatch(
        action=action,
        similarity=sim,
        donor_url=best["url"],
        donor_company=best["company"],
        donor_date=best["date"],
        donor_folder=best["folder_path"],
    )


# ── Reuse execution ───────────────────────────────────────────────────────────

_COPY_PATTERNS = ("*.pdf", "*.docx", "outreach.md", "judge_report.json")


def execute_reuse(
    match: RepostMatch,
    url: str,
    job_text: str,
    *,
    permalink: str = "",
) -> Path | None:
    """Copy the donor folder's docs into a fresh dated folder, write the
    applied tracker row (Re-application, cost $0), stamp the donor's verdict,
    notify Telegram with the files. Returns the new folder, or None on any
    failure (caller falls back to normal generation)."""
    from hunter.apply_shared import (
        PASTE_NO_URL_PLACEHOLDER,
        compute_output_folder,
        notify,
        send_telegram_documents,
    )

    try:
        content = json.loads((match.donor_folder / "content.json").read_text(encoding="utf-8"))

        # Folder name: reuse the donor's already-sanitized folder name (minus
        # any earlier _reused_{date} tag and same-day _2/_3 suffix — a chained
        # re-post must not grow "AcmeCorp_reused_A_reused_B") rather than the
        # raw content.json company string — the donor name is proven
        # filesystem-safe. Then tag the copy with WHERE it came from (owner
        # request 2026-07-20): "AcmeCorp_reused_2026-07-01" is visible at a
        # glance locally and on Drive, so a reused application is never
        # mistaken for a freshly generated one.
        folder_base = (
            re.sub(r"(?:_reused(?:_\d{4}-\d{2}-\d{2})?)?(?:_\d+)?$", "", match.donor_folder.name)
            or "Unknown"
        )
        donor_day = (
            match.donor_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", match.donor_date or "") else ""
        )
        reuse_tag = f"_reused_{donor_day}" if donor_day else "_reused"
        new_folder = compute_output_folder(folder_base + reuse_tag)
        new_folder.mkdir(parents=True, exist_ok=True)

        copied: list[Path] = []
        for pattern in _COPY_PATTERNS:
            for src in sorted(match.donor_folder.glob(pattern)):
                dst = new_folder / src.name
                shutil.copy2(src, dst)
                copied.append(dst)
        docs = [p for p in copied if p.suffix in (".pdf", ".docx")]
        if not docs:
            print(f"[repost_gate] donor folder has no rendered docs: {match.donor_folder}")
            return None

        real_url = "" if url == PASTE_NO_URL_PLACEHOLDER else url
        content["apply_url"] = real_url
        content["output_folder"] = str(new_folder).replace("\\", "/")
        content["reused_from"] = str(match.donor_folder).replace("\\", "/")
        content["reused_similarity"] = round(match.similarity, 3)
        # $0 truthfully — no LLM call was made for this vacancy. add_applied
        # reads cost.total_usd, so the Sheet column M shows 0 instead of blank.
        content["cost"] = {"total_usd": 0.0, "reused": True}
        if permalink:
            content["source_permalink"] = permalink
        (new_folder / "content.json").write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        url_line = f"URL: {real_url}\n\n" if real_url else "URL: (none — pasted by user)\n\n"
        if permalink:
            url_line += f"Post: {permalink}\n\n"
        (new_folder / "job_posting.txt").write_text(url_line + job_text, encoding="utf-8")

        from hunter.tracker import add_applied

        add_applied(content, reapplication=True)

        verdict = content.get("ats_verdict")
        score = verdict.get("score") if isinstance(verdict, dict) else None
        if real_url and score is not None:
            try:
                from hunter.tracker import set_ats_verdict

                set_ats_verdict(real_url, float(score))
            except Exception as e:  # noqa: BLE001 — stamp is best-effort
                print(f"[repost_gate] Warning: verdict stamp failed: {e}")

        ats_line = f"ATS: {score}% (reused verdict)" if score is not None else ""
        file_names = "\n".join(f"  • {p.name}" for p in sorted(docs))
        notify(
            f"♻️ <b>Re-post detected — reusing existing CV ($0)</b>\n\n"
            f"Same vacancy as <b>{match.donor_company}</b> "
            f"({match.donor_date}, {match.similarity:.0%} text match):\n"
            f"🔗 {match.donor_url}\n\n"
            f"📁 <code>Applications/{new_folder.parent.name}/{new_folder.name}/</code>\n"
            f"{file_names}\n\n"
            f"{ats_line}\n"
            f"🔗 {real_url or '(pasted text)'}\n"
            f"Use /force to regenerate from scratch instead."
        )
        send_telegram_documents(docs)
        print(
            f"[repost_gate] REUSED docs from {match.donor_folder} "
            f"(sim {match.similarity:.3f}) -> {new_folder}"
        )
        return new_folder
    except Exception as e:  # noqa: BLE001 — reuse must never lose an apply
        print(f"[repost_gate] Warning: reuse failed (falling back to generation): {e}")
        return None


# ── Pipeline entry point ──────────────────────────────────────────────────────


def run_repost_gate(
    job_text: str,
    url: str,
    *,
    company: str = "",
    permalink: str = "",
    is_force_override: bool = False,
) -> Path | None:
    """Step 1.5g in both pipelines, right after the doomed gate.

    Returns the reused folder when the vacancy is a re-post and the existing
    CV was successfully reused — the caller must then SKIP generation (and
    the dual-apply shadow). Returns None to continue normally: gate disabled,
    `/force`, no match, warn-band match (Telegram-notified here), or any
    internal failure — best-effort, the gate never blocks an apply.
    """
    from hunter.config import REPOST_GATE_ENABLED, REPOST_WINDOW_DAYS

    if not REPOST_GATE_ENABLED or is_force_override:
        return None

    try:
        match = find_repost(job_text, company, window_days=REPOST_WINDOW_DAYS)
    except Exception as e:  # noqa: BLE001 — best-effort, never block apply
        print(f"[repost_gate] Warning: repost check failed (continuing): {e}")
        return None
    if match is None:
        return None

    from hunter.apply_shared import notify

    if match.action == "warn":
        notify(
            f"⚠️ <b>Possible re-post</b> ({match.similarity:.0%} text match, "
            f"below auto-reuse confidence)\n"
            f"Earlier: <b>{match.donor_company}</b> ({match.donor_date})\n"
            f"🔗 {match.donor_url}\n\n"
            f"Generating fresh documents anyway…"
        )
        print(
            f"[repost_gate] WARN — possible repost of {match.donor_url} "
            f"(sim {match.similarity:.3f}), generating anyway"
        )
        return None

    folder = execute_reuse(match, url, job_text, permalink=permalink)
    return folder  # None on failure -> caller continues with generation
