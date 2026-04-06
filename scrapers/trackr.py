"""
Trackr Scraper  (Gold Standard source #1)

Public JSON API — no auth required.
  GET https://api.the-trackr.com/programmes
    ?region=UK|NA|EU
    &industry=Finance|Technology|Law
    &season=2026|2027
    &type=summer-internships|training-contracts|off-cycle-internships

Returns structured programme records with company, URL, open/close dates, etc.
"""
import os
import json
import logging
from datetime import date
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority, infer_industries,
    clean_date, today_iso, RequestError,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.the-trackr.com/programmes"

# (region, industry, season, type) — only buckets with known data
QUERY_BUCKETS = [
    # UK
    ("UK", "Finance",    "2026", "summer-internships"),
    ("UK", "Finance",    "2026", "off-cycle-internships"),
    ("UK", "Technology", "2026", "summer-internships"),
    ("UK", "Technology", "2026", "off-cycle-internships"),
    ("UK", "Law",        "2026", "training-contracts"),
    # North America (2027 cycle already live)
    ("NA", "Finance",    "2026", "summer-internships"),
    ("NA", "Finance",    "2027", "summer-internships"),
    ("NA", "Technology", "2026", "summer-internships"),
    ("NA", "Technology", "2027", "summer-internships"),
    # Europe
    ("EU", "Finance",    "2026", "summer-internships"),
    ("EU", "Finance",    "2027", "summer-internships"),
]

# Map Trackr region codes → inroad region strings
REGION_MAP = {"UK": "UK", "NA": "US", "EU": "EU"}

# Map Trackr industry → inroad industry list
INDUSTRY_MAP = {
    "Finance":    ["Finance", "Investment Banking"],
    "Technology": ["Technology", "Software Engineering", "Data & Analytics"],
    "Law":        ["Law"],
}

# Map Trackr type → employment_type string
TYPE_MAP = {
    "summer-internships":    "internship",
    "off-cycle-internships": "internship",
    "training-contracts":    "full-time",
    "industrial-placements": "internship",
}


class TrackrScraper(BaseScraper):
    source_id   = "trackr"
    source_name = "Trackr"
    tier        = 1  # Gold Standard

    def scrape(self) -> Iterator[dict]:
        seen: set = set()
        today = date.today().isoformat()

        for region, industry, season, prog_type in QUERY_BUCKETS:
            url = (
                f"{API_BASE}"
                f"?region={region}&industry={industry}&season={season}&type={prog_type}"
            )
            try:
                data = self.fetch_json(url, headers={
                    "Accept": "application/json",
                    "Referer": "https://app.the-trackr.com/",
                    "Origin": "https://app.the-trackr.com",
                })
            except RequestError as e:
                self.logger.warning(f"Trackr fetch failed [{region}/{industry}/{season}/{prog_type}]: {e}")
                continue
            except Exception as e:
                self.logger.error(f"Trackr unexpected error [{region}/{industry}/{season}/{prog_type}]: {e}", exc_info=True)
                continue

            if not isinstance(data, list):
                self.logger.warning(f"Trackr: unexpected response type for {url}: {type(data)}")
                continue

            self.logger.info(f"Trackr [{region}/{industry}/{season}/{prog_type}]: {len(data)} programmes")

            count = 0
            for raw in data:
                try:
                    prog_id = raw.get("id", "")
                    if prog_id in seen:
                        continue
                    seen.add(prog_id)

                    # Skip programmes that have already closed
                    closing_raw = raw.get("closingDate") or ""
                    closing_date = clean_date(closing_raw)
                    if closing_date and closing_date < today:
                        continue

                    job = self._parse_programme(raw, region, industry, prog_type)
                    if job:
                        yield job
                        count += 1
                except Exception as e:
                    self.logger.debug(f"Trackr parse error: {e}", exc_info=True)

            self.logger.info(f"Trackr [{region}/{industry}/{season}/{prog_type}]: yielded {count} open programmes")

    def _parse_programme(self, raw: dict, region: str, industry: str, prog_type: str) -> dict | None:
        title = (raw.get("name") or "").strip()
        if not title:
            return None

        company_obj  = raw.get("company") or {}
        company_name = (company_obj.get("name") or raw.get("companyId") or "").strip()
        if not company_name:
            return None

        url           = (raw.get("url") or "").strip()
        opening_raw   = raw.get("openingDate") or ""
        closing_raw   = raw.get("closingDate") or ""

        # Use today as posted_date so jobs remain fresh in the active_jobs window
        posted_date   = today_iso()
        opening_date  = clean_date(opening_raw)
        closing_date  = clean_date(closing_raw)

        # Location string from locations list
        locations = raw.get("locations") or []
        location_str = ", ".join(locations) if locations else ""

        industries   = INDUSTRY_MAP.get(industry, [industry])
        out_region   = REGION_MAP.get(region, region)
        emp_type     = TYPE_MAP.get(prog_type, "internship")

        job = make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries,
            seniority       = infer_seniority(title),
            employment_type = emp_type,
            region          = out_region,
            posted_date     = posted_date,
            closing_date    = closing_date,
        )
        job["opening_date"] = opening_date
        return job
