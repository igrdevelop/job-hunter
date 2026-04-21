"""High-level tracker operations used by apply/hunt flows."""

import logging

from hunter.tracker import add_applied, get_url_status_flags

logger = logging.getLogger(__name__)


def should_skip_url(url: str) -> bool:
    """Return True when URL is already successfully processed or react-skipped."""
    flags = get_url_status_flags(url)
    return flags["has_success"] or flags["is_react_skip"]


def record_successful_apply(content: dict, force: bool = False) -> bool:
    """Record successful generated docs in tracker, then rebuild to_send.xlsx."""
    written = add_applied(content, force=force)
    if written:
        try:
            from hunter import to_send  # local import avoids circular deps
            to_send.sync_and_rebuild()
        except Exception as exc:
            logger.warning("[tracker_service] to_send sync failed: %s", exc)
    return written

