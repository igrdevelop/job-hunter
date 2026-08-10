"""JustJoin listing pagination (no network).

Regression for the 2026-08 API change: the server fixed its page size at 10
(``perPage`` ignored) and stopped honoring the ``cursor`` request param —
every page returned the same first 10 offers, so the source silently shrank
to ~1-2 jobs/run. The offset must be sent as ``from`` (``meta.next.cursor``
still carries the next offset value), and the loop budget is counted in
offers scanned so a server-side page-size change can't shrink coverage again.
"""

from unittest.mock import patch

from hunter.sources import justjoin as jj


def _offer(slug: str) -> dict:
    return {
        "slug": slug,
        "title": f"Angular Developer {slug}",
        "companyName": "Acme",
        "city": "Warszawa",
        "workplaceType": "remote",
        "employmentTypes": [],
    }


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


def _make_fake_get(pages: dict[int, list[dict]], calls: list[dict]):
    """Serve pages keyed by the ``from`` offset, mimicking the live API:
    fixed page size, ``cursor`` param ignored, ``meta.next.cursor`` = next offset."""

    def fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        calls.append(dict(params))
        offset = int(params.get("from", 0))
        data = pages.get(offset, [])
        next_offset = offset + len(data)
        has_next = next_offset in pages
        meta = {
            "from": offset,
            "next": {
                "cursor": next_offset if has_next else None,
                "itemsCount": len(pages.get(next_offset, [])),
            },
        }
        return _FakeResponse({"data": data, "meta": meta})

    return fake_get


def _run_search(monkeypatch, pages: dict[int, list[dict]]) -> tuple[list, list[dict]]:
    calls: list[dict] = []
    monkeypatch.setattr(jj, "FILTER", {"require_angular": False, "title_keywords": ["angular"]})
    monkeypatch.setattr(jj, "WORKPLACE_TYPES", ["remote"])
    with (
        patch.object(jj.requests, "get", side_effect=_make_fake_get(pages, calls)),
        patch.object(jj.time, "sleep"),
    ):
        jobs = jj.JustJoinSource().search()
    return jobs, calls


def test_paginates_via_from_offset(monkeypatch) -> None:
    pages = {
        0: [_offer(f"acme-angular-{i}") for i in range(10)],
        10: [_offer(f"acme-angular-{i}") for i in range(10, 20)],
        20: [_offer(f"acme-angular-{i}") for i in range(20, 25)],
    }
    jobs, calls = _run_search(monkeypatch, pages)

    assert len(jobs) == 25  # all three pages collected, not 10 copies of page one
    assert [c.get("from") for c in calls] == [0, 10, 20]
    assert all("cursor" not in c for c in calls)  # the old param name is dead


def test_offer_budget_counts_offers_not_pages(monkeypatch) -> None:
    monkeypatch.setattr(jj, "PER_PAGE", 10)
    monkeypatch.setattr(jj, "MAX_PAGES", 2)  # budget = 20 offers
    pages = {i * 10: [_offer(f"acme-angular-{i}-{j}") for j in range(10)] for i in range(5)}
    jobs, calls = _run_search(monkeypatch, pages)

    assert len(calls) == 2  # stopped by the offer budget, not by running out of pages
    assert len(jobs) == 20


def test_stops_on_empty_page(monkeypatch) -> None:
    jobs, calls = _run_search(monkeypatch, {0: []})
    assert jobs == []
    assert len(calls) == 1
