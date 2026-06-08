"""Jobspresso RSS parsing + location handling (no network)."""

from hunter.sources.jobspresso import (
    JobspressoSource,
    _format_location,
    parse_jobspresso_rss_xml,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:job_listing="https://jobspresso.co/ns/job_listing">
  <channel>
    <title>Jobspresso</title>
    <item>
      <title>Senior Frontend Developer</title>
      <link>https://jobspresso.co/job/senior-frontend-developer/</link>
      <guid>https://jobspresso.co/?post_type=job_listing&amp;p=1</guid>
      <description>We need Angular and TypeScript skills.</description>
      <job_listing:company>Acme Corp</job_listing:company>
      <job_listing:location>Worldwide</job_listing:location>
      <job_listing:job_type>Developer</job_listing:job_type>
      <job_listing:job_category>Full Time</job_listing:job_category>
    </item>
    <item>
      <title>Customer Support Lead</title>
      <link>https://jobspresso.co/job/customer-support-lead/</link>
      <description>Support our users.</description>
      <job_listing:company>Helpful Inc</job_listing:company>
      <job_listing:location>United States</job_listing:location>
    </item>
    <item>
      <title></title>
      <link>https://jobspresso.co/job/no-title/</link>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_extracts_namespaced_fields() -> None:
    items = parse_jobspresso_rss_xml(SAMPLE_RSS)
    # third item has no title -> dropped
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Senior Frontend Developer"
    assert first["url"] == "https://jobspresso.co/job/senior-frontend-developer/"
    assert first["company"] == "Acme Corp"
    assert first["location"] == "Worldwide"
    assert first["job_type"] == "Developer"


def test_parse_rss_handles_broken_xml() -> None:
    assert parse_jobspresso_rss_xml("<not xml") == []


def test_parse_builds_job_with_remote_location() -> None:
    src = JobspressoSource()
    raw = parse_jobspresso_rss_xml(SAMPLE_RSS)[0]
    job = src._parse(raw)
    assert job is not None
    assert job.title == "Senior Frontend Developer"
    assert job.company == "Acme Corp"
    # Worldwide -> Remote so the central location whitelist keeps it
    assert job.location == "Remote"
    assert job.source == "jobspresso"


def test_parse_rejects_missing_url() -> None:
    src = JobspressoSource()
    assert src._parse({"title": "X", "url": ""}) is None
    assert src._parse({"title": "", "url": "http://x"}) is None


def test_parse_defaults_company_to_unknown() -> None:
    src = JobspressoSource()
    job = src._parse({"title": "Frontend Dev", "url": "http://x", "location": ""})
    assert job is not None
    assert job.company == "Unknown"
    assert job.location == "Remote"


def test_format_location() -> None:
    assert _format_location("Worldwide") == "Remote"
    assert _format_location("anywhere") == "Remote"
    assert _format_location("") == "Remote"
    assert _format_location(None) == "Remote"
    # Geographic restriction kept as hint but still carries the remote token
    assert _format_location("United States") == "United States (Remote)"


def test_matches_url() -> None:
    src = JobspressoSource()
    assert src.matches_url("https://jobspresso.co/job/abc/") is True
    assert src.matches_url("https://www.jobspresso.co/job/abc/") is True
    assert src.matches_url("https://example.com/x") is False
