"""
ATSProvider — interface every ATS-system adapter implements.

An ATS (Workable, Greenhouse, Lever, Recruitee, Ashby) hosts career pages
for many companies. Each provider has a single public JSON API shape;
one adapter therefore handles all companies on that ATS.

To add a new provider:
  1. Create hunter/ats/<provider>.py
  2. Subclass ATSProvider, implement fetch(slug, company_name)
  3. Register in hunter/sources/ats_aggregator.py PROVIDERS dict
"""

from abc import ABC, abstractmethod
from typing import Optional

from hunter.models import Job


class ATSProvider(ABC):
    name: str = "base"  # override in subclass: "workable" | "greenhouse" | ...

    @abstractmethod
    def fetch(self, slug: str, company_name: Optional[str] = None) -> list[Job]:
        """Fetch published jobs for a single company on this ATS.

        Args:
            slug: company identifier inside this ATS (e.g. "netguru" on Workable).
            company_name: optional human-readable name; falls back to slug if None.

        Must return [] on failure (network, 404, malformed JSON) — never raise.
        Filtering is done centrally by hunter.filters.apply_filters().
        """
        ...

    def __repr__(self) -> str:
        return f"<ATSProvider: {self.name}>"
