"""
Scraper registry — import and instantiate all active scrapers.
"""
from scrapers.trackr import TrackrScraper


def get_all_scrapers() -> list:
    """Return one instance of every scraper. Trackr first — highest quality."""
    return [
        TrackrScraper(),
    ]


def get_scraper_by_id(source_id: str):
    """Return a specific scraper by its source_id."""
    for s in get_all_scrapers():
        if s.source_id == source_id:
            return s
    raise ValueError(f"No scraper with source_id '{source_id}'")
