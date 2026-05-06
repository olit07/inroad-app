"""
Trackr Scraper  (Gold Standard source #1)

Public JSON API — no auth required.
  GET https://api.the-trackr.com/programmes
    ?region=UK|NA|EU
    &industry=Finance|Technology|Law
    &season=2026|2027
    &type=summer-internships|spring-weeks|...

UK Finance has a dedicated scraper per programme type so each page can be
run, tested and monitored independently:

  TrackrSummerInternshipsScraper   → summer-internships
  TrackrSpringWeeksScraper         → spring-weeks
  TrackrOffCycleScraper            → off-cycle-internships
  TrackrIndustrialPlacementsScraper→ industrial-placements
  TrackrGradProgrammesScraper      → graduate-programmes
  TrackrEventsScraper              → events

TrackrScraper handles remaining buckets (pre-uni, Technology, Law, NA, EU).
"""
import logging
from datetime import date
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority,
    clean_date, RequestError,
)
from config.settings import COMPANY_SIZE_LOOKUP

logger = logging.getLogger(__name__)

API_BASE = "https://api.the-trackr.com/programmes"

REGION_MAP = {"UK": "UK", "NA": "US", "EU": "EU"}

INDUSTRY_MAP = {
    "Finance":    ["Finance", "Investment Banking"],
    "Technology": ["Technology", "Software Engineering", "Data & Analytics"],
    "Law":        ["Law"],
}

TYPE_MAP = {
    "summer-internships":    "internship",
    "spring-weeks":          "internship",
    "off-cycle-internships": "internship",
    "industrial-placements": "internship",
    "graduate-programmes":   "full-time",
    "training-contracts":    "full-time",
    "pre-uni":               "internship",
    "events":                "event",
}

TRACKR_TYPE_LABEL = {
    "summer-internships":    "Summer Internship",
    "spring-weeks":          "Spring Week",
    "off-cycle-internships": "Off-Cycle Internship",
    "industrial-placements": "Industrial Placement",
    "graduate-programmes":   "Graduate Programme",
    "training-contracts":    "Graduate Programme",
    "pre-uni":               "Pre-Uni",
    "events":                "Events",
}

_ACTIVE_SEASONS = ["2026", "2027"]


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_programme(raw: dict, region: str, industry: str, prog_type: str) -> dict | None:
    title = (raw.get("name") or "").strip()
    if not title:
        return None

    company_obj  = raw.get("company") or {}
    company_name = (company_obj.get("name") or raw.get("companyId") or "").strip()
    if not company_name:
        return None

    url          = (raw.get("url") or "").strip()
    opening_date = clean_date(raw.get("openingDate") or "")
    closing_date = clean_date(raw.get("closingDate") or "")
    locations    = raw.get("locations") or []

    trackr_id = (raw.get("id") or "").strip()

    job = make_job(
        company_name    = company_name,
        title           = title,
        source_id       = "trackr",
        source_name     = "Trackr",
        url             = url,
        industries      = INDUSTRY_MAP.get(industry, [industry]),
        seniority       = infer_seniority(title),
        employment_type = TYPE_MAP.get(prog_type, "internship"),
        region          = REGION_MAP.get(region, region),
        posted_date     = opening_date or None,
        closing_date    = closing_date,
    )
    job["opening_date"]      = opening_date
    job["company_size"]      = COMPANY_SIZE_LOOKUP.get(company_name.lower().strip(), "")
    job["location"]          = ", ".join(locations) if locations else ""
    job["trackr_type"]       = prog_type
    job["source_identifier"] = trackr_id
    categories = raw.get("categories") or []
    job["trackr_categories"] = categories  # stored in raw JSON; used to derive vertical
    return job


