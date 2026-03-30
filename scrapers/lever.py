"""
CCC Backend — Lever ATS Scraper

Lever exposes a free public JSON feed per company:
  GET https://api.lever.co/v0/postings/{board_token}?mode=json

No auth required.
"""
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError
from scrapers.greenhouse import _infer_region, _infer_employment_type

logger = logging.getLogger(__name__)

BASE_URL = "https://api.lever.co/v0/postings/{token}?mode=json"

DEFAULT_TARGETS = [
    # Finance / Quant
    ("Point72",              "point72"),
    ("D.E. Shaw",            "deshaw"),
    ("Millennium",           "millennium"),
    ("Schonfeld",            "schonfeld"),
    # Technology
    ("Canva",                "canva"),
    ("Rippling",             "rippling"),
    ("Carta",                "carta"),
    ("Front",                "front"),
    ("Loom",                 "loom"),
    ("Mercury",              "mercury"),
    ("Linear",               "linear"),
    ("Retool",               "retool"),
    ("Amplitude",            "amplitude"),
    ("Mixpanel",             "mixpanel"),
    ("Segment",              "segment"),
    ("Heap",                 "heap"),
    # Consulting / Strategy
    ("Bain & Company",       "bain"),
    ("Booz Allen Hamilton",  "boozallen"),
    # Growth / Marketing
    ("Attentive",            "attentive"),
    ("Iterable",             "iterable"),
    # Design / UX
    ("Abstract",             "abstract"),
    # VC / PE
    ("General Catalyst",     "generalcatalyst"),
    ("Bessemer",             "bvp"),
    # Healthcare
    ("Oscar Health",         "oscar"),
    ("Cityblock Health",     "cityblock"),
    # Non-profit / Policy
    ("Code for America",     "codeforamerica"),
    ("Chan Zuckerberg",      "chanzuckerberg"),
    # Real Estate
    ("Opendoor",             "opendoor"),
    ("Compass",              "compass"),
]


class LeverScraper(BaseScraper):
    source_id   = "lever_feed"
    source_name = "Lever ATS"
    tier        = 1

    def __init__(self, extra_targets: list[tuple[str, str]] | None = None):
        super().__init__()
        self.targets = list(DEFAULT_TARGETS)
        if extra_targets:
            self.targets.extend(extra_targets)

    def scrape(self) -> Iterator[dict]:
        for company_name, token in self.targets:
            url = BASE_URL.format(token=token)
            try:
                data = self.fetch_json(url)
            except RequestError as e:
                self.logger.warning(f"Lever [{company_name}] fetch failed: {e}")
                continue
            except Exception as e:
                self.logger.warning(f"Lever [{company_name}] unexpected error: {e}")
                continue

            postings = data if isinstance(data, list) else []
            self.logger.info(f"Lever [{company_name}]: {len(postings)} listings")

            for raw in postings:
                try:
                    job = self._parse_posting(raw, company_name)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"Lever parse error [{company_name}]: {e}")
                    continue

    def _parse_posting(self, raw: dict, company_name: str) -> dict | None:
        title = raw.get("text", "").strip()
        if not title:
            return None

        url = raw.get("hostedUrl", raw.get("applyUrl", ""))

        # Location
        location_obj = raw.get("categories", {})
        location     = location_obj.get("location", "") if isinstance(location_obj, dict) else ""
        if not location:
            locs = raw.get("workplaceType", "")
            location = str(locs)

        region = _infer_region(location)

        # Posted at (millisecond epoch)
        created_ms = raw.get("createdAt", 0)
        if created_ms:
            from datetime import datetime
            posted_date = datetime.utcfromtimestamp(created_ms / 1000).strftime("%Y-%m-%d")
        else:
            posted_date = today_iso()

        # Description
        desc_obj = raw.get("description", "")
        description = desc_obj if isinstance(desc_obj, str) else ""
        lists = raw.get("lists", [])
        for lst in lists:
            if isinstance(lst, dict):
                description += " " + lst.get("content", "")

        industries = infer_industries(title, description)
        seniority  = infer_seniority(title)

        return make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries,
            seniority       = seniority,
            employment_type = _infer_employment_type(title),
            region          = region,
            posted_date     = posted_date,
        )
