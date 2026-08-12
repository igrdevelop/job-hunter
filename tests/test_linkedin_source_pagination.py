"""Tests for LinkedInSource's guest-search pagination and query parameters.

Background (measured live 2026-08-12, see docs/AGENT_LOG.md): the guest endpoint
returns 10 cards per call, not 25. The old code stepped `start` by 25 and broke
out of the loop when a page returned fewer than 25 rows — so it always stopped
after ONE call and the source reported 10 postings where ~69 were available.
These tests pin the corrected contract.
"""

from unittest.mock import MagicMock, patch

from hunter.models import Job
from hunter.sources.linkedin import (
    MAX_PAGES_PER_KEYWORD,
    PAGE_STEP,
    LinkedInSource,
)


def _job(job_id: int) -> Job:
    return Job(
        title=f"Angular Developer {job_id}",
        company="ACME",
        location="Wrocław, Dolnośląskie, Poland",
        salary=None,
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        source="linkedin",
        raw={"jobId": str(job_id)},
    )


def _pages(*sizes: int) -> list[list[Job]]:
    """Build page payloads with globally unique job ids."""
    pages: list[list[Job]] = []
    next_id = 1000
    for size in sizes:
        pages.append([_job(next_id + i) for i in range(size)])
        next_id += 1000
    return pages


# ── pagination ───────────────────────────────────────────────────────────────


def test_walks_past_the_first_page():
    """The regression: one full page used to end the walk."""
    src = LinkedInSource()
    pages = _pages(PAGE_STEP, PAGE_STEP, 4, 0)

    with (
        patch.object(LinkedInSource, "_fetch_page", side_effect=pages) as fetch,
        patch("hunter.sources.linkedin.time.sleep"),
    ):
        jobs = src._search_keyword("angular", "105072130")

    assert len(jobs) == PAGE_STEP * 2 + 4
    assert fetch.call_count == 4


def test_short_page_does_not_stop_pagination():
    """A short-but-populated page is normal mid-sequence, not end-of-results.

    Measured live: ... 10, 10, 9, then an empty page. Treating "< page size" as
    the end is exactly the bug being fixed, so a short page in the MIDDLE must
    not truncate the walk.
    """
    src = LinkedInSource()
    pages = _pages(PAGE_STEP, 9, PAGE_STEP, 0)

    with (
        patch.object(LinkedInSource, "_fetch_page", side_effect=pages) as fetch,
        patch("hunter.sources.linkedin.time.sleep"),
    ):
        jobs = src._search_keyword("angular", "105072130")

    assert fetch.call_count == 4
    assert len(jobs) == PAGE_STEP + 9 + PAGE_STEP


def test_stops_on_empty_page():
    src = LinkedInSource()
    pages = _pages(PAGE_STEP, 0)

    with (
        patch.object(LinkedInSource, "_fetch_page", side_effect=pages) as fetch,
        patch("hunter.sources.linkedin.time.sleep"),
    ):
        jobs = src._search_keyword("angular", "105072130")

    assert fetch.call_count == 2
    assert len(jobs) == PAGE_STEP


def test_respects_the_page_cap():
    """A bottomless result set must not walk forever."""
    src = LinkedInSource()

    with (
        patch.object(
            LinkedInSource, "_fetch_page", side_effect=_pages(*([PAGE_STEP] * 50))
        ) as fetch,
        patch("hunter.sources.linkedin.time.sleep"),
    ):
        jobs = src._search_keyword("angular", "105072130")

    assert fetch.call_count == MAX_PAGES_PER_KEYWORD
    assert len(jobs) == MAX_PAGES_PER_KEYWORD * PAGE_STEP


def test_offsets_step_by_the_real_page_size():
    src = LinkedInSource()

    with (
        patch.object(
            LinkedInSource, "_fetch_page", side_effect=_pages(PAGE_STEP, PAGE_STEP, 0)
        ) as fetch,
        patch("hunter.sources.linkedin.time.sleep"),
    ):
        src._search_keyword("angular", "105072130")

    starts = [call.args[2] for call in fetch.call_args_list]
    assert starts == [0, PAGE_STEP, PAGE_STEP * 2]


def test_dedups_repeated_ids_across_pages():
    """Overlapping offsets can repeat a posting — one keyword's list stays clean."""
    src = LinkedInSource()
    repeated = [_job(7001), _job(7002)]
    pages = [repeated, [_job(7002), _job(7003)], []]

    with (
        patch.object(LinkedInSource, "_fetch_page", side_effect=pages),
        patch("hunter.sources.linkedin.time.sleep"),
    ):
        jobs = src._search_keyword("angular", "105072130")

    assert [j.raw["jobId"] for j in jobs] == ["7001", "7002", "7003"]


# ── query parameters ─────────────────────────────────────────────────────────


def _captured_params(env: dict | None = None) -> dict:
    src = LinkedInSource()
    resp = MagicMock(status_code=200, text="")
    with (
        patch("hunter.sources.linkedin.requests.get", return_value=resp) as get,
        patch.dict("os.environ", env or {}, clear=False),
    ):
        src._fetch_page("angular", "105072130", 0)
    return get.call_args.kwargs["params"]


def test_dead_filters_are_not_sent():
    """f_E and f_WT are measured no-ops on the guest endpoint — don't send them."""
    params = _captured_params()
    assert "f_E" not in params
    assert "f_WT" not in params


def test_window_defaults_to_seven_days():
    assert _captured_params()["f_TPR"] == "r604800"


def test_window_is_env_overridable():
    assert _captured_params({"LINKEDIN_TPR": "r86400"})["f_TPR"] == "r86400"


def test_blank_env_window_falls_back_to_default():
    assert _captured_params({"LINKEDIN_TPR": "   "})["f_TPR"] == "r604800"
