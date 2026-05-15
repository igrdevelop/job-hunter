import base64
import logging
from datetime import datetime, timedelta, timezone

from hunter.gmail_client import get_gmail_service
from hunter.gmail_parsers import PARSERS
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 25  # slightly over one day — bridges the gap between scheduled runs


class GmailSource(BaseSource):
    name = "gmail"

    def search(self) -> list[Job]:
        try:
            service = get_gmail_service()
            return self._fetch_jobs(service)
        except FileNotFoundError as e:
            logger.error(f"[gmail] Token missing: {e}")
            return []
        except Exception as e:
            logger.error(f"[gmail] Error: {e}")
            return []

    def _fetch_jobs(self, service) -> list[Job]:
        after_ts = int(
            (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).timestamp()
        )
        sender_filter = " OR ".join(f"from:{d}" for d in PARSERS)
        query = f"({sender_filter}) after:{after_ts}"

        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100)
            .execute()
        )
        messages = results.get("messages", [])
        logger.info(f"[gmail] Found {len(messages)} matching emails")

        jobs: list[Job] = []
        for stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
            )
            jobs.extend(self._parse_message(msg))

        logger.info(f"[gmail] Extracted {len(jobs)} job URLs total")
        return jobs

    # Subjects that indicate confirmation/activity emails, not job alert emails.
    _SKIP_SUBJECTS = (
        "you applied",
        "your application",
        "application received",
        "application was sent",
    )

    def _parse_message(self, msg: dict) -> list[Job]:
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")

        if any(s in subject.lower() for s in self._SKIP_SUBJECTS):
            logger.debug(f"[gmail] skipping confirmation email: '{subject}'")
            return []

        body_text, body_html = self._extract_body(msg["payload"])

        for domain, parser_fn in PARSERS.items():
            if domain in sender:
                try:
                    found = parser_fn(subject, body_text, body_html)
                    logger.debug(f"[gmail] {domain}: {len(found)} jobs from '{subject}'")
                    return found
                except Exception as e:
                    logger.warning(f"[gmail] Parser error ({domain}): {e}")
                    return []
        return []

    def _extract_body(self, payload: dict) -> tuple[str, str]:
        """Recursively extract text/plain and text/html from MIME tree."""
        body_text = ""
        body_html = ""

        if "parts" in payload:
            for part in payload["parts"]:
                t, h = self._extract_body(part)
                body_text = body_text or t
                body_html = body_html or h
        else:
            mime = payload.get("mimeType", "")
            data = payload.get("body", {}).get("data", "")
            if data:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                if mime == "text/plain":
                    body_text = decoded
                elif mime == "text/html":
                    body_html = decoded

        return body_text, body_html
