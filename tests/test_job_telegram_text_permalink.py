"""Job.telegram_text() must surface a captured post permalink when present.

A linkedin_scout_relay job's `url` is a synthetic dedup key, not something
the owner can open. Before this fix, the manual-mode Apply/Skip card (built
from telegram_text()) showed only that fake link — the real permalink was
only ever shown once, pre-generation, in the AUTO_APPLY hunt loop's ping.
"""

from hunter.models import Job


def test_telegram_text_includes_permalink_when_present() -> None:
    job = Job(
        title="[LI post] hiring",
        company="Deloitte",
        location="",
        salary=None,
        url="https://linkedin-scout.internal/posts/pabc",
        source="linkedin_scout_relay",
        raw={"permalink": "https://www.linkedin.com/posts/someone_activity-123"},
    )
    text = job.telegram_text()
    assert "https://www.linkedin.com/posts/someone_activity-123" in text


def test_telegram_text_omits_permalink_line_when_absent() -> None:
    job = Job(
        title="Angular Dev",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://justjoin.it/job-offer/acme",
        source="justjoin",
    )
    text = job.telegram_text()
    assert "Post:" not in text
