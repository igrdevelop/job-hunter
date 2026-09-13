"""Tests for tools/market_memory_m0.py (docs/MARKET_MEMORY_PLAN.md M0.a).

No network, no real DB: every test feeds hand-built ``hunter.models.Job``
lists into the pure ``summarise`` / ``evaluate_rules`` / dump round trip, or
stub sources into ``probe_sources``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hunter.filter_profile import load_profile
from hunter.models import Job
from hunter.tracker import normalize_url

TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "market_memory_m0", TOOLS_DIR / "market_memory_m0.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["market_memory_m0"] = module
    spec.loader.exec_module(module)
    return module


m0 = _load_module()

_BASE = "https://jobs.example.com"


def _job(title, location, salary, url, source, raw=None) -> Job:
    return Job(
        title=title,
        company="Acme",
        location=location,
        salary=salary,
        url=url,
        source=source,
        raw=raw or {},
    )


@pytest.fixture()
def flt():
    return load_profile()


@pytest.fixture()
def jobs_by_source() -> dict[str, list[Job]]:
    """3 fake sources, 12 jobs: duplicates within and across sources, an empty
    url, salaried/unsalaried, remote/hybrid/onsite/city-only locations, one
    level-rejected title, several that pass the default profile."""
    alpha = [
        _job(  # 1 passes; PLN B2B; remote; JustJoin-shaped skills
            "Senior Angular Developer",
            "Remote",
            "15 000 - 20 000 PLN B2B",
            f"{_BASE}/a1?utm_source=x",
            "alpha",
            raw={"requiredSkills": [{"name": "Angular"}], "skills": None},
        ),
        _job("Frontend Intern", "Wrocław (Hybrid)", None, f"{_BASE}/a2", "alpha"),  # 2 level
        _job("Angular Developer", "Kraków", "", "", "alpha"),  # 3 no url; location
        _job(  # 4 dup of a1 (utm stripped); salary present but unparsed
            "Senior Angular Developer", "remote", "competitive", f"{_BASE}/a1", "alpha"
        ),
        _job("Angular Engineer", "Poland (Remote)", "€5k-7k", f"{_BASE}/a5", "alpha"),  # 5
    ]
    beta = [
        _job(
            "Senior Angular Developer", "Remote", "$100k/yr", f"{_BASE}/a1", "beta"
        ),  # 6 x-src dup
        _job("Angular Developer", "Warszawa (On-site)", None, f"{_BASE}/b7", "beta"),  # 7 location
        _job("Angular Frontend Developer", "Remote", "20-25k PLN UoP", f"{_BASE}/b8", "beta"),  # 8
        _job("Angular Developer", "Somewhere", None, f"{_BASE}/b9", "beta"),  # 9 location; unknown
    ]
    gamma = [
        _job(  # 10 NoFluff-shaped technology key
            "Angular Developer",
            "Remote",
            None,
            f"{_BASE}/g10",
            "gamma",
            raw={"technology": "Angular"},
        ),
        _job("Angular Developer", "Remote", "brak danych", f"{_BASE}/g11", "gamma"),  # 11 unparsed
        _job("Angular Developer", "zdalnie", "od 18000 zł", f"{_BASE}/g12", "gamma"),  # 12
    ]
    return {"alpha": alpha, "beta": beta, "gamma": gamma}


@pytest.fixture()
def known() -> set[str]:
    return {normalize_url(f"{_BASE}/a1"), normalize_url(f"{_BASE}/b9")}


def _row(report, name):
    return next(r for r in report.sources if r.source == name)


# ── summarise: exact counts ───────────────────────────────────────────────────


def test_summarise_per_source_counts(jobs_by_source, known, flt):
    report = m0.summarise(jobs_by_source, known, flt)

    a = _row(report, "alpha")
    assert (a.raw, a.unique, a.no_url, a.known, a.new) == (5, 3, 1, 1, 2)
    assert (a.salary_present, a.salary_parsed) == (3, 2)
    assert (a.location_classified, a.city_present) == (4, 2)
    assert a.passed == 3
    assert a.skills_listing_present == 1
    assert a.skill_keys_found == {"requiredSkills": 1}  # the null ``skills`` key is not counted

    b = _row(report, "beta")
    assert (b.raw, b.unique, b.no_url, b.known, b.new) == (4, 4, 0, 2, 2)
    assert (b.salary_present, b.salary_parsed) == (2, 2)
    assert (b.location_classified, b.city_present) == (3, 1)
    assert b.passed == 2
    assert b.skills_listing_present == 0

    g = _row(report, "gamma")
    assert (g.raw, g.unique, g.known, g.new) == (3, 3, 0, 3)
    assert (g.salary_present, g.salary_parsed) == (2, 1)
    assert g.location_classified == 3
    assert g.passed == 3
    assert g.skill_keys_found == {"technology": 1}


def test_summarise_totals_dedup_across_sources(jobs_by_source, known, flt):
    """Totals dedup url_norm GLOBALLY — a1 appears in alpha twice and in beta
    once but is one unique (and one known) listing for the sweep."""
    report = m0.summarise(jobs_by_source, known, flt)
    t = report.totals
    assert t.source == "TOTAL"
    assert (t.raw, t.unique, t.known, t.new, t.no_url) == (12, 9, 2, 7, 1)
    assert (t.salary_present, t.salary_parsed) == (7, 5)
    assert (t.location_classified, t.city_present) == (10, 3)
    assert t.skills_listing_present == 2
    assert t.passed == 8
    assert t.skill_keys_found == {"requiredSkills": 1, "technology": 1}
    assert t.share("salary_parsed") == pytest.approx(5 / 12)
    assert t.share("location_classified") == pytest.approx(10 / 12)


def test_summarise_verdict_distribution(jobs_by_source, known, flt):
    report = m0.summarise(jobs_by_source, known, flt)
    assert report.verdicts == {"passed": 8, "level": 1, "location": 3}


def test_summarise_splits_and_samples(jobs_by_source, known, flt):
    report = m0.summarise(jobs_by_source, known, flt)
    assert report.currency_split == {"PLN": 3, "EUR": 1, "USD": 1}
    assert report.contract_split == {"b2b": 1, "uop": 1, "unspecified": 3}
    assert report.mode_split == {"remote": 8, "hybrid": 1, "onsite": 1, "unknown": 2}
    assert report.unparsed_salaries == ["competitive", "brak danych"]
    assert report.unknown_locations == ["Kraków", "Somewhere"]


def test_summarise_sample_cap(jobs_by_source, known, flt):
    report = m0.summarise(jobs_by_source, known, flt, max_samples=1)
    assert report.unparsed_salaries == ["competitive"]
    assert report.unknown_locations == ["Kraków"]


def test_summarise_keeps_an_errored_source_as_a_row(jobs_by_source, known, flt):
    report = m0.summarise(jobs_by_source, known, flt, errors={"delta": "Boom: 503"})
    d = _row(report, "delta")
    assert d.raw == 0 and d.error == "Boom: 503"
    assert d.share("salary_parsed") is None
    assert report.totals.raw == 12


def test_summarise_empty_input(flt):
    report = m0.summarise({}, set(), flt)
    assert report.totals.raw == 0 and report.sources == []
    assert report.verdicts == {}


# ── evaluate_rules: both sides of every threshold ────────────────────────────


def _synthetic_report(*, raw, new, salary_parsed, location_classified, known_available=True):
    totals = m0.SourceRow(
        source="TOTAL",
        raw=raw,
        unique=raw,
        known=raw - new,
        new=new,
        salary_parsed=salary_parsed,
        location_classified=location_classified,
    )
    return m0.Report(
        sources=[],
        totals=totals,
        verdicts={},
        currency_split={},
        contract_split={},
        mode_split={},
        unparsed_salaries=[],
        unknown_locations=[],
        known_available=known_available,
        known_note="synthetic",
        probed_at="",
    )


def _status(rules, prefix):
    return next(r.status for r in rules if r.rule.startswith(prefix))


@pytest.mark.parametrize(
    ("new", "expected"),
    [(30, "PASS"), (29, "FAIL"), (0, "FAIL"), (500, "PASS")],
)
def test_rule_new_listings_threshold(new, expected):
    report = _synthetic_report(raw=1000, new=new, salary_parsed=500, location_classified=800)
    assert _status(m0.evaluate_rules(report), "new listings") == expected


@pytest.mark.parametrize(
    ("parsed", "expected"),
    [(25, "PASS"), (24, "FAIL"), (0, "FAIL"), (100, "PASS")],
)
def test_rule_salary_threshold(parsed, expected):
    report = _synthetic_report(raw=100, new=50, salary_parsed=parsed, location_classified=80)
    rules = m0.evaluate_rules(report)
    assert _status(rules, "salary_parsed") == expected
    rule = next(r for r in rules if r.rule.startswith("salary_parsed"))
    assert rule.value == pytest.approx(parsed / 100)
    assert rule.threshold == m0.MIN_SALARY_PARSED_SHARE


@pytest.mark.parametrize(
    ("classified", "expected"),
    [(60, "PASS"), (59, "FAIL"), (0, "FAIL"), (100, "PASS")],
)
def test_rule_location_threshold(classified, expected):
    report = _synthetic_report(raw=100, new=50, salary_parsed=50, location_classified=classified)
    assert _status(m0.evaluate_rules(report), "location_classified") == expected


def test_rule_inserts_per_day_is_unmeasured_and_carries_new_share():
    report = _synthetic_report(raw=200, new=50, salary_parsed=50, location_classified=150)
    rule = next(r for r in m0.evaluate_rules(report) if r.rule.startswith("expected inserts"))
    assert rule.status == "UNMEASURED"
    assert rule.value == pytest.approx(0.25)
    assert rule.threshold == m0.MAX_INSERTS_PER_DAY
    assert "0.250" in rule.note


def test_rule_new_listings_unmeasured_without_known_set():
    report = _synthetic_report(
        raw=100, new=100, salary_parsed=50, location_classified=80, known_available=False
    )
    assert _status(m0.evaluate_rules(report), "new listings") == "UNMEASURED"


def test_rules_unmeasured_on_empty_probe():
    report = _synthetic_report(raw=0, new=0, salary_parsed=0, location_classified=0)
    rules = m0.evaluate_rules(report)
    assert _status(rules, "salary_parsed") == "UNMEASURED"
    assert _status(rules, "location_classified") == "UNMEASURED"
    assert _status(rules, "new listings") == "FAIL"  # 0 new is a real, measured 0


# ── dump -> from-dump round trip ─────────────────────────────────────────────


def test_dump_round_trip_reproduces_totals(tmp_path, jobs_by_source, known, flt):
    probed = {name: (jobs, "") for name, jobs in jobs_by_source.items()}
    probed["delta"] = ([], "RuntimeError: down")
    live = m0.summarise(jobs_by_source, known, flt, errors={"delta": "RuntimeError: down"})

    path = tmp_path / "probe.json"
    m0.write_dump(path, m0.build_dump(probed, flt))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["errors"] == {"delta": "RuntimeError: down"}
    assert set(data["sources"]) == {"alpha", "beta", "gamma", "delta"}
    first = data["jobs"][0]
    assert set(first) == {
        "source",
        "title",
        "company",
        "location",
        "salary",
        "url",
        "raw_keys",
        "verdict",
    }
    assert first["raw_keys"] == ["requiredSkills"]  # keys only, never the payload
    assert "raw" not in first
    assert first["verdict"] == "passed"

    rebuilt, errors, _ts = m0.load_dump(path)
    offline = m0.summarise(rebuilt, known, flt, errors=errors)

    assert offline.totals == live.totals
    assert offline.sources == live.sources
    assert offline.verdicts == live.verdicts
    assert offline.currency_split == live.currency_split
    assert offline.contract_split == live.contract_split
    assert offline.mode_split == live.mode_split
    assert offline.unparsed_salaries == live.unparsed_salaries
    assert offline.unknown_locations == live.unknown_locations


def test_from_dump_jobs_carry_no_payload(tmp_path, jobs_by_source, flt):
    probed = {name: (jobs, "") for name, jobs in jobs_by_source.items()}
    path = tmp_path / "probe.json"
    m0.write_dump(path, m0.build_dump(probed, flt))
    rebuilt, _errors, _ts = m0.load_dump(path)
    first = rebuilt["alpha"][0]
    assert set(first.raw) == {m0._RAW_KEYS_MARKER}
    assert m0.skill_keys_present(first) == ["requiredSkills"]


# ── probe_sources: one raising source never kills the others ─────────────────


class _StubSource:
    def __init__(self, name, jobs=None, exc=None):
        self.name = name
        self._jobs = jobs or []
        self._exc = exc

    def search(self):
        if self._exc is not None:
            raise self._exc
        return list(self._jobs)


def test_probe_sources_isolates_a_raising_source():
    ok_job = _job("Angular Developer", "Remote", None, f"{_BASE}/p1", "good")
    sources = [
        _StubSource("good", jobs=[ok_job]),
        _StubSource("bad", exc=RuntimeError("HTTP 503")),
        _StubSource("also_good", jobs=[ok_job, ok_job]),
    ]
    probed = m0.probe_sources(sources)
    assert list(probed) == ["good", "bad", "also_good"]
    assert probed["good"] == ([ok_job], "")
    assert probed["bad"][0] == [] and probed["bad"][1] == "RuntimeError: HTTP 503"
    assert len(probed["also_good"][0]) == 2


def test_probe_sources_treats_none_as_empty():
    class _NoneSource:
        name = "nothing"

        def search(self):
            return None

    assert m0.probe_sources([_NoneSource()]) == {"nothing": ([], "")}


# ── skill keys ──────────────────────────────────────────────────────────────


def test_skill_keys_present_ignores_empty_values():
    job = _job(
        "Angular Developer",
        "Remote",
        None,
        f"{_BASE}/s1",
        "x",
        raw={
            "requiredSkills": [],
            "niceToHaveSkills": None,
            "technologies": ["Angular"],
            "technologyTags": ["TypeScript"],
            "mainTechnology": "Angular",
            "unrelated": "value",
        },
    )
    assert m0.skill_keys_present(job) == ["technologies", "technologyTags", "mainTechnology"]
    assert m0.nonempty_raw_keys(job) == [
        "technologies",
        "technologyTags",
        "mainTechnology",
        "unrelated",
    ]


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_main_rejects_dump_with_from_dump(tmp_path, capsys):
    rc = m0.main(["--dump", str(tmp_path / "a.json"), "--from-dump", str(tmp_path / "b.json")])
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_rejects_missing_dump(tmp_path, capsys):
    rc = m0.main(["--from-dump", str(tmp_path / "missing.json")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_main_rejects_unknown_source(capsys):
    rc = m0.main(["--sources", "no-such-source"])
    assert rc == 2
    assert "unknown source" in capsys.readouterr().err


def test_main_from_dump_text_and_json(tmp_path, jobs_by_source, known, flt, monkeypatch, capsys):
    probed = {name: (jobs, "") for name, jobs in jobs_by_source.items()}
    path = tmp_path / "probe.json"
    m0.write_dump(path, m0.build_dump(probed, flt))
    monkeypatch.setattr(m0, "load_known_urls", lambda: (known, True, "2 known (stub)"))

    assert m0.main(["--from-dump", str(path), "--unparsed-salaries", "1"]) == 0
    text = capsys.readouterr().out
    assert "TOTAL" in text and "raw=12 unique=9 known=2 new=7" in text
    assert "Unparsed salary samples (1 shown)" in text
    assert "SELECT substr(ts,1,10)" in text  # the M0.b SQL is printed verbatim
    assert "[FAIL      ] new listings" in text  # 7 < 30

    assert m0.main(["--from-dump", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["new"] == 7
    assert data["verdicts"] == {"passed": 8, "level": 1, "location": 3}
    assert [r["status"] for r in data["rules"]] == ["FAIL", "PASS", "PASS", "UNMEASURED"]
    assert data["m0b"]["sql"] == m0.M0B_SQL
