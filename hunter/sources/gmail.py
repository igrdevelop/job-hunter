import base64
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from hunter.config import (
    GMAIL_ENRICH_ENABLED,
    GMAIL_LOOKBACK_HOURS,
    GMAIL_MAX_RESULTS,
)
from hunter.gmail_client import get_gmail_service
from hunter.gmail_parsers import PARSERS
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

# Back-compat alias — historically a module constant; now sourced from config.
LOOKBACK_HOURS = GMAIL_LOOKBACK_HOURS


def _aggregator_name(domain: str) -> str:
    """'linkedin.com' → 'linkedin', 'nofluffjobs.com' → 'nofluffjobs'."""
    return domain.split(".")[0]


class GmailSource(BaseSource):
    name = "gmail"

    def __init__(self) -> None:
        # Per-scan diagnostics, read by the hunt report after search().
        # One record per email seen (incl. those that yielded 0 URLs), so the
        # report can show coverage — which emails were checked and what came out.
        self.last_email_log: list[dict] = []
        self.last_capped: bool = False  # True → hit GMAIL_MAX_RESULTS ceiling

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
        self.last_email_log = []
        self.last_capped = False

        after_ts = int(
            (datetime.now(timezone.utc) - timedelta(hours=GMAIL_LOOKBACK_HOURS)).timestamp()
        )
        sender_filter = " OR ".join(f"from:{d}" for d in PARSERS)
        query = f"({sender_filter}) after:{after_ts}"

        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=GMAIL_MAX_RESULTS)
            .execute()
        )
        messages = results.get("messages", [])
        self.last_capped = len(messages) >= GMAIL_MAX_RESULTS
        logger.info(
            "[gmail] Found %d matching email(s) in the last %dh%s",
            len(messages),
            GMAIL_LOOKBACK_HOURS,
            " (CEILING hit — emails may be truncated)" if self.last_capped else "",
        )

        jobs: list[Job] = []
        for stub in messages:
            msg = (
                service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
            )
            found = self._parse_message(msg)
            jobs.extend(found)

        logger.info("[gmail] Extracted %d job URL(s) total across all emails", len(jobs))

        if jobs and GMAIL_ENRICH_ENABLED:
            from hunter.gmail_enricher import enrich_jobs

            jobs = enrich_jobs(jobs)
            logger.info("[gmail] After enrichment: %d job(s)", len(jobs))

        return jobs

    @staticmethod
    def _parse_date(raw: str):
        """RFC-2822 'Date' header → datetime, or None if unparseable."""
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None

    # Subjects that indicate confirmation/activity emails, not job alert emails.
    # Covers English (LinkedIn) and Polish (Pracuj, NoFluffJobs) platforms.
    _SKIP_SUBJECTS = (
        # English
        "you applied",
        "your application",
        "application received",
        "application was sent",
        # Polish — Pracuj.pl activity notifications
        "zapoznał się z twoją aplikacją",  # employer viewed your application
        "twoja aplikacja została wysłana",  # your application was sent
        "potwierdzenie aplikacji",  # application confirmation
        "aplikacja została przyjęta",  # application accepted
        "dziękujemy za aplikację",  # thank you for applying
        "pracodawca zaprosił cię",  # employer invited you
        "zaproszenie do rozmowy",  # invitation to interview
    )

    def _parse_message(self, msg: dict) -> list[Job]:
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "(unknown sender)")
        date = headers.get("Date", "(no date)")

        msg_id = msg.get("id", "")
        parsed_date = self._parse_date(date)

        # One log record per email, regardless of outcome. Updated below as we learn
        # the aggregator and how many URLs were extracted.
        record = {
            "msg_id": msg_id,
            "date": parsed_date,
            "subject": subject,
            "sender": sender,
            "aggregator": "",
            "extracted": 0,
            "skipped": False,
        }
        self.last_email_log.append(record)

        logger.info("[gmail] ✉  from=%r  date=%s  subject=%r", sender, date, subject)

        body_text, body_html = self._extract_body(msg["payload"])
        is_ack_subject = any(s in subject.lower() for s in self._SKIP_SUBJECTS)

        # Try the parser FIRST, even on ACK-looking subjects: NoFluffJobs (and
        # occasionally Pracuj) bundle a "similar job offers especially for you"
        # block into the application-confirmation email. Skipping on subject
        # alone threw those 10 recommendations away. Only fall back to SKIP if
        # the parser found 0 URLs in an ACK-shaped email.
        for domain, parser_fn in PARSERS.items():
            if domain in sender:
                aggregator = _aggregator_name(domain)
                record["aggregator"] = aggregator
                try:
                    found = parser_fn(subject, body_text, body_html)
                except Exception as e:
                    logger.warning("[gmail]    → %s: parser error — %s", domain, e)
                    return []
                record["extracted"] = len(found)
                # Stamp provenance so the hunt report can group by email.
                meta = {
                    "msg_id": msg_id,
                    "date": parsed_date,
                    "subject": subject,
                    "sender": sender,
                    "aggregator": aggregator,
                }
                for job in found:
                    job.email_meta = meta
                if found:
                    logger.info(
                        "[gmail]    → %s: extracted %d URL(s)%s:",
                        domain,
                        len(found),
                        " (similar offers in ACK email)" if is_ack_subject else "",
                    )
                    for job in found:
                        logger.info("[gmail]       %s", job.url)
                    return found
                if is_ack_subject:
                    logger.info(
                        "[gmail]    → SKIP (confirmation/activity email, no similar offers)"
                    )
                    record["skipped"] = True
                    return []
                logger.info(
                    "[gmail]    → %s: 0 URLs extracted (no matching pattern in body)", domain
                )
                return []

        # No parser matched the sender. Honor SKIP for ACK-shaped subjects so
        # the report still groups them under "подтверждений пропущено" rather
        # than the noisier "парсер не распознал".
        if is_ack_subject:
            logger.info("[gmail]    → SKIP (confirmation/activity email, unknown sender)")
            record["skipped"] = True
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
