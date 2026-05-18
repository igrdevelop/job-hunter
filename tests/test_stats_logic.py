"""Unit tests for cmd_stats row-counting logic (isolated from Telegram)."""
from datetime import date, timedelta
from collections import Counter


def _cutoff(days: int = 30) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


_REACT_SENT_MARKERS = {"—", "–", "-"}


def _compute_stats(rows: list[dict], days: int = 30):
    """Extracted counting logic from cmd_stats — tested here without mocking Telegram."""
    cutoff = _cutoff(days)
    applied_scores: list[float] = []
    counts: Counter[str] = Counter()
    company_counter: Counter[str] = Counter()

    for row in rows:
        row_date = row.get("Date", "")[:10]
        if len(row_date) < 10 or row_date < cutoff:
            continue
        ats = row.get("ATS %", "").strip()
        if not ats or ats in ("—", "-", "–"):
            continue
        sent = row.get("Sent", "").strip()
        try:
            score = float(ats)
            counts["applied"] += 1
            applied_scores.append(score)
            company = row.get("Company", "").strip()
            if company:
                company_counter[company] += 1
        except ValueError:
            status = ats.upper()
            if status == "SKIP" and sent in _REACT_SENT_MARKERS:
                counts["react"] += 1
            else:
                counts[status] += 1

    avg = sum(applied_scores) / len(applied_scores) if applied_scores else None
    return counts, avg, company_counter


def _row(ats: str, sent: str = "", company: str = "Acme", days_ago: int = 1) -> dict:
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return {"Date": d, "ATS %": ats, "Sent": sent, "Company": company}


def test_applied_counted():
    counts, avg, _ = _compute_stats([_row("87"), _row("92"), _row("95")])
    assert counts["applied"] == 3
    assert round(avg) == 91


def test_skip_counted():
    counts, _, _ = _compute_stats([_row("SKIP", sent="2026-05-01")])
    assert counts["SKIP"] == 1
    assert counts["react"] == 0


def test_react_only_distinguished_from_skip():
    counts, _, _ = _compute_stats([
        _row("SKIP", sent="—"),   # react-only
        _row("SKIP", sent="2026-05-01"),  # regular skip
    ])
    assert counts["react"] == 1
    assert counts["SKIP"] == 1


def test_expired_and_fail():
    counts, _, _ = _compute_stats([_row("EXPIRED"), _row("FAIL")])
    assert counts["EXPIRED"] == 1
    assert counts["FAIL"] == 1


def test_old_rows_excluded():
    counts, _, _ = _compute_stats([_row("87", days_ago=31)])
    assert counts["applied"] == 0


def test_missing_date_excluded():
    counts, _, _ = _compute_stats([{"ATS %": "87", "Sent": "", "Company": "X", "Date": ""}])
    assert counts["applied"] == 0


def test_top_companies():
    rows = [
        _row("87", company="Allegro"),
        _row("90", company="Allegro"),
        _row("85", company="Revolut"),
    ]
    _, _, company_counter = _compute_stats(rows)
    top = company_counter.most_common(3)
    assert top[0] == ("Allegro", 2)
    assert top[1] == ("Revolut", 1)


def test_empty_tracker():
    counts, avg, company_counter = _compute_stats([])
    assert counts["applied"] == 0
    assert avg is None
    assert len(company_counter) == 0
