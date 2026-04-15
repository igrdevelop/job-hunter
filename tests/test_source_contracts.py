import json
from pathlib import Path

from hunter.models import Job
from hunter.sources.inhire import InhireSource
from hunter.sources.theprotocol import TheProtocolSource


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sources"


def _assert_job_contract(job: Job, source_name: str) -> None:
    assert isinstance(job, Job)
    assert job.source == source_name
    assert job.title
    assert job.company
    assert job.location
    assert job.url.startswith("http")


def test_theprotocol_contract_from_next_data_offers_fixture() -> None:
    html = (FIXTURES_DIR / "theprotocol_next_data_offers.html").read_text(encoding="utf-8")
    src = TheProtocolSource()

    raw_items = src._extract_next_data(html)
    assert len(raw_items) == 1

    job = src._parse(raw_items[0])
    assert job is not None
    _assert_job_contract(job, "theprotocol")
    assert "theprotocol.it" in job.url


def test_theprotocol_contract_from_next_data_dehydrated_fixture() -> None:
    html = (FIXTURES_DIR / "theprotocol_next_data_dehydrated.html").read_text(encoding="utf-8")
    src = TheProtocolSource()

    raw_items = src._extract_next_data(html)
    assert len(raw_items) == 1

    job = src._parse(raw_items[0])
    assert job is not None
    _assert_job_contract(job, "theprotocol")
    assert job.location == "Remote"
    assert "theprotocol.it" in job.url


def test_inhire_contract_from_vuex_fixture() -> None:
    raw = json.loads((FIXTURES_DIR / "inhire_vuex_offer.json").read_text(encoding="utf-8"))
    src = InhireSource()

    job = src._parse(raw)
    assert job is not None
    _assert_job_contract(job, "inhire")
    assert job.location == "Wroclaw (Remote)"


def test_inhire_contract_from_dom_fallback_fixture() -> None:
    raw = json.loads((FIXTURES_DIR / "inhire_dom_offer.json").read_text(encoding="utf-8"))
    src = InhireSource()

    job = src._parse(raw)
    assert job is not None
    _assert_job_contract(job, "inhire")
    assert job.title == "Junior Angular Developer"
    assert "?" not in job.url
