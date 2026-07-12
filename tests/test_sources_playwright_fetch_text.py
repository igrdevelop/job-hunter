"""Tests for fetch_text on Playwright-heavy sources (Phase 3.2e).

Covers: linkedin, inhire, jobleads.

These three rely on optional Playwright + cloudscraper for detail fetching.
The tests mock those external dependencies and exercise:
  * matches_url
  * the html_fallback fallback path (works without Playwright installed)
  * the cloudscraper happy path where applicable
  * JobLeadsCloudflareError when nothing works
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from hunter.sources.inhire import InhireSource
from hunter.sources.jobleads import (
    JOBLEADS_PASTE_MARKER,
    JobLeadsCloudflareError,
    JobLeadsSource,
    try_load_manual_job_posting,
)
from hunter.sources.linkedin import LinkedInSource


def _mk_html_response(text: str, status: int = 200, final_url: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.url = final_url or "https://example.com"
    resp.raise_for_status = MagicMock()
    if status >= 400 and status != 403:  # 403 handled specially by jobleads
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


# ── LinkedIn ────────────────────────────────────────────────────────────────


def test_linkedin_matches_url() -> None:
    s = LinkedInSource()
    assert s.matches_url("https://www.linkedin.com/jobs/view/12345/")
    assert s.matches_url("https://linkedin.com/jobs/x")
    assert not s.matches_url("https://example.com/x")


def test_linkedin_fetch_text_falls_back_when_no_playwright(monkeypatch) -> None:
    """If playwright is not installed, default fetch_text must fall back to fetch_html."""
    import builtins

    real_import = builtins.__import__

    def fail_on_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_on_playwright)

    with patch(
        "hunter.sources.html_fallback.fetch_html",
        return_value="fallback ok",
    ) as m:
        out = LinkedInSource().fetch_text("https://www.linkedin.com/jobs/view/123/")
    assert out == "fallback ok"
    m.assert_called_once()


def test_linkedin_fetch_text_falls_back_when_no_storage_state(monkeypatch) -> None:
    """If LINKEDIN_STORAGE_STATE is not set, fall back to fetch_html."""
    monkeypatch.delenv("LINKEDIN_STORAGE_STATE", raising=False)

    # Stub a fake playwright so the storage-state branch is reached
    fake_pw = MagicMock()
    with patch.dict("sys.modules", {"playwright": fake_pw, "playwright.sync_api": fake_pw}):
        with patch(
            "hunter.sources.html_fallback.fetch_html",
            return_value="fallback ok",
        ) as m:
            out = LinkedInSource().fetch_text("https://www.linkedin.com/jobs/view/123/")
    assert out == "fallback ok"
    m.assert_called_once()


# ── Inhire ──────────────────────────────────────────────────────────────────


def test_inhire_matches_url() -> None:
    s = InhireSource()
    assert s.matches_url("https://app.inhire.io/oferty-pracy/x")
    assert s.matches_url("https://inhire.io/x")
    assert not s.matches_url("https://example.com/x")


def test_inhire_fetch_text_from_json_ld() -> None:
    ld = {
        "@type": "JobPosting",
        "title": "Senior Frontend Developer",
        "hiringOrganization": {"name": "ExampleCo"},
        "description": "<p>" + ("Build cool stuff. " * 8) + "</p>",
    }
    html = f'<html><body><script type="application/ld+json">{json.dumps(ld)}</script></body></html>'
    fake_scraper = MagicMock()
    fake_scraper.get.return_value = _mk_html_response(html)
    fake_cs = MagicMock()
    fake_cs.create_scraper.return_value = fake_scraper

    with patch.dict("sys.modules", {"cloudscraper": fake_cs}):
        out = InhireSource().fetch_text("https://app.inhire.io/oferty-pracy/x")
    assert "Senior Frontend Developer" in out
    assert "ExampleCo" in out
    assert "Build cool stuff" in out


def test_inhire_fetch_text_falls_back_without_cloudscraper(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fail_on_cs(name, *args, **kwargs):
        if name == "cloudscraper":
            raise ImportError("no cloudscraper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_on_cs)

    with patch(
        "hunter.sources.html_fallback.fetch_html",
        return_value="fallback ok",
    ) as m:
        out = InhireSource().fetch_text("https://app.inhire.io/x")
    assert out == "fallback ok"
    m.assert_called_once()


# ── JobLeads ────────────────────────────────────────────────────────────────


def test_jobleads_matches_url() -> None:
    s = JobLeadsSource()
    assert s.matches_url("https://www.jobleads.com/pl/job/x")
    assert s.matches_url("https://jobleads.com/pl/jobs?q=x")
    assert not s.matches_url("https://example.com/x")


def test_try_load_manual_job_posting_returns_none_when_no_file(tmp_path, monkeypatch) -> None:
    # Make manual_jobleads_job_posting_path return a path that doesn't exist
    from hunter import tracker as _tracker

    monkeypatch.setattr(
        _tracker, "manual_jobleads_job_posting_path", lambda url: tmp_path / "nope.txt"
    )
    assert try_load_manual_job_posting("https://www.jobleads.com/pl/job/x") is None


def test_try_load_manual_job_posting_returns_body_with_marker(tmp_path, monkeypatch) -> None:
    p = tmp_path / "job_posting.txt"
    body_text = "Senior Angular Developer at ExampleCo\n" + ("Real content. " * 30)
    p.write_text(
        f"URL: https://www.jobleads.com/pl/job/x\n\n{JOBLEADS_PASTE_MARKER}\n{body_text}",
        encoding="utf-8",
    )
    from hunter import tracker as _tracker

    monkeypatch.setattr(_tracker, "manual_jobleads_job_posting_path", lambda url: p)
    out = try_load_manual_job_posting("https://www.jobleads.com/pl/job/x")
    assert out is not None
    assert "Senior Angular Developer" in out


def test_jobleads_fetch_text_uses_manual_when_available(tmp_path, monkeypatch) -> None:
    p = tmp_path / "job_posting.txt"
    body_text = "Senior Angular Developer at ExampleCo\n" + ("Real content. " * 30)
    p.write_text(
        f"URL: https://www.jobleads.com/pl/job/x\n\n{JOBLEADS_PASTE_MARKER}\n{body_text}",
        encoding="utf-8",
    )
    from hunter import tracker as _tracker

    monkeypatch.setattr(_tracker, "manual_jobleads_job_posting_path", lambda url: p)

    out = JobLeadsSource().fetch_text("https://www.jobleads.com/pl/job/x")
    assert "Senior Angular Developer" in out


def test_jobleads_fetch_text_from_json_ld(tmp_path, monkeypatch) -> None:
    from hunter import tracker as _tracker

    monkeypatch.setattr(
        _tracker, "manual_jobleads_job_posting_path", lambda url: tmp_path / "nope.txt"
    )
    ld = {
        "@type": "JobPosting",
        "title": "Senior Angular Developer",
        "hiringOrganization": {"name": "ExampleCo"},
        "description": "<p>" + ("Build amazing things. " * 8) + "</p>",
    }
    html = f'<html><body><script type="application/ld+json">{json.dumps(ld)}</script></body></html>'
    with patch(
        "hunter.sources.jobleads._scraper.get",
        return_value=_mk_html_response(html),
    ):
        out = JobLeadsSource().fetch_text("https://www.jobleads.com/pl/job/x")
    assert "Senior Angular Developer" in out
    assert "ExampleCo" in out


def test_jobleads_fetch_text_raises_cloudflare_error_when_all_strategies_fail(
    tmp_path, monkeypatch
) -> None:
    from hunter import tracker as _tracker

    monkeypatch.setattr(
        _tracker, "manual_jobleads_job_posting_path", lambda url: tmp_path / "nope.txt"
    )

    with (
        patch(
            "hunter.sources.jobleads._scraper.get",
            return_value=_mk_html_response(
                "<html><body>just a moment...</body></html>", status=403
            ),
        ),
        patch("hunter.sources.jobleads._try_detail_playwright", return_value=""),
        patch(
            "hunter.sources.html_fallback.fetch_html",
            return_value="<html><body>just a moment</body></html>",
        ),
    ):
        with pytest.raises(JobLeadsCloudflareError):
            JobLeadsSource().fetch_text("https://www.jobleads.com/pl/job/x")
