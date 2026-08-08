"""
Tests for the 2026-08-08 filter hardening (Sent-notes audit over the last 600
tracker rows — see the audit's category counts in the PR description):

1. Low-frequency-hybrid exception broadened: a hybrid role in ANY Polish city
   is kept when office visits are ~once a week or less (twice a month, monthly,
   quarterly, occasional) — the description's frequency phrasing wins over a
   bare "hybrid" header (owner decision 2026-08-08).
2. Doomed-gate HARD rule `pl_onsite_or_frequent_hybrid`: explicit ≥2-days/week
   phrasing, or strict on-site wording near a Polish city outside Wrocław.
3. RU-market hardening: RU city tokens at listing level, city location-tags and
   ruble salaries in the gate, RU intern titles in exclude_levels.
4. Doomed-gate HARD rule `foreign_stack_no_angular`: an unambiguous foreign
   stack (PHP/WordPress/.NET/…) in a posting that never mentions Angular/React.
5. Team-lead titles (EN + RU) in exclude_levels.
"""

import pytest

from hunter.filters import assess_job_text, classify_job
from hunter.models import Job


def _job(
    *,
    title: str,
    company: str = "Acme",
    location: str = "remote",
    body: str = "",
) -> Job:
    return Job(
        title=title,
        company=company,
        location=location,
        salary=None,
        url="https://example.com/job",
        source="test",
        raw={"description": body} if body else {},
    )


def _hard_rules(text: str, title: str = "") -> set[str]:
    return {f.rule for f in assess_job_text(text, title=title) if f.severity == "hard"}


# ── 1. Low-frequency hybrid kept (any Polish city, ≤ once a week) ─────────────


@pytest.mark.parametrize(
    "location,body",
    [
        ("Warszawa (Hybrid)", "Hybrid model: office visits twice a month."),
        ("Katowice", "Praca hybrydowa, wizyty w biurze raz w miesiącu."),
        ("Gdańsk (Hybrid)", "Hybrid with occasional office visits, mostly remote."),
        ("Kraków", "We meet in the office once a quarter."),
        ("Remote", "Hybrid — our office is in Poznań, kilka razy w miesiącu."),
    ],
)
def test_low_frequency_polish_hybrid_kept(location: str, body: str) -> None:
    job = _job(title="Angular Developer", location=location, body=body)
    assert classify_job(job) is None, f"{location!r}/{body!r} should be kept"


@pytest.mark.parametrize(
    "location,body",
    [
        # ≥2 days/week — too frequent
        ("Warszawa (Hybrid)", "Hybrid, 3 days a week in our Warszawa office."),
        # unspecified frequency — not enough to qualify
        ("Katowice (Hybrid)", "Hybrid work model based in Katowice."),
        # rare visits but to a FOREIGN city — not commutable
        ("Berlin (Hybrid)", "Hybrid, office visits once a month in Berlin."),
    ],
)
def test_frequent_or_foreign_hybrid_still_rejected(location: str, body: str) -> None:
    job = _job(title="Angular Developer", location=location, body=body)
    assert classify_job(job) == "location", f"{location!r}/{body!r} should be rejected"


# ── 2. Doomed-gate HARD: frequent office in a Polish city ─────────────────────


def test_gate_hard_on_multi_day_hybrid_warsaw() -> None:
    text = (
        "Angular Developer\nWe are hiring for our Warszawa office.\n"
        "Requirements: Angular, TypeScript.\n"
        "Work model: hybrid, 3 days each week in the office."
    )
    assert "pl_onsite_or_frequent_hybrid" in _hard_rules(text)


def test_gate_hard_on_stacjonarna_opole() -> None:
    text = (
        "Frontend Developer (Angular)\nPraca stacjonarna, biuro w Opolu.\n"
        "Wymagania: Angular, TypeScript, RxJS."
    )
    assert "pl_onsite_or_frequent_hybrid" in _hard_rules(text)


def test_gate_hard_on_onsite_torun() -> None:
    text = (
        "Senior Frontend Engineer (Angular)\nThis role is on-site in our Toruń office.\n"
        "Requirements: Angular, TypeScript."
    )
    assert "pl_onsite_or_frequent_hybrid" in _hard_rules(text)


