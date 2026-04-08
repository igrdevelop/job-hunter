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
        lines.append(f"<i>Source: {self.source}</i>")
        return "\n".join(lines)
