"""
gmail_report.py — per-email breakdown of a Gmail hunt scan.

Builds the Telegram report the user asked for: for every alert email seen in a
hunt, show its date, the aggregator that sent it, and which vacancies were taken
into work (vs deduplicated or filtered, with the reason). Emails that yielded no
URLs (regex miss) and a hit on the fetch ceiling are surfaced too, so coverage
gaps are visible instead of silently hidden.

Inputs come from two places gathered in hunter.main:
  - email_log / capped: GmailSource.last_email_log / .last_capped (one record per
    email, incl. 0-URL and skipped confirmation emails)
  - outcomes: one JobOutcome per Gmail-sourced Job, tagged with its fate as the
    hunt's filter + dedup steps decide it

build_gmail_report() returns a list of message chunks, each under the Telegram
4096-char limit, so the caller sends them one by one without truncation.
"""

from collections import defaultdict
from dataclasses import dataclass

# Human-friendly labels for hunter.filters.FILTER_REASONS.
_REASON_LABELS: dict[str, str] = {
    "title_kw": "keyword mismatch",
    "require_angular": "no Angular",
    "level": "level",
    "exclude_pattern": "excluded stack",
    "react_no_angular": "React w/o Angular",
    "location": "location",
    "russia": "Russia-based",
    "german": "German required",
    "contract": "contract/part-time",
    "relocation": "relocation",
}

_DUP_STATUSES = frozenset({"dup_url", "dup_ct", "cooldown"})


@dataclass
class JobOutcome:
    """The fate of one Gmail-sourced vacancy in a hunt."""

    msg_id: str
    url: str
    title: str
    company: str
    status: str  # taken | dup_url | dup_ct | cooldown | filtered
    reason: str | None = None  # filter reason key when status == "filtered"

    @classmethod
    def from_job(cls, job, status: str, reason: str | None = None) -> "JobOutcome":
        meta = job.email_meta or {}
        return cls(
            msg_id=meta.get("msg_id", ""),
            url=job.url,
            title=job.title,
            company=job.company,
            status=status,
            reason=reason,
        )


def _esc(s: str) -> str:
    """Minimal HTML escape for Telegram HTML parse mode."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_date(dt) -> str:
    if dt is None:
        return "—"
    try:
        return dt.strftime("%d.%m %H:%M")
    except (AttributeError, ValueError):
        return "—"


def _shorten(s: str, n: int = 60) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _email_lines(record: dict, outcomes: list[JobOutcome]) -> list[str]:
    """Render the block of lines for a single email."""
    date = _fmt_date(record.get("date"))
    agg = record.get("aggregator") or "?"
    subj = _esc(_shorten(record.get("subject", "")))
    extracted = record.get("extracted", 0)

    # Confirmation / activity email — collapse to one dim line.
    if record.get("skipped"):
        return [f"📧 {date} · {agg} · «{subj}» — confirmation, skipped"]

    # Regex miss: email arrived but no extractable URL — a real coverage gap.
    if extracted == 0:
        return [f"📧 {date} · {agg} · «{subj}» — ⚠️ 0 URLs (parser miss)"]

    taken = [o for o in outcomes if o.status == "taken"]
    dups = [o for o in outcomes if o.status in _DUP_STATUSES]
    filtered = [o for o in outcomes if o.status == "filtered"]

    lines = [f"📧 {date} · {agg} · «{subj}» ({extracted}→{len(taken)})"]
    for o in taken:
        lines.append(f"   ✅ {_esc(o.title)} @ {_esc(o.company)}")

    tail_parts: list[str] = []
    if dups:
        tail_parts.append(f"♻️ {len(dups)} dup")
    if filtered:
        labels = sorted({_REASON_LABELS.get(o.reason, o.reason or "?") for o in filtered})
        tail_parts.append(f"✂️ {len(filtered)} ({', '.join(labels)})")
    if tail_parts:
        lines.append("   " + " · ".join(tail_parts))

    return lines


def build_gmail_report(
    email_log: list[dict],
    capped: bool,
    max_results: int,
    outcomes: list[JobOutcome],
    *,
    max_chars: int = 3500,
) -> list[str]:
    """Build per-email report chunks. Returns [] when there was no Gmail activity."""
    if not email_log and not outcomes:
        return []

    by_msg: dict[str, list[JobOutcome]] = defaultdict(list)
    for o in outcomes:
        by_msg[o.msg_id].append(o)

    # Newest emails first; undated (None) sink to the bottom.
    ordered = sorted(
        email_log,
        key=lambda r: (r.get("date") is not None, r.get("date")),
        reverse=True,
    )

    total_emails = len(email_log)
    total_found = sum(r.get("extracted", 0) for r in email_log)
    total_taken = sum(1 for o in outcomes if o.status == "taken")
    zero_url = sum(1 for r in email_log if not r.get("skipped") and r.get("extracted", 0) == 0)
    skipped = sum(1 for r in email_log if r.get("skipped"))

    header = [
        "<b>--- Gmail (by email) ---</b>",
        f"{total_emails} emails · {total_found} vacancies · taken <b>{total_taken}</b>",
    ]
    if zero_url:
        header.append(f"⚠️ {zero_url} emails with no parsed URLs (see below)")
    if skipped:
        header.append(f"· {skipped} confirmation emails skipped")
    if capped:
        header.append(
            f"⚠️ ceiling of {max_results} emails reached — some may be missing "
            f"(raise GMAIL_MAX_RESULTS)"
        )

    # Chunk by whole-email blocks so a message never splits mid-email.
    chunks: list[str] = []
    current = list(header)
    current_len = sum(len(line) + 1 for line in current)

    for record in ordered:
        block = _email_lines(record, by_msg.get(record.get("msg_id", ""), []))
        block_len = sum(len(line) + 1 for line in block)
        if current_len + block_len > max_chars and len(current) > len(header):
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.extend(block)
        current_len += block_len

    if current:
        chunks.append("\n".join(current))

    return chunks
