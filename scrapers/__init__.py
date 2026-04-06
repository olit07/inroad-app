"""
CCC Backend — Scraper registry
Import and instantiate all scrapers from one place.
"""
from scrapers.greenhouse import GreenhouseScraper
from scrapers.lever      import LeverScraper
from scrapers.reed       import ReedUKScraper
from scrapers.trackr     import TrackrScraper
from scrapers.adzuna     import AdzunaScraper
from scrapers.workday    import WorkdayScraper
from scrapers.handshake  import HandshakeScraper


def get_all_scrapers() -> list:
    """Return one instance of every scraper. Trackr first — highest quality."""
    return [
        TrackrScraper(),
        GreenhouseScraper(),
        LeverScraper(),
        WorkdayScraper(),
        HandshakeScraper(),
        ReedUKScraper(),
        AdzunaScraper(),
    ]


def get_scraper_by_id(source_id: str):
    """Return a specific scraper by its source_id."""
    for s in get_all_scrapers():
        if s.source_id == source_id:
            return s
    raise ValueError(f"No scraper with source_id '{source_id}'")
