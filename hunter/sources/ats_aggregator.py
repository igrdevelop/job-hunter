"""
ATS Aggregator — single Source that reads career pages of many companies
through their ATS provider's public JSON API.

Companies are listed in hunter/ats_companies.json. Each entry has:
  - slug: company id inside the ATS (e.g. "netguru" on Workable)
  - provider: workable | greenhouse | lever | recruitee | ashby (see hunter/ats/*.py)
  - name (optional): display name; defaults to slug.title()

Adding a new ATS provider: implement hunter/ats/<name>.py and register
it in PROVIDERS below. Adding a new company: append a JSON entry, no code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hunter.ats.ashby import AshbyProvider
from hunter.ats.base import ATSProvider
from hunter.ats.greenhouse import GreenhouseProvider
from hunter.ats.lever import LeverProvider
from hunter.ats.recruitee import RecruiteeProvider
from hunter.ats.workable import WorkableProvider
from hunter.config import ATS_COMPANIES_PATH
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, ATSProvider] = {
    "workable": WorkableProvider(),
    "greenhouse": GreenhouseProvider(),
    "lever": LeverProvider(),
    "recruitee": RecruiteeProvider(),
    "ashby": AshbyProvider(),
}


class AtsAggregatorSource(BaseSource):
    name = "ats_aggregator"

    def search(self) -> list[Job]:
        companies = load_companies(ATS_COMPANIES_PATH)
        if not companies:
            logger.info("[ats_aggregator] no companies configured")
            return []

        seen_urls: set[str] = set()
        jobs: list[Job] = []
        for company in companies:
            slug = (company.get("slug") or "").strip()
            provider_name = (company.get("provider") or "").strip().lower()
            display_name = (company.get("name") or "").strip() or None
            if not slug or not provider_name:
                logger.warning(f"[ats_aggregator] incomplete entry skipped: {company!r}")
                continue
            provider = PROVIDERS.get(provider_name)
            if provider is None:
                logger.warning(
                    f"[ats_aggregator] unknown provider '{provider_name}' for slug '{slug}' — skipped"
                )
                continue

            try:
                batch = provider.fetch(slug, display_name)
            except Exception as e:
                logger.warning(f"[ats_aggregator] {provider_name}:{slug} crashed: {e}")
                continue

            for job in batch:
                if not job.url or job.url in seen_urls:
                    continue
                if not self.matches_coarse_prefilter(job.title):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)

        logger.info(f"[ats_aggregator] {len(jobs)} jobs after pre-filter")
        return jobs


def load_companies(path: Path) -> list[dict[str, Any]]:
    """Load and validate the companies list. Returns [] on missing/broken file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.info(f"[ats_aggregator] config not found: {path}")
        return []
    except Exception as e:
        logger.warning(f"[ats_aggregator] failed to load config {path}: {e}")
        return []

    if not isinstance(data, dict):
        return []
    raw_list = data.get("companies")
    if not isinstance(raw_list, list):
        return []
    return [c for c in raw_list if isinstance(c, dict)]
