#!/usr/bin/env python3
"""One-off: generate tests/fixtures/filter_parity/golden_verdicts.json.

Run against CURRENT (pre-filter_profile) code BEFORE moving FILTER into
hunter/filter_profile.py — see docs/FILTERS_YAML_PLAN.md M1. The parity test
replays the same corpus after the move and asserts every verdict/reason is
identical.

Re-running after the move is fine for regeneration only if you trust the new
code; the committed golden is the freeze of pre-refactor behavior.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hunter.filters import assess_job_text, classify_job, screen_job_text
from hunter.models import Job

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "tests" / "fixtures" / "sample_jobs"
OUT = ROOT / "tests" / "fixtures" / "filter_parity" / "golden_verdicts.json"

_HEADER_RE = {
    "title": re.compile(r"^Job Title:\s*(.+)$", re.M),
    "company": re.compile(r"^Company:\s*(.+)$", re.M),
    "location": re.compile(r"^Location:\s*(.+)$", re.M),
}


def _job(
    *,
    title: str,
    company: str = "Acme",
    location: str = "Remote",
    body: str = "",
    source: str = "test",
) -> Job:
    return Job(
        title=title,
        company=company,
        location=location,
        salary=None,
        url=f"https://example.com/{title[:40]}",
        source=source,
        raw={"description": body} if body else {},
    )


def _findings(text: str, *, title: str = "", company: str = "") -> list[dict]:
    return [
        {"rule": f.rule, "severity": f.severity, "evidence": f.evidence}
        for f in assess_job_text(text, title=title, company=company)
    ]


def _parse_sample(path: Path) -> tuple[str, str, str, str]:
    text = path.read_text(encoding="utf-8")
    title_m = _HEADER_RE["title"].search(text)
    company_m = _HEADER_RE["company"].search(text)
    location_m = _HEADER_RE["location"].search(text)
    title = title_m.group(1).strip() if title_m else path.stem
    company = company_m.group(1).strip() if company_m else "Unknown"
    location = location_m.group(1).strip() if location_m else "Remote"
    return title, company, location, text


def _synthetic_cases() -> list[dict]:
    """One (or a few) cases per rule family named in FILTERS_YAML_PLAN M1."""
    cases: list[dict] = []

    def add_classify(cid: str, job: Job, note: str = "") -> None:
        cases.append(
            {
                "id": cid,
                "kind": "classify",
                "note": note,
                "input": {
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "body": (job.raw or {}).get("description", ""),
                    "source": job.source,
                },
                "verdict": classify_job(job),
            }
        )

    def add_screen(
        cid: str, text: str, *, title: str = "", company: str = "", note: str = ""
    ) -> None:
        cases.append(
            {
                "id": cid,
                "kind": "screen",
                "note": note,
                "input": {"text": text, "title": title, "company": company},
                "verdict": screen_job_text(text, title=title, company=company),
            }
        )

    def add_assess(
        cid: str, text: str, *, title: str = "", company: str = "", note: str = ""
    ) -> None:
        cases.append(
            {
                "id": cid,
                "kind": "assess",
                "note": note,
                "input": {"text": text, "title": title, "company": company},
                "verdict": _findings(text, title=title, company=company),
            }
        )

    # ── classify: happy path ──────────────────────────────────────────────
    add_classify("classify_pass_angular", _job(title="Senior Angular Developer"))

    # exclude_levels — sample of EN + RU
    for lvl, title in [
        ("junior", "Junior Angular Developer"),
        ("intern", "Angular Intern"),
        ("tech_lead", "Angular Tech Lead"),
        ("team_lead", "Frontend Team Lead Angular"),
        ("ru_techlead", "Техлид Frontend Angular"),
        ("ru_intern", "Стажер Frontend developer"),
        ("cto", "CTO / Angular Architect"),
    ]:
        add_classify(f"classify_level_{lvl}", _job(title=title), note="exclude_levels")

    # exclude_patterns — incl. the tricky \\bc#
    for pid, title in [
        ("java", "Java Frontend Developer"),
        ("csharp", "C# Frontend Developer"),
        ("php", "PHP Frontend Developer"),
        ("vue", "Vue Frontend Developer"),
        ("wordpress", "WordPress Frontend Developer"),
        ("react_native", "React Native Frontend Developer"),
    ]:
        add_classify(f"classify_pattern_{pid}", _job(title=title), note="exclude_patterns")

    # react-without-angular
    add_classify(
        "classify_react_only",
        _job(title="Senior React Developer"),
        note="react_without_angular",
    )
    add_classify(
        "classify_react_with_angular",
        _job(title="Senior React / Angular Developer"),
        note="react_with_angular_kept",
    )

    # fullstack ± angular ± backend
    add_classify(
        "classify_fullstack_no_angular",
        _job(title="Fullstack Developer"),
        note="fullstack_without_angular",
    )
    add_classify(
        "classify_fullstack_angular_java",
        _job(title="Fullstack Angular / Java Developer"),
        note="fullstack_with_backend",
    )
    add_classify(
        "classify_fullstack_angular_node",
        _job(title="Fullstack Angular / Node Developer"),
        note="fullstack_angular_node_kept",
    )

    # German required / not required
    add_classify(
        "classify_german_title",
        _job(title="Frontend Developer with German"),
        note="german_required",
    )
    add_classify(
        "classify_german_body",
        _job(
            title="Angular Developer",
            body="Requirements: fluent in German, Angular, TypeScript.",
        ),
        note="german_required_body",
    )
    add_classify(
        "classify_german_not_required",
        _job(
            title="Angular Developer",
            body="English is the working language. German not required.",
        ),
        note="german_not_required",
    )

    # onsite / hybrid city
    add_classify(
        "classify_hybrid_warsaw_frequent",
        _job(
            title="Angular Developer",
            location="Warszawa (Hybrid)",
            body="Hybrid, 3 days a week in our Warszawa office.",
        ),
        note="onsite_city_frequent",
    )
    add_classify(
        "classify_hybrid_warsaw_low_freq",
        _job(
            title="Angular Developer",
            location="Warszawa (Hybrid)",
            body="Hybrid model: office visits twice a month.",
        ),
        note="low_frequency_hybrid_kept",
    )
    add_classify(
        "classify_hybrid_berlin",
        _job(
            title="Angular Developer",
            location="Berlin (Hybrid)",
            body="Hybrid, office visits once a month in Berlin.",
        ),
        note="foreign_hybrid_rejected",
    )

    # AI-mill company
    add_classify(
        "classify_ai_mill",
        _job(title="Angular Developer", company="micro1"),
        note="exclude_companies",
    )

    # RU tech-lead / market
    add_classify(
        "classify_russia_location",
        _job(title="Angular Developer", location="Remote · Russia"),
        note="russia_market",
    )

    # title_kw miss
    add_classify("classify_title_kw_miss", _job(title="Plumber"))

    # ── screen / assess: doomed-gate rule families ─────────────────────────
    add_screen(
        "screen_hybrid_warsaw_3days",
        "Angular Developer\nHybrid, 3 days a week in our Warszawa office.\nRequirements: Angular.",
        title="Angular Developer",
        note="screen_frequent_hybrid",
    )
    add_assess(
        "assess_hybrid_warsaw_3days",
        "Angular Developer\nHybrid, 3 days a week in our Warszawa office.\nRequirements: Angular, TypeScript.",
        title="Angular Developer",
        note="pl_onsite_or_frequent_hybrid",
    )
    add_assess(
        "assess_hybrid_low_freq",
        "Angular Developer\nHybrid — Kraków office.\n"
        "In practice we meet raz w miesiącu; otherwise fully flexible remote work.\n"
        "Requirements: Angular, TypeScript.",
        title="Angular Developer",
        note="low_frequency_veto",
    )
    add_assess(
        "assess_german_required",
        "Frontend Developer\nRequirements: Angular. German C1 required.",
        title="Frontend Developer",
        note="german_hard",
    )
    add_assess(
        "assess_foreign_stack_php",
        "Web developer\nRequirements: PHP, WordPress, Joomla, HTML, CSS.",
        title="Web developer",
        note="foreign_stack_no_angular",
    )
    add_assess(
        "assess_ai_mill_body",
        "Angular Developer\nApply via micro1.com — AI training data labeling role.\nRequirements: Angular.",
        title="Angular Developer",
        company="QuikHireStaffing",
        note="ai_mill_body",
    )
    add_assess(
        "assess_pass_angular_remote",
        "Angular Developer\nFully remote, EU-based.\nRequirements: Angular, TypeScript, RxJS.",
        title="Angular Developer",
        note="clean_pass",
    )

    return cases


def _sample_cases() -> list[dict]:
    cases: list[dict] = []
    for path in sorted(SAMPLE_DIR.glob("*.txt")):
        title, company, location, text = _parse_sample(path)
        job = _job(title=title, company=company, location=location, body=text)
        cid = f"sample_{path.stem}"
        cases.append(
            {
                "id": f"{cid}_classify",
                "kind": "classify",
                "note": f"sample_jobs/{path.name}",
                "input": {
                    "title": title,
                    "company": company,
                    "location": location,
                    "body": text,
                    "source": "test",
                },
                "verdict": classify_job(job),
            }
        )
        cases.append(
            {
                "id": f"{cid}_screen",
                "kind": "screen",
                "note": f"sample_jobs/{path.name}",
                "input": {"text": text, "title": title, "company": company},
                "verdict": screen_job_text(text, title=title, company=company),
            }
        )
        cases.append(
            {
                "id": f"{cid}_assess",
                "kind": "assess",
                "note": f"sample_jobs/{path.name}",
                "input": {"text": text, "title": title, "company": company},
                "verdict": _findings(text, title=title, company=company),
            }
        )
    return cases


def main() -> None:
    cases = _synthetic_cases() + _sample_cases()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "description": (
            "Frozen filter verdicts generated against pre-filter_profile code "
            "(docs/FILTERS_YAML_PLAN.md M1). Do not hand-edit."
        ),
        "cases": cases,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(cases)} cases -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
