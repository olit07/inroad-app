"""
Scraper registry — import and instantiate all active scrapers.

Active sources:
  1. Trackr              — gold standard, curated UK internships
  2. Reed.co.uk          — official API, broad UK graduate/internship coverage
                           (requires REED_API_KEY env var)
  3. Adzuna              — official API, UK + US graduate roles
                           (requires ADZUNA_APP_ID + ADZUNA_APP_KEY env vars)
  4. Greenhouse ATS      — public JSON API, direct company career boards
  5. Lever ATS           — public JSON API, direct company career boards
  6. Ashby ATS           — public JSON API, direct company career boards
  7. Workday ATS         — public JSON API, large employers (banks, consulting, pharma)
  8. eFinancialCareers   — HTML scraper, finance/IB/quant (requires beautifulsoup4)

Note: TargetJobs is disabled — site renders job listings via JavaScript; static HTTP
fetching returns an empty shell and the scraper produces 0 jobs.
"""
import os
from scrapers.trackr import (
    TrackrScraper,
    TrackrSummerInternshipsScraper,
    TrackrSpringWeeksScraper,
    TrackrOffCycleScraper,
    TrackrIndustrialPlacementsScraper,
    TrackrGradProgrammesScraper,
    TrackrEventsScraper,
    TrackrEA27SummerInternshipsScraper,
    TrackrEA27SpringWeeksScraper,
    TrackrEA27OffCycleScraper,
    TrackrEA27IndustrialPlacementsScraper,
    TrackrEA27GradProgrammesScraper,
    TrackrEA27EventsScraper,
)
from scrapers.greenhouse import GreenhouseScraper
from scrapers.lever import LeverScraper
from scrapers.ashby import AshbyScraper
from scrapers.workday import WorkdayScraper
from scrapers.efinancialcareers import EFinancialCareersScraper


def get_all_scrapers() -> list:
    """Return one instance of every scraper. Trackr first — highest quality."""
    scrapers = [
        # UK Finance — one dedicated scraper per page
        TrackrSummerInternshipsScraper(),
        TrackrSpringWeeksScraper(),
        TrackrOffCycleScraper(),
        TrackrIndustrialPlacementsScraper(),
        TrackrGradProgrammesScraper(),
        TrackrEventsScraper(),
        # UK Finance 2027 early-access pages
        TrackrEA27SummerInternshipsScraper(),
        TrackrEA27SpringWeeksScraper(),
        TrackrEA27OffCycleScraper(),
        TrackrEA27IndustrialPlacementsScraper(),
        TrackrEA27GradProgrammesScraper(),
        TrackrEA27EventsScraper(),
        # Everything else (pre-uni, Technology, Law, NA, EU)
        TrackrScraper(),
    ]

    # Reed — activated when REED_API_KEY is set (free registration at reed.co.uk/developers)
    if os.environ.get("REED_API_KEY"):
        from scrapers.reed import ReedUKScraper
        scrapers.append(ReedUKScraper())

    # Adzuna — activated when both keys are set (free registration at developer.adzuna.com)
    if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        from scrapers.adzuna import AdzunaScraper
        scrapers.append(AdzunaScraper())

    # Greenhouse ATS — no key needed, scrapes public company career boards
    scrapers.append(GreenhouseScraper())

    # Lever ATS — no key needed, scrapes public company career boards
    scrapers.append(LeverScraper())

    # Ashby ATS — no key needed, scrapes public company career boards
    scrapers.append(AshbyScraper())

    # Workday ATS — no key needed; covers banks, consulting, pharma, big tech
    scrapers.append(WorkdayScraper())

    # eFinancialCareers — HTML scraper; covers finance, IB, quant, risk, compliance
    # Requires: pip install beautifulsoup4
    scrapers.append(EFinancialCareersScraper())

    return scrapers


def get_scraper_by_id(source_id: str):
    """Return a specific scraper by its source_id."""
    for s in get_all_scrapers():
        if s.source_id == source_id:
            return s
    raise ValueError(f"No scraper with source_id '{source_id}'")