def test_gate_low_frequency_body_vetoes_hybrid_header() -> None:
    # Header says hybrid + a day-count city, but the body clarifies rare visits.
    text = (
        "Angular Developer\nHybrid — Kraków office.\n"
        "In practice we meet raz w miesiącu; otherwise fully flexible remote work.\n"
        "Requirements: Angular, TypeScript."
    )
    assert "pl_onsite_or_frequent_hybrid" not in _hard_rules(text)


def test_gate_no_hard_on_bare_hybrid_pl_city() -> None:
    # Bare "hybrid" near a PL city with no frequency stays SOFT (M4 calibration:
    # flexible-hybrid Warsaw roles were sometimes acceptable) — must not be HARD.
    text = (
        "Angular Developer\nHybrid work model, office in Warszawa.\n"
        "Requirements: Angular, TypeScript."
    )
    assert "pl_onsite_or_frequent_hybrid" not in _hard_rules(text)


def test_gate_wroclaw_onsite_not_flagged() -> None:
    text = (
        "Angular Developer\nOn-site in our Wrocław office, 5 days a week.\nRequirements: Angular."
    )
    assert "pl_onsite_or_frequent_hybrid" not in _hard_rules(text)


# ── 3. RU market ──────────────────────────────────────────────────────────────


def test_moscow_location_rejected_at_listing() -> None:
    job = _job(title="Frontend-разработчик", location="Москва")
    assert classify_job(job) == "russia"


def test_ru_intern_title_rejected() -> None:
    job = _job(title="Стажер Frontend developer [MWS Octapi]")
    assert classify_job(job) == "level"


def test_gate_hard_on_ruble_salary() -> None:
    text = (
        "Frontend Developer\nМы ищем фронтенд-разработчика.\n"
        "Зарплата: 250 000 руб. на руки.\nСтек: Angular, TypeScript."
    )
    assert "russia_remote_market" in _hard_rules(text)


def test_gate_hard_on_moscow_location_tag() -> None:
    text = "Frontend Developer\nЛокация: Москва\nСтек: Angular, TypeScript."
    assert "russia_remote_market" in _hard_rules(text)


def test_gate_no_russia_on_plain_eu_posting() -> None:
    text = (
        "Angular Developer\nFully remote, EU-based company.\n"
        "Salary: 20 000 PLN. Requirements: Angular."
    )
    assert "russia_remote_market" not in _hard_rules(text)


# ── 4. Foreign stack with no Angular/React anywhere ───────────────────────────


def test_gate_hard_on_php_wordpress_no_angular() -> None:
    text = (
        "Web developer\nWe need an experienced developer for our agency.\n"
        "Requirements: PHP, WordPress, Joomla, HTML, CSS."
    )
    assert "foreign_stack_no_angular" in _hard_rules(text)


def test_gate_no_hard_when_angular_present() -> None:
    text = (
        "Frontend Developer\nOur stack: Angular on the frontend, "
        "some legacy WordPress pages.\nRequirements: Angular, TypeScript."
    )
    assert "foreign_stack_no_angular" not in _hard_rules(text)


def test_gate_no_hard_on_nice_to_have_stack() -> None:
    text = (
        "Frontend Developer\nRequirements: JavaScript, TypeScript, HTML, CSS.\n"
        "Nice to have: WordPress experience."
    )
    assert "foreign_stack_no_angular" not in _hard_rules(text)


def test_gate_dotnet_domain_link_not_flagged() -> None:
    text = (
        "Frontend Developer\nRequirements: JavaScript, TypeScript.\n"
        "Visit us at https://company.net for more information."
    )
    assert "foreign_stack_no_angular" not in _hard_rules(text)


# ── 5. Team-lead titles ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "Frontend Team Lead",
        "Angular Team Leader",
        "Тимлид Frontend",
        "Frontend разработчик (тим-лид)",
    ],
)
def test_team_lead_titles_rejected(title: str) -> None:
    assert classify_job(_job(title=title)) == "level"
