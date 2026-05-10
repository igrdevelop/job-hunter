"""exclude_react_without_angular uses stack/tools (e.g. 4dayweek API)."""

from hunter.config import FILTER
from hunter.models import Job
from hunter.filters import _is_react_without_angular, apply_filters


def test_react_in_stack_triggers_exclude_when_no_angular() -> None:
    assert FILTER.get("exclude_react_without_angular") is True
    j = Job(
        title="Senior Frontend Engineer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://4dayweek.io/job/x",
        source="fourdayweek",
        raw={
            "skills": [{"name": "Communication", "slug": "x"}],
            "stack": [
                {"name": "TypeScript", "slug": "ts"},
                {"name": "React", "slug": "react"},
            ],
            "tools": [],
        },
    )
    assert _is_react_without_angular(j) is True


def test_react_and_angular_in_stack_not_excluded() -> None:
    j = Job(
        title="Frontend Engineer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://x",
        source="fourdayweek",
        raw={
            "stack": [
                {"name": "React", "slug": "react"},
                {"name": "Angular", "slug": "angular"},
            ],
        },
    )
    assert _is_react_without_angular(j) is False


def test_apply_filters_drops_react_stack_job() -> None:
    jobs = [
        Job(
            title="Staff Engineer, Frontend",
            company="BigCo",
            location="Remote",
            salary=None,
            url="https://4dayweek.io/job/y",
            source="fourdayweek",
            raw={"stack": [{"name": "React", "slug": "r"}]},
        )
    ]
    assert apply_filters(jobs) == []
