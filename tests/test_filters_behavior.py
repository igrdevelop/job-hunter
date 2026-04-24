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


# ── Hybrid city logic ─────────────────────────────────────────────────────────

def test_apply_filters_accepts_wroclaw_hybrid() -> None:
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Wrocław (Hybrid)")]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1


def test_apply_filters_accepts_wroclaw_hybrid_ascii() -> None:
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Wroclaw (Hybrid)")]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1


def test_apply_filters_rejects_krakow_hybrid() -> None:
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Kraków (Hybrid)")]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_rejects_warszawa_hybrid() -> None:
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Warszawa (Hybrid)")]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_accepts_remote_any_city() -> None:
    """Fully remote jobs are always accepted regardless of listed city."""
    jobs = [
        _job(title="Senior Frontend Developer (Angular)", location="Remote"),
        _job(title="Senior Angular Developer", location="Kraków (Remote)"),
        _job(title="Frontend Developer Angular", location="zdalnie"),
    ]
    filtered = apply_filters(jobs)
    assert len(filtered) == 3


def test_apply_filters_rejects_pracuj_hybrydowa_krakow() -> None:
    """Regression: Pracuj-style 'Kraków - praca hybrydowa' must be rejected."""
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Kraków - praca hybrydowa")]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_accepts_pracuj_hybrydowa_wroclaw() -> None:
    """Pracuj-style 'Wrocław - praca hybrydowa' must be accepted."""
    jobs = [_job(title="Senior Frontend Developer (Angular)", location="Wrocław - praca hybrydowa")]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1


def test_apply_filters_rejects_qa_automation_roles() -> None:
    jobs = [
        _job(title="QA Automation Engineer (JavaScript)", location="Remote"),
        _job(title="SDET Frontend Engineer", location="Wroclaw (Hybrid)"),
    ]
    filtered = apply_filters(jobs)
    assert filtered == []


# ── German language requirement ───────────────────────────────────────────────


def test_apply_filters_rejects_fluent_german_in_description() -> None:
    jobs = [
        _job(
            title="Senior Frontend Developer (Angular)",
            location="Remote",
            raw={"description": "<p>We need someone fluent in German for client calls.</p>"},
        )
    ]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_rejects_german_c1_in_title() -> None:
    jobs = [_job(title="Frontend Developer (Angular, German C1)", location="Remote")]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_keeps_english_working_language_exemption() -> None:
    jobs = [
        _job(
            title="Senior Frontend Developer (Angular)",
            location="Berlin (Remote)",
            raw={
                "description": (
                    "English is the company language. "
                    "Knowledge of German is not required. "
                ),
            },
        )
    ]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1


def test_apply_filters_rejects_polish_niemiecki_wymagany() -> None:
    jobs = [
        _job(
            title="Frontend Developer Angular",
            location="Remote",
            raw={"description": "Wymagany język niemiecki min. B2."},
        )
    ]
    filtered = apply_filters(jobs)
    assert filtered == []


def test_apply_filters_allows_german_when_filter_disabled(monkeypatch) -> None:
    import hunter.config as cfg

    monkeypatch.setitem(cfg.FILTER, "exclude_german_language_required", False)
    jobs = [
        _job(
            title="Senior Frontend Developer (Angular)",
            location="Remote",
            raw={"description": "Fluent in German required."},
        )
    ]
    filtered = apply_filters(jobs)
    assert len(filtered) == 1
