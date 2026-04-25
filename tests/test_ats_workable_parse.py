"""Workable adapter — pure-parse tests (no network)."""

from hunter.ats.workable import parse_workable_job
from hunter.sources.ats_aggregator import load_companies


def test_parse_published_remote() -> None:
    raw = {
        "id": "abc",
        "shortcode": "DEF123",
        "title": "Senior Frontend Engineer (Angular)",
        "state": "published",
        "country": "Poland",
        "city": "Wrocław",
        "telecommuting": True,
    }
    job = parse_workable_job(raw, "netguru", "Netguru")
    assert job is not None
    assert job.title == "Senior Frontend Engineer (Angular)"
    assert job.company == "Netguru"
    assert job.source == "ats:workable:netguru"
    assert "Remote" in job.location
    assert job.url == "https://apply.workable.com/j/DEF123"


def test_parse_uses_explicit_url_when_present() -> None:
    raw = {
        "title": "Frontend Dev",
        "state": "published",
        "shortcode": "X",
        "url": "https://apply.workable.com/j/CUSTOM",
    }
    job = parse_workable_job(raw, "netguru", "Netguru")
    assert job is not None
    assert job.url == "https://apply.workable.com/j/CUSTOM"


def test_parse_skips_archived() -> None:
    raw = {"title": "Frontend Dev", "shortcode": "X", "state": "archived"}
    assert parse_workable_job(raw, "netguru", "Netguru") is None


def test_parse_skips_missing_title() -> None:
    raw = {"shortcode": "X", "state": "published"}
    assert parse_workable_job(raw, "netguru", "Netguru") is None


def test_parse_skips_no_url_no_shortcode() -> None:
    raw = {"title": "Frontend Dev", "state": "published"}
    assert parse_workable_job(raw, "netguru", "Netguru") is None


def test_parse_onsite_location_no_remote_suffix() -> None:
    raw = {
        "title": "Frontend",
        "state": "published",
        "shortcode": "X",
        "country": "Poland",
        "city": "Warsaw",
        "telecommuting": False,
    }
    job = parse_workable_job(raw, "acme", "Acme")
    assert job is not None
    assert "Remote" not in job.location
    assert "Warsaw" in job.location


def test_load_companies_missing_file(tmp_path) -> None:
    assert load_companies(tmp_path / "does-not-exist.json") == []


def test_load_companies_valid(tmp_path) -> None:
    p = tmp_path / "ats.json"
    p.write_text(
        '{"companies": [{"slug": "netguru", "provider": "workable"}, "junk"]}',
        encoding="utf-8",
    )
    out = load_companies(p)
    assert len(out) == 1
    assert out[0]["slug"] == "netguru"


def test_load_companies_malformed(tmp_path) -> None:
    p = tmp_path / "ats.json"
    p.write_text("not json at all", encoding="utf-8")
    assert load_companies(p) == []
