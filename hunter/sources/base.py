"""
BaseSource — abstract interface every job-site scraper must implement.

To add a new source:
  1. Create hunter/sources/yoursite.py
  2. Subclass BaseSource, implement search()
  3. Register in hunter/sources/__init__.py → ALL_SOURCES
"""

from abc import ABC, abstractmethod
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
