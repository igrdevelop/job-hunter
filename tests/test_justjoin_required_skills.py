"""JustJoin skill keys: requiredSkills / niceToHaveSkills (no network).

Regression for 2026-08-12: the candidate API returns ``skills: null`` and puts
the real stack under ``requiredSkills`` (plus ``niceToHaveSkills``). Both the
React-without-Angular filter and the detail-page fetcher still read the old
flat ``skills`` key, so:

  * every JustJoin job was judged on its TITLE alone — an "Intermediate
    Frontend Developer" whose required skills were TypeScript/React/Next.js
    passed the listing filter and was auto-applied for;
  * ``job_posting.txt`` lost the whole stack section, which is what the
    apply-time text screens read.
"""

from unittest.mock import patch

from hunter.filters import _is_react_without_angular, classify_job
from hunter.models import Job
from hunter.sources import justjoin as jj


def _offer(**extra) -> dict:
    offer = {
        "slug": "itfs-intermediate-frontend-developer-warszawa-javascript",
        "title": "Intermediate Frontend Developer / UI & Platform Integration Specialist",
        "companyName": "ITFS",
        "city": "Warszawa",
        "workplaceType": "remote",
        "employmentTypes": [],
        "skills": None,
    }
    offer.update(extra)
    return offer


def _job(offer: dict) -> Job:
    job = jj.JustJoinSource()._parse_offer(offer, offer["slug"], "remote")
    assert job is not None
    return job


def test_required_skills_react_without_angular_is_excluded() -> None:
    offer = _offer(
        requiredSkills=[
            {"name": "TypeScript", "level": 4},
            {"name": "React", "level": 4},
            {"name": "Next.js", "level": 4},
        ]
    )
    job = _job(offer)
    assert _is_react_without_angular(job) is True
    assert classify_job(job) == "react_no_angular"


def test_nice_to_have_skills_are_read_too() -> None:
    offer = _offer(
        requiredSkills=[{"name": "TypeScript", "level": 4}],
        niceToHaveSkills=[{"name": "React", "level": 2}],
    )
    assert _is_react_without_angular(_job(offer)) is True


def test_angular_in_required_skills_keeps_the_job() -> None:
    offer = _offer(
        requiredSkills=[
            {"name": "Angular", "level": 5},
            {"name": "React", "level": 3},
        ]
    )
    job = _job(offer)
    assert _is_react_without_angular(job) is False
    assert classify_job(job) is None


def test_legacy_flat_skills_key_still_works() -> None:
    """Other sources (and older fixtures) still use the flat list."""
    offer = _offer(skills=[{"name": "React.js"}])
    assert _is_react_without_angular(_job(offer)) is True


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


def test_fetch_text_renders_required_and_nice_to_have_skills() -> None:
    body = _offer(
        requiredSkills=[{"name": "TypeScript", "level": 4}, {"name": "React", "level": 4}],
        niceToHaveSkills=[{"name": "Figma", "level": 1}],
        body="<p>Build the platform UI.</p>",
        experienceLevel="mid",
    )
    with patch.object(jj.requests, "get", return_value=_FakeResponse(body)):
        text = jj.JustJoinSource().fetch_text(
            "https://justjoin.it/job-offer/itfs-intermediate-frontend-developer-warszawa-javascript"
        )
    assert "Required Skills: TypeScript (4), React (4)" in text
    assert "Nice to Have: Figma (1)" in text


def test_fetch_text_falls_back_to_the_legacy_skills_key() -> None:
    body = _offer(skills=[{"name": "Angular", "level": 5}], body="<p>Angular work.</p>")
    with patch.object(jj.requests, "get", return_value=_FakeResponse(body)):
        text = jj.JustJoinSource().fetch_text("https://justjoin.it/job-offer/x-angular-dev")
    assert "Required Skills: Angular (5)" in text
