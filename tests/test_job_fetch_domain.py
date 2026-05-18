"""Tests for _matches_domain and fetch_job_text dispatch logic."""
import pytest
from job_fetch import _matches_domain


@pytest.mark.parametrize("domain,host,expected", [
    # Exact match
    ("linkedin.com",       "linkedin.com",  True),
    # Legitimate subdomain
    ("www.linkedin.com",   "linkedin.com",  True),
    ("uk.linkedin.com",    "linkedin.com",  True),
    # Crafted attack domain — old `in` check would have matched
    ("linkedin.com.evil.example.com", "linkedin.com", False),
    ("notlinkedin.com",    "linkedin.com",  False),
    ("mylinkedin.com",     "linkedin.com",  False),
    # Recruitee pattern
    ("company.recruitee.com", "recruitee.com", True),
    ("recruitee.com",         "recruitee.com", True),
    ("notrecruitee.com",      "recruitee.com", False),
    # ATS exact subdomains
    ("apply.workable.com", "workable.com", True),
    ("jobs.lever.co",      "lever.co",     True),
    ("jobs.ashbyhq.com",   "ashbyhq.com",  True),
])
def test_matches_domain(domain, host, expected):
    assert _matches_domain(domain, host) is expected
