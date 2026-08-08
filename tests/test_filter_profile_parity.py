"""Quality-parity guards for filter_profile M1 (docs/FILTERS_YAML_PLAN.md).

1. Dict equality — load_profile() with no user file matches the frozen
   pre-refactor FILTER snapshot (builtin_expected.json).
2. Golden verdict parity — replay classify_job / screen_job_text /
   assess_job_text over the corpus frozen in golden_verdicts.json (generated
   against pre-refactor code by tools/gen_filter_parity_golden.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter import candidate
from hunter.filters import assess_job_text, classify_job, screen_job_text
from hunter.filter_profile import clear_profile_cache, load_profile
from hunter.models import Job

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "filter_parity"
EXPECTED_PATH = FIXTURE_DIR / "builtin_expected.json"
GOLDEN_PATH = FIXTURE_DIR / "golden_verdicts.json"


@pytest.fixture(autouse=True)
def _pin_candidate_defaults(tmp_path):
    """Force missing candidate.yaml so locations match the frozen snapshot."""
    candidate._set_path(tmp_path / "missing_candidate.yaml")
    clear_profile_cache()
    yield
    candidate._set_path(None)
    clear_profile_cache()


def test_load_profile_equals_frozen_builtin():
    """Dict equality: no user file ⇒ byte-for-byte today's FILTER."""
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    assert load_profile() == expected


def _replay_case(case: dict):
    kind = case["kind"]
    inp = case["input"]
    if kind == "classify":
        job = Job(
            title=inp["title"],
            company=inp.get("company", "Acme"),
            location=inp.get("location", "Remote"),
            salary=None,
            url=f"https://example.com/{case['id']}",
            source=inp.get("source", "test"),
            raw={"description": inp["body"]} if inp.get("body") else {},
        )
        return classify_job(job)
    if kind == "screen":
        return screen_job_text(
            inp["text"],
            title=inp.get("title", ""),
            company=inp.get("company", ""),
        )
    if kind == "assess":
        return [
            {"rule": f.rule, "severity": f.severity, "evidence": f.evidence}
            for f in assess_job_text(
                inp["text"],
                title=inp.get("title", ""),
                company=inp.get("company", ""),
            )
        ]
    raise AssertionError(f"unknown kind {kind!r}")


def test_golden_verdict_parity():
    """Every frozen pre-refactor verdict/reason is identical after the move."""
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    mismatches: list[str] = []
    for case in payload["cases"]:
        got = _replay_case(case)
        if got != case["verdict"]:
            mismatches.append(
                f"{case['id']} ({case['kind']}): got={got!r} expected={case['verdict']!r}"
            )
    assert not mismatches, "parity drift:\n" + "\n".join(mismatches)
