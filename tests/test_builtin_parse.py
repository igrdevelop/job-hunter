"""Built In source parsing (no network)."""

from hunter.sources.builtin import BuiltInSource, parse_builtin_cards


SAMPLE_HTML = """
<html><body>
  <div data-id="job-card">
    <a data-id="company-title" href="/company/acme">Acme</a>
    <a data-id="job-card-title" href="/job/senior-frontend-engineer/123">Senior Frontend Engineer</a>
    <div class="d-flex"><span class="font-barlow text-gray-04">Remote or Hybrid</span></div>
    <span>New York, NY, USA</span>
  </div>
  <div data-id="job-card">
    <a data-id="company-title" href="#">Two-Up</a>
    <a data-id="job-card-title" href="/job/fe-react-remote/456">Frontend Developer [React] - Remote</a>
    <span class="font-barlow text-gray-04">In-Office or Remote</span>
  </div>
  <div data-id="job-card">
    <a data-id="company-title" href="#">NoLink Co</a>
    <span data-id="job-card-title">Title Without Anchor Href</span>
  </div>
  <div data-id="job-card">
    <a data-id="job-card-title" href="/job/no-arrangement/789">Frontend Developer</a>
  </div>
</body></html>
"""


def test_parse_cards_extracts_fields() -> None:
    cards = parse_builtin_cards(SAMPLE_HTML)
    # card 3 has no href on the title element -> skipped
    assert len(cards) == 3
    first = cards[0]
    assert first["title"] == "Senior Frontend Engineer"
    assert first["company"] == "Acme"
    assert first["href"] == "/job/senior-frontend-engineer/123"
    assert first["location"] == "Remote or Hybrid"


def test_arrangement_not_taken_from_title() -> None:
    """A title containing 'Remote' must not be mistaken for the arrangement."""
    cards = parse_builtin_cards(SAMPLE_HTML)
    react = cards[1]
    assert react["title"] == "Frontend Developer [React] - Remote"
    assert react["location"] == "In-Office or Remote"


def test_parse_builds_absolute_url() -> None:
    src = BuiltInSource()
    raw = parse_builtin_cards(SAMPLE_HTML)[0]
    job = src._parse(raw)
    assert job is not None
    assert job.url == "https://builtin.com/job/senior-frontend-engineer/123"
    assert job.company == "Acme"
    assert job.location == "Remote or Hybrid"
    assert job.source == "builtin"


def test_parse_defaults_location_to_remote() -> None:
    src = BuiltInSource()
    # the 'no-arrangement' card (last in fixture) has no arrangement label
    raw = next(c for c in parse_builtin_cards(SAMPLE_HTML) if c["href"].endswith("/789"))
    job = src._parse(raw)
    assert job is not None
    assert job.location == "Remote"
    assert job.company == "Unknown"


def test_parse_rejects_missing_fields() -> None:
    src = BuiltInSource()
    assert src._parse({"title": "X", "href": ""}) is None
    assert src._parse({"title": "", "href": "/job/x/1"}) is None


def test_parse_cards_handles_empty_html() -> None:
    assert parse_builtin_cards("<html><body>no cards</body></html>") == []


def test_matches_url() -> None:
    src = BuiltInSource()
    assert src.matches_url("https://builtin.com/job/x/123") is True
    assert src.matches_url("https://www.builtin.com/jobs") is True
    assert src.matches_url("https://example.com/x") is False
