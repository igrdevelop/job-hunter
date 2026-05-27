import base64
import logging
from datetime import datetime, timedelta, timezone

from hunter.config import GMAIL_ENRICH_ENABLED
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
        logger.info("[gmail] Found %d matching email(s) in the last %dh", len(messages), LOOKBACK_HOURS)

        jobs: list[Job] = []
        for stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
            )
            found = self._parse_message(msg)
            jobs.extend(found)

        logger.info("[gmail] Extracted %d job URL(s) total across all emails", len(jobs))

        if jobs and GMAIL_ENRICH_ENABLED:
            from hunter.gmail_enricher import enrich_jobs
            jobs = enrich_jobs(jobs)
            logger.info("[gmail] After enrichment: %d job(s)", len(jobs))

        return jobs

    # Subjects that indicate confirmation/activity emails, not job alert emails.
    # Covers English (LinkedIn) and Polish (Pracuj, NoFluffJobs) platforms.
    _SKIP_SUBJECTS = (
        # English
        "you applied",
        "your application",
        "application received",
        "application was sent",
        # Polish — Pracuj.pl activity notifications
        "zapoznał się z twoją aplikacją",   # employer viewed your application
        "twoja aplikacja została wysłana",   # your application was sent
        "potwierdzenie aplikacji",           # application confirmation
        "aplikacja została przyjęta",        # application accepted
        "dziękujemy za aplikację",           # thank you for applying
        "pracodawca zaprosił cię",           # employer invited you
        "zaproszenie do rozmowy",            # invitation to interview
    )

    def _parse_message(self, msg: dict) -> list[Job]:
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "(no subject)")
        sender  = headers.get("From", "(unknown sender)")
        date    = headers.get("Date", "(no date)")

        logger.info("[gmail] ✉  from=%r  date=%s  subject=%r", sender, date, subject)

        if any(s in subject.lower() for s in self._SKIP_SUBJECTS):
            logger.info("[gmail]    → SKIP (confirmation/activity email)")
            return []

        body_text, body_html = self._extract_body(msg["payload"])

        for domain, parser_fn in PARSERS.items():
            if domain in sender:
                try:
                    found = parser_fn(subject, body_text, body_html)
                    if found:
                        logger.info(
                            "[gmail]    → %s: extracted %d URL(s):",
                            domain, len(found),
                        )
                        for job in found:
                            logger.info("[gmail]       %s", job.url)
                    else:
                        logger.info("[gmail]    → %s: 0 URLs extracted (no matching pattern in body)", domain)
                    return found
                except Exception as e:
                    logger.warning("[gmail]    → %s: parser error — %s", domain, e)
                    return []

        logger.debug("[gmail]    → no parser matched sender %r", sender)
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
