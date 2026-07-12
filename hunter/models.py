from dataclasses import dataclass, field
from typing import Optional
import hashlib


@dataclass
class Job:
    title: str
    company: str
    location: str          # "Wrocław (Hybrid)" / "Remote" / "Wrocław (On-site)"
    salary: Optional[str]  # "15 000–20 000 PLN B2B" — free-form text, each site differs
    url: str               # canonical URL — used as unique key for dedup
    source: str            # "justjoin" | "linkedin" | "nofluffjobs" | "pracuj"
    raw: dict = field(default_factory=dict, repr=False)  # original API response for debugging
    # Gmail provenance (gmail_* sources only): which alert email this URL came from.
    # Keys: msg_id, date (datetime|None), subject, sender, aggregator. Empty for
    # all non-gmail sources. Lets the hunt report group vacancies per email.
    email_meta: dict = field(default_factory=dict, repr=False)

    def job_id(self) -> str:
        """Short hash of URL — used as callback_data key in Telegram (max 64 bytes)."""
        return hashlib.md5(self.url.encode()).hexdigest()[:10]

    def telegram_text(self) -> str:
        """One-card message text for Telegram notification."""
        lines = [
            f"<b>{self.title}</b> — {self.company}",
            f"📍 {self.location}",
        ]
        if self.salary:
            lines.append(f"💰 {self.salary}")
        lines.append(f"🔗 {self.url}")
        # A synthetic dedup-key url (e.g. linkedin_scout_relay) isn't openable
        # — surface the real, clickable post permalink when one was captured,
        # so a manual-mode Apply card actually has a link the owner can use.
        permalink = (self.raw or {}).get("permalink")
        if permalink:
            lines.append(f"🔗 Post: {permalink}")
        lines.append(f"<i>Source: {self.source}</i>")
        return "\n".join(lines)
