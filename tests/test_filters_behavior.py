from hunter.filters import apply_filters
from hunter.models import Job


def _job(
    *,
    title: str,
    location: str = "Wroclaw (Hybrid)",
    raw: dict | None = None,
) -> Job:
    return Job(
        title=title,
        company="Acme",
        location=location,
        salary=None,
        url=f"https://example.com/{title.replace(' ', '-').lower()}",
        source="test",
        raw=raw or {},
    )


def test_apply_filters_allows_angular_frontend_in_allowed_location() -> None:
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Wroclaw (Hybrid)")]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1


def test_apply_filters_rejects_react_only_role() -> None:
    jobs = [
        _job(
            title="Senior Frontend Developer",
            raw={"skills": [{"name": "React"}], "technology": "React"},
        )
    ]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_keeps_react_plus_angular_role() -> None:
    jobs = [
        _job(
            title="Senior Frontend Developer",
            raw={"skills": [{"name": "React"}, {"name": "Angular"}]},
        )
    ]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1


def test_apply_filters_rejects_disallowed_location() -> None:
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Berlin (On-site)")]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_rejects_qa_automation_roles() -> None:
    jobs = [
        _job(title="QA Automation Engineer (JavaScript)", location="Remote"),
        _job(title="SDET Frontend Engineer", location="Wroclaw (Hybrid)"),
    ]
    filtered = apply_filters(jobs)
    assert filtered == []
