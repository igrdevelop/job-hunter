"""Lever detail-page fetch: path parsing + posting-dict → text rendering, and
the deleted-posting → EXPIRED-marker contract (no network — the HTTP call is
monkeypatched).

Guards the 2026-07-12 fix where a deleted jobs.lever.co posting (HTTP 404 on the
public page) landed a FAIL tracker row: fetch_html raises on the 404 page before
the pipeline's expiry check runs, so the Lever posting API is queried instead and
a deleted posting returns a synthetic marker that hunter.expired_check recognises.
"""

from hunter.expired_check import is_job_expired
from hunter.sources import ats_aggregator
from hunter.sources.ats_aggregator import (
    _LEVER_EXPIRED_TEXT,
    _fetch_lever_text,
    _lever_dict_to_text,
    _parse_lever_path,
)


# ── path parsing ─────────────────────────────────────────────────────────────


def test_parse_lever_path_site_and_id() -> None:
    assert _parse_lever_path("/jobgether/9c52de44-7afa-40ab-8c31-6b30cd0d0fea") == (
        "jobgether",
        "9c52de44-7afa-40ab-8c31-6b30cd0d0fea",
    )


def test_parse_lever_path_with_apply_suffix() -> None:
    assert _parse_lever_path("/acme/abc123/apply") == ("acme", "abc123")


def test_parse_lever_path_incomplete() -> None:
    assert _parse_lever_path("/jobgether") == (None, None)
    assert _parse_lever_path("") == (None, None)


# ── dict → text ──────────────────────────────────────────────────────────────


def test_lever_dict_to_text_combines_sections() -> None:
    data = {
        "text": "Senior Angular Developer",
        "country": "BR",
        "workplaceType": "remote",
        "categories": {"location": "Brazil", "commitment": "Full-time"},
        "descriptionPlain": "We are hiring an Angular engineer.",
        "lists": [
            {"text": "Accountabilities", "content": "<ul><li>Build features</li></ul>"},
            {"text": "Requirements", "content": "<ul><li>5+ years Angular</li></ul>"},
        ],
        "additionalPlain": "How Jobgether works: AI matching.",
    }
    text = _lever_dict_to_text(data)
    assert "Senior Angular Developer" in text
    assert "Brazil" in text and "remote" in text
    assert "Full-time" in text
    assert "We are hiring an Angular engineer." in text
    assert "Accountabilities" in text and "Build features" in text
    assert "5+ years Angular" in text
    assert "How Jobgether works" in text


def test_lever_dict_to_text_falls_back_to_html_description() -> None:
    data = {"text": "Dev", "description": "<p>Do <strong>things</strong> here.</p>"}
    text = _lever_dict_to_text(data)
    assert "things" in text


# ── fetch (network monkeypatched) ────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_fetch_lever_deleted_404_returns_expired_marker(monkeypatch) -> None:
    monkeypatch.setattr(
        ats_aggregator.requests,
        "get",
        lambda *a, **k: _Resp(404, {"ok": False, "error": "Document not found"}),
    )
    text = _fetch_lever_text("https://jobs.lever.co/jobgether/deadbeef")
    assert text == _LEVER_EXPIRED_TEXT
    assert is_job_expired(text) is True


def test_fetch_lever_ok_false_body_returns_expired_marker(monkeypatch) -> None:
    # Some Lever errors come back HTTP 200 with an {"ok": false} body.
    monkeypatch.setattr(
        ats_aggregator.requests,
        "get",
        lambda *a, **k: _Resp(200, {"ok": False, "error": "Document not found"}),
    )
    text = _fetch_lever_text("https://jobs.lever.co/jobgether/deadbeef")
    assert text == _LEVER_EXPIRED_TEXT
    assert is_job_expired(text) is True


def test_fetch_lever_live_posting_returns_rendered_text(monkeypatch) -> None:
    payload = {
        "text": "Senior Angular Developer",
        "country": "BR",
        "workplaceType": "remote",
        "categories": {"location": "Brazil", "commitment": "Full-time"},
        "descriptionPlain": "We are hiring an Angular engineer to build our product. " * 4,
        "lists": [{"text": "Requirements", "content": "<ul><li>5+ years Angular</li></ul>"}],
    }
    monkeypatch.setattr(ats_aggregator.requests, "get", lambda *a, **k: _Resp(200, payload))
    text = _fetch_lever_text("https://jobs.lever.co/jobgether/abc123")
    assert "Senior Angular Developer" in text
    assert is_job_expired(text) is False


def test_fetch_lever_bad_path_uses_html_fallback(monkeypatch) -> None:
    called: dict[str, str] = {}

    def _fake_fetch_html(url: str) -> str:
        called["url"] = url
        return "fallback text"

    monkeypatch.setattr("hunter.sources.html_fallback.fetch_html", _fake_fetch_html, raising=True)
    # No posting id in the path → never touches the API, straight to fallback.
    out = _fetch_lever_text("https://jobs.lever.co/jobgether")
    assert out == "fallback text"
    assert called["url"] == "https://jobs.lever.co/jobgether"


def test_fetch_lever_network_error_uses_html_fallback(monkeypatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(ats_aggregator.requests, "get", _boom)
    monkeypatch.setattr(
        "hunter.sources.html_fallback.fetch_html",
        lambda url: "fallback text",
        raising=True,
    )
    assert _fetch_lever_text("https://jobs.lever.co/jobgether/abc123") == "fallback text"


def test_lever_routed_through_api_by_fetch_text(monkeypatch) -> None:
    """jobs.lever.co URLs dispatch to _fetch_lever_text, not html_fallback."""
    monkeypatch.setattr(
        ats_aggregator.requests,
        "get",
        lambda *a, **k: _Resp(404, {"ok": False, "error": "Document not found"}),
    )
    src = ats_aggregator.AtsAggregatorSource()
    assert src.fetch_text("https://jobs.lever.co/jobgether/deadbeef") == _LEVER_EXPIRED_TEXT