def _scrape_bucket(
    scraper: "BaseScraper",
    region: str,
    industry: str,
    prog_type: str,
    seasons: list[str] = _ACTIVE_SEASONS,
) -> Iterator[dict]:
    """Yield open jobs for one (region, industry, prog_type) across all seasons."""
    seen: set = set()
    today = date.today().isoformat()

    for season in seasons:
        url = f"{API_BASE}?region={region}&industry={industry}&season={season}&type={prog_type}"
        try:
            data = scraper.fetch_json(url, headers={
                "Accept":  "application/json",
                "Referer": "https://app.the-trackr.com/",
                "Origin":  "https://app.the-trackr.com",
            })
        except RequestError as e:
            scraper.logger.warning(f"Trackr fetch failed [{region}/{industry}/{season}/{prog_type}]: {e}")
            continue
        except Exception as e:
            scraper.logger.error(f"Trackr error [{region}/{industry}/{season}/{prog_type}]: {e}", exc_info=True)
            continue

        if not isinstance(data, list):
            scraper.logger.warning(f"Trackr: unexpected response for {url}: {type(data)}")
            continue

        scraper.logger.info(f"Trackr [{region}/{industry}/{season}/{prog_type}]: {len(data)} records")
        count = 0
        for raw in data:
            try:
                prog_id = raw.get("id") or ""
                if prog_id in seen:
                    continue
                seen.add(prog_id)

                closing_date = clean_date(raw.get("closingDate") or "")
                if closing_date and closing_date < today:
                    continue

                job = _parse_programme(raw, region, industry, prog_type)
                if job:
                    yield job
                    count += 1
            except Exception as e:
                scraper.logger.debug(f"Trackr parse error: {e}", exc_info=True)

        scraper.logger.info(f"Trackr [{region}/{industry}/{season}/{prog_type}]: yielded {count} open")


# ── Dedicated UK Finance scrapers (one per page) ──────────────────────────────

class _TrackrUKFinanceBase(BaseScraper):
    source_name = "Trackr"
    tier        = 1
    PROG_TYPE: str  # set by each subclass

    def scrape(self) -> Iterator[dict]:
        yield from _scrape_bucket(self, "UK", "Finance", self.PROG_TYPE)


class TrackrSummerInternshipsScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/summer-internships"""
    source_id = "trackr_summer_internships"
    PROG_TYPE = "summer-internships"


class TrackrSpringWeeksScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/spring-weeks"""
    source_id = "trackr_spring_weeks"
    PROG_TYPE = "spring-weeks"


class TrackrOffCycleScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/off-cycle-internships"""
    source_id = "trackr_off_cycle"
    PROG_TYPE = "off-cycle-internships"


class TrackrIndustrialPlacementsScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/industrial-placements"""
    source_id = "trackr_industrial_placements"
    PROG_TYPE = "industrial-placements"


class TrackrGradProgrammesScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/graduate-programmes"""
    source_id = "trackr_grad_programmes"
    PROG_TYPE = "graduate-programmes"


class TrackrEventsScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/events"""
    source_id = "trackr_events"
    PROG_TYPE = "events"


# ── General scraper (non-UK-Finance buckets) ──────────────────────────────────

_OTHER_BUCKETS = [
    # UK Finance: pre-uni only (rest handled by dedicated scrapers above)
    ("UK", "Finance",    "2026", "pre-uni"),
    ("UK", "Finance",    "2027", "pre-uni"),
    # UK other industries
    ("UK", "Technology", "2026", "summer-internships"),
    ("UK", "Technology", "2027", "summer-internships"),
    ("UK", "Technology", "2026", "off-cycle-internships"),
    ("UK", "Technology", "2026", "graduate-programmes"),
    ("UK", "Law",        "2026", "training-contracts"),
    # North America — paused (too many US roles surfacing)
    # ("NA", "Finance",    "2026", "summer-internships"),
    # ("NA", "Finance",    "2027", "summer-internships"),
    # ("NA", "Technology", "2026", "summer-internships"),
    # ("NA", "Technology", "2027", "summer-internships"),
    # Europe
    ("EU", "Finance",    "2026", "summer-internships"),
    ("EU", "Finance",    "2027", "summer-internships"),
]


class TrackrScraper(BaseScraper):
    """
    Handles non-UK-Finance Trackr buckets (pre-uni, Technology, Law, NA, EU).
    UK Finance pages are scraped by the dedicated scrapers above.
    """
    source_id   = "trackr"
    source_name = "Trackr"
    tier        = 1

    def scrape(self) -> Iterator[dict]:
        for region, industry, season, prog_type in _OTHER_BUCKETS:
            yield from _scrape_bucket(self, region, industry, prog_type, seasons=[season])
