"""M2 filter-profile generalizations (docs/FILTERS_YAML_PLAN.md).

Legacy-key aliases in the loader + a non-default ``flt`` flipping classify_job.
"""

from __future__ import annotations

import copy

import yaml

from hunter import candidate
from hunter.filter_profile import (
    builtin_defaults,
    clear_profile_cache,
    load_profile,
)
from hunter.filters import classify_job
from hunter.models import Job


def _job(title: str, **kw) -> Job:
    return Job(
        title=title,
        company=kw.get("company", "Acme"),
        location=kw.get("location", "Remote"),
        salary=None,
        url="https://example.com/x",
        source="test",
        raw=kw.get("raw", {}),
    )


def setup_function():
    candidate._set_path(None)
    clear_profile_cache()


def teardown_function():
    candidate._set_path(None)
    clear_profile_cache()


def test_legacy_require_angular_maps_to_require_title_terms(tmp_path):
    path = tmp_path / "filters.yaml"
    path.write_text(yaml.safe_dump({"require_angular": True}), encoding="utf-8")
    profile = load_profile(path)
    assert profile["require_title_terms"] == ["angular"]
    assert profile["require_angular"] is True


def test_legacy_exclude_react_without_angular_false_disables_rule(tmp_path):
    path = tmp_path / "filters.yaml"
    path.write_text(
        yaml.safe_dump({"exclude_react_without_angular": False}),
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile["exclude_stacks_without"] is None
    assert profile["exclude_react_without_angular"] is False


def test_canonical_require_title_terms_wins_over_legacy(tmp_path):
    path = tmp_path / "filters.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "require_angular": True,
                "require_title_terms": ["react"],
            }
        ),
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile["require_title_terms"] == ["react"]
    # Scrapers gate on require_angular ≡ "angular" in terms — not "any term".
    assert profile["require_angular"] is False


def test_require_angular_true_only_when_angular_in_terms(tmp_path):
    path = tmp_path / "filters.yaml"
    path.write_text(
        yaml.safe_dump({"require_title_terms": ["angular", "rxjs"]}),
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile["require_angular"] is True


def test_nondefault_flt_flips_classify_verdict():
    """A React-only title passes under a React-seeker profile, fails under default.

    Mutation-verify target: if classify_job ignores ``flt``, this assertion fails.
    """
    job = _job("Senior Frontend React Developer")
    assert classify_job(job) == "react_no_angular"

    react_flt = copy.deepcopy(builtin_defaults())
    react_flt["exclude_stacks_without"] = None
    react_flt["exclude_react_without_angular"] = False
    react_flt["title_keywords"] = ["react", "frontend", "javascript", "typescript"]
    assert classify_job(job, flt=react_flt) is None


def test_require_title_terms_via_flt():
    job = _job("Senior Frontend Developer")  # no angular
    assert classify_job(job) is None  # default require_title_terms=[]

    strict = copy.deepcopy(builtin_defaults())
    strict["require_title_terms"] = ["angular"]
    strict["require_angular"] = True
    assert classify_job(job, flt=strict) == "require_angular"
