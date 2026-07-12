"""Phase C/D: build_gmail_report renders the per-email breakdown."""

from datetime import datetime, timezone

from hunter.gmail_report import build_gmail_report, JobOutcome
from hunter.models import Job


def _rec(msg_id, agg, subj, extracted, skipped=False, date=None):
    return {
        "msg_id": msg_id,
        "date": date or datetime(2026, 6, 9, 14, 20, tzinfo=timezone.utc),
        "subject": subj,
        "sender": f"alerts@{agg}.com",
        "aggregator": agg,
        "extracted": extracted,
        "skipped": skipped,
    }


def _out(msg_id, status, reason=None, title="Angular Dev", company="Acme"):
    return JobOutcome(
        msg_id=msg_id,
        url=f"https://x/{title}-{status}",
        title=title,
        company=company,
        status=status,
        reason=reason,
    )


def test_empty_returns_no_chunks():
    assert build_gmail_report([], False, 100, []) == []


def test_from_job_pulls_email_meta():
    job = Job(
        title="Senior Angular",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://x/1",
        source="gmail_linkedin",
        email_meta={"msg_id": "m1"},
    )
    o = JobOutcome.from_job(job, "taken")
    assert o.msg_id == "m1"
    assert o.status == "taken"
    assert o.title == "Senior Angular"


def test_header_totals():
    log = [_rec("m1", "linkedin", "10 new jobs", 3)]
    outcomes = [
        _out("m1", "taken"),
        _out("m1", "dup_url"),
        _out("m1", "filtered", "location"),
    ]
    text = "\n".join(build_gmail_report(log, False, 100, outcomes))
    assert "1 писем" in text
    assert "3 вакансий" in text
    assert "взято <b>1</b>" in text


def test_taken_jobs_listed():
    log = [_rec("m1", "linkedin", "digest", 2)]
    outcomes = [
        _out("m1", "taken", title="Senior Angular", company="Acme"),
        _out("m1", "dup_url"),
    ]
    text = "\n".join(build_gmail_report(log, False, 100, outcomes))
    assert "✅ Senior Angular @ Acme" in text
    assert "♻️ 1 дубл" in text


def test_filtered_reason_label():
    log = [_rec("m1", "pracuj", "oferty", 1)]
    outcomes = [_out("m1", "filtered", "react_no_angular")]
    text = "\n".join(build_gmail_report(log, False, 100, outcomes))
    assert "✂️ 1 (React без Angular)" in text


def test_zero_url_email_surfaced():
    log = [_rec("m1", "pracuj", "Praca dla Ciebie", 0)]
    chunks = build_gmail_report(log, False, 100, [])
    text = "\n".join(chunks)
    assert "0 ссылок" in text
    assert "без распознанных ссылок" in text  # header coverage warning


def test_skipped_email_collapsed():
    log = [_rec("m1", "", "Your application was sent", 0, skipped=True)]
    text = "\n".join(build_gmail_report(log, False, 100, []))
    assert "подтверждение, пропущено" in text
    assert "1 писем-подтверждений" in text


def test_ceiling_warning_when_capped():
    log = [_rec("m1", "linkedin", "digest", 1)]
    text = "\n".join(build_gmail_report(log, True, 100, [_out("m1", "taken")]))
    assert "потолок 100" in text


def test_chunks_stay_under_limit():
    # Many emails → multiple chunks, each within budget, no email split.
    log = [_rec(f"m{i}", "linkedin", f"digest number {i} " * 5, 1) for i in range(80)]
    outcomes = [_out(f"m{i}", "taken", title="Senior Angular Developer") for i in range(80)]
    chunks = build_gmail_report(log, False, 100, outcomes, max_chars=1500)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 1700  # budget + one block of slack


def test_newest_email_first():
    older = _rec("old", "linkedin", "older", 1, date=datetime(2026, 6, 1, tzinfo=timezone.utc))
    newer = _rec("new", "linkedin", "newer", 1, date=datetime(2026, 6, 9, tzinfo=timezone.utc))
    text = "\n".join(
        build_gmail_report([older, newer], False, 100, [_out("old", "taken"), _out("new", "taken")])
    )
    assert text.index("newer") < text.index("older")


def test_html_escaped_in_subject_and_title():
    log = [_rec("m1", "linkedin", "Dev <script> & co", 1)]
    outcomes = [_out("m1", "taken", title="A<b>B", company="X&Y")]
    text = "\n".join(build_gmail_report(log, False, 100, outcomes))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "A&lt;b&gt;B" in text
    assert "X&amp;Y" in text
