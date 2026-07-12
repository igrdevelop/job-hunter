"""
bot/formatters.py — Pure text-formatting helpers (no I/O, no Telegram calls).

Functions:
  _build_schedule_text()               → HTML string for /schedule command
  _format_check_responses_report(...)  → HTML string for /check_responses
  _format_daily_summary(...)           → HTML string for daily summary job
"""

from hunter.config import SCHEDULE_TIMES, SCHEDULE_SOURCE_OFFSET_MIN, TIMEZONE


def _build_schedule_text() -> str:
    from hunter.sources import ALL_SOURCES

    lines = []
    for idx, source in enumerate(ALL_SOURCES):
        times = []
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            times.append(f"{total // 60:02d}:{total % 60:02d}")
        lines.append(f"  <b>{source.name}</b>: {' / '.join(times)}")

    schedule_str = "\n".join(lines)
    return (
        f"⏰ <b>Schedule</b> ({TIMEZONE}, offset {SCHEDULE_SOURCE_OFFSET_MIN} min):\n{schedule_str}"
    )


def _format_check_responses_report(results) -> str:
    """Format run_confirmation_check() results into a Telegram HTML message."""
    from hunter.email_response_checker import MatchResult  # noqa: F401 (type hint)

    confirmed = [r for r in results if r.match_type in ("exact", "fuzzy") and r.row_id]
    ambiguous = [r for r in results if r.match_type == "ambiguous"]
    no_match = [r for r in results if r.match_type == "no_match"]

    if not results:
        return "📭 <b>No confirmation emails found</b> in the last few days."

    lines = []

    if confirmed:
        lines.append(f"✅ <b>Confirmed ({len(confirmed)}):</b>")
        for r in confirmed:
            c = r.candidates[0]
            tag = f"[{r.email.platform}]"
            lines.append(f"  • <b>{c['company']}</b> — {c['title']} <i>{tag}</i>")
        lines.append("")

    if ambiguous:
        lines.append(f"❓ <b>Ambiguous — needs review ({len(ambiguous)}):</b>")
        for r in ambiguous:
            company = r.email.company or "?"
            title = r.email.title or "(no title extracted)"
            lines.append(f"  • {company} — {title}")
            cands = ", ".join(c["title"] for c in r.candidates[:3])
            lines.append(f"    <i>Candidates: {cands}</i>")
        lines.append("")

    if no_match:
        lines.append(f"📭 <b>Not matched ({len(no_match)}):</b>")
        for r in no_match:
            company = r.email.company or "(no company)"
            title = r.email.title or "(no title)"
            lines.append(f"  • {company} — {title}")

    return "\n".join(lines).strip()


def _format_daily_summary(apps: list[dict], date_str: str) -> str:
    """Format a list of applications (from tracker) as an HTML summary."""
    if not apps:
        return f"📋 No applications recorded on {date_str}."
    lines = [f"📋 <b>Applications on {date_str} — {len(apps)} total:</b>"]
    for a in apps:
        ats = a.get("ats", "")
        ats_label = f" ({ats})" if ats and ats not in ("-", "—", "") else ""
        lines.append(f"  • <b>{a['company']}</b> — {a['title']}{ats_label}")
    return "\n".join(lines)
