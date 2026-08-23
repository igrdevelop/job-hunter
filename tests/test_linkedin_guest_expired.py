"""LinkedIn closed-posting detection on logged-out (guest) HTML.

Measured 2026-08-22 against the live site: a guest page NEVER contains the
"No longer accepting applications" banner (14 live + 1 closed posting, zero
hits on either), so the only signal available without a session is the apply
CTA — present on all 14 live pages, absent on the closed one.

The markup below mirrors the real pages (jobs/view/4455428397 closed vs
jobs/view/4456213260 live), trimmed to the part the check reads.
"""

from unittest.mock import Mock, patch

import pytest

from hunter.expired_check import is_job_expired
from hunter.sources.linkedin import LinkedInSource, guest_html_expired

_DESCRIPTION = (
    "<section class='description'><div class='show-more-less-html__markup'>"
    "We are looking for a Senior Frontend Developer to lead the frontend "
    "architecture of our Analytics Portal. Strong hands-on Angular expertise "
    "is essential, together with TypeScript, RxJS and modern tooling. "
    "You will work closely with Product Owners, Designers and Architects."
    "</div></section>"
)

CLOSED_HTML = (
    "<html><body><section class='top-card-layout'>"
    "<h1 class='top-card-layout__title'>Frontend Developer</h1>"
    "<div class='top-card-layout__cta-container flex flex-wrap mt-0.5'>"
    "<!----> <!---->"
    "</div>"
    "</section>" + _DESCRIPTION + "</body></html>"
)

LIVE_HTML = (
    "<html><body><section class='top-card-layout'>"
    "<h1 class='top-card-layout__title'>Frontend Developer</h1>"
    "<div class='top-card-layout__cta-container flex flex-wrap mt-0.5'>"
    "<button class='sign-up-modal__outlet top-card-layout__cta btn-primary' "
    "data-modal='job-details-topcard-apply-modal'>Apply</button>"
    "</div>"
    "</section>" + _DESCRIPTION + "</body></html>"
)

# Some live pages render the CTA as an offsite <a> instead of a modal button.
LIVE_HTML_ANCHOR = LIVE_HTML.replace(
    "<button class='sign-up-modal__outlet top-card-layout__cta btn-primary' "
    "data-modal='job-details-topcard-apply-modal'>Apply</button>",
    "<a href='https://example.com/apply' class='top-card-layout__cta'>Apply</a>",
)

# What a renamed/removed container would look like — must NOT read as expired.
NO_CONTAINER_HTML = (
    "<html><body><section class='top-card-layout'>"
    "<h1 class='top-card-layout__title'>Frontend Developer</h1>"
    "<div class='top-card-layout__brand-new-cta-name'><!----></div>"
    "</section>" + _DESCRIPTION + "</body></html>"
)


class TestGuestHtmlExpired:
    def test_empty_cta_container_is_expired(self) -> None:
        assert guest_html_expired(CLOSED_HTML) is True

    @pytest.mark.parametrize("html", [LIVE_HTML, LIVE_HTML_ANCHOR])
    def test_cta_with_apply_control_is_live(self, html: str) -> None:
        assert guest_html_expired(html) is False

    def test_missing_container_is_not_expired(self) -> None:
        """Markup change must go silent, never expire every LinkedIn posting."""
        assert guest_html_expired(NO_CONTAINER_HTML) is False

    def test_empty_html(self) -> None:
        assert guest_html_expired("") is False


class TestFetchText:
    def _response(self, html: str) -> Mock:
        resp = Mock()
        resp.text = html
        resp.raise_for_status = Mock()
        return resp

    def test_closed_posting_returns_expired_marker(self) -> None:
        """The marker must be one is_job_expired recognizes — that is the whole
        point: apply Step 1.5a then skips for $0 instead of generating a CV."""
        with patch(
            "hunter.sources.linkedin.requests.get", return_value=self._response(CLOSED_HTML)
        ):
            text = LinkedInSource().fetch_text("https://www.linkedin.com/jobs/view/4455428397/")

        assert is_job_expired(text) is True

    def test_live_posting_returns_description_text(self) -> None:
        with patch("hunter.sources.linkedin.requests.get", return_value=self._response(LIVE_HTML)):
            text = LinkedInSource().fetch_text("https://www.linkedin.com/jobs/view/4456213260/")

        assert "Senior Frontend Developer" in text
        assert is_job_expired(text) is False


class TestExpiredMarkerWiring:
    """/check_expired used to report every LinkedIn row as skipped."""

    def test_closed_linkedin_html_marks_expired(self) -> None:
        from hunter.expired_marker import _check_html_expired

        assert _check_html_expired(CLOSED_HTML, "www.linkedin.com", url="x") is True

    def test_live_linkedin_html_is_inconclusive(self) -> None:
        from hunter.expired_marker import _check_html_expired

        assert _check_html_expired(LIVE_HTML, "www.linkedin.com", url="x") is None
