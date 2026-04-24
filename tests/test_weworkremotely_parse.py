"""We Work Remotely RSS parsing (no network)."""

from hunter.sources.weworkremotely import (
    WeworkremotelySource,
    _split_company_title,
    parse_weworkremotely_rss_xml,
)


MINIMAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Acme Corp: Senior Frontend Developer</title>
      <link>https://weworkremotely.com/remote-jobs/acme-senior-frontend-1</link>
      <region>Anywhere in the World</region>
      <country></country>
      <state></state>
      <skills>JavaScript</skills>
      <category>Programming</category>
      <description>&lt;p&gt;We use Angular and TypeScript.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_xml_one_item() -> None:
    rows = parse_weworkremotely_rss_xml(MINIMAL_RSS)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Senior Frontend Developer"
    assert r["company"] == "Acme Corp"
    assert r["url"] == "https://weworkremotely.com/remote-jobs/acme-senior-frontend-1"
    assert "Anywhere" in r["location"]
    assert r["category"] == "Programming"
    assert r["skills"] == "JavaScript"


def test_parse_skips_item_without_link() -> None:
    xml = MINIMAL_RSS.replace(
        "<link>https://weworkremotely.com/remote-jobs/acme-senior-frontend-1</link>",
        "",
    )
    rows = parse_weworkremotely_rss_xml(xml)
    assert rows == []


def test_split_company_title() -> None:
    assert _split_company_title("Co: Role Here") == ("Co", "Role Here")
    assert _split_company_title("No colon title") == ("Unknown", "No colon title")


def test_weworkremotely_source_parse() -> None:
    src = WeworkremotelySource()
    raw = parse_weworkremotely_rss_xml(MINIMAL_RSS)[0]
    job = src._parse(raw)
    assert job is not None
    assert job.title == "Senior Frontend Developer"
    assert job.company == "Acme Corp"
    assert job.source == "weworkremotely"
    assert job.salary is None


def test_weworkremotely_parse_incomplete() -> None:
    src = WeworkremotelySource()
    assert src._parse({"title": "", "company": "X", "url": "https://x"}) is None
    assert src._parse({"title": "T", "company": "C", "url": ""}) is None
