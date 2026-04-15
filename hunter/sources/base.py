"""
BaseSource — abstract interface every job-site scraper must implement.

To add a new source:
  1. Create hunter/sources/yoursite.py
  2. Subclass BaseSource, implement search()
  3. Register in hunter/sources/__init__.py → ALL_SOURCES
"""

import re
from abc import ABC, abstractmethod

from hunter.config import FILTER
from hunter.models import Job


class BaseSource(ABC):
    name: str = "base"  # override in subclass

    @abstractmethod
    def search(self) -> list[Job]:
        """
        Fetch vacancies from the source.
        Must return a list of Job objects — no filtering, no dedup (done centrally).
        Should handle its own network errors and return [] on failure.
        """
        ...

    def __repr__(self) -> str:
        return f"<Source: {self.name}>"

    def matches_coarse_prefilter(self, title: str, context_text: str = "") -> bool:
        """Fast source-level prefilter shared by source implementations.

        This does only coarse keyword/exclude checks to reduce noise early.
        Full filtering still happens centrally in hunter.filters.apply_filters().
        """
        t = (title or "").lower()
        c = (context_text or "").lower()

        for pat in FILTER.get("exclude_patterns", []):
            if re.search(pat, t, re.I):
                return False

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        combined = f"{t} {c}".strip()
        return any(kw in combined for kw in keywords)
