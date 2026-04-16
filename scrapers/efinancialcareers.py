"""
inroad — eFinancialCareers Scraper

Scrapes finance/IB/quant job listings from efinancialcareers.co.uk.
No API key required. Uses HTML parsing on search result pages.

URL format (confirmed working):
  /jobs/{sector}?contractType=2,3&datePosted={days}&page={n}
  contractType 2 = internship, 3 = graduate
  datePosted = days since posting (2 = last 2 days)
"""
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority, infer_industries,
    today_iso, RequestError,
)
from scrapers.greenhouse import _infer_employment_type

logger = logging.getLogger(__name__)

BASE_URL = "https://www.efinancialcareers.co.uk"

# (sector_slug, hint_industries) — confirmed working slugs
SEARCH_PAGES = [
    ("investment-banking",   ["Investment Banking"]),
    ("finance",              ["Finance"]),
    ("asset-management",     ["Finance"]),
    ("private-equity",       ["Finance", "Investment Banking"]),
    ("venture-capital",      ["Venture Capital"]),
    ("quantitative-finance", ["Finance"]),
    ("technology",           ["Technology", "Software Engineering"]),
    ("data-science",         ["Data & Analytics"]),
    ("risk-management",      ["Finance"]),
    ("consulting",           ["Consulting"]),
    ("compliance",           ["Finance", "Law"]),
]

# Only fetch jobs posted in the last N days
DATE_WINDOW_DAYS = 2

# How many pages per search to fetch
MAX_PAGES = 3


def _parse_relative_date(text: str) -> str:
    """Convert relative date text to ISO date. Returns today_iso() on failure."""
    text = text.lower().strip()
    if not text:
        return today_iso()
    if "today" in text or "just" in text or "hour" in text or "minute" in text:
        return today_iso()
    m = re.search(r"(\d+)\s+day", text)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s+week", text)
    if m:
        return (date.today() - timedelta(weeks=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s+month", text)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)) * 30)).isoformat()
    return today_iso()


class EFinancialCareersScraper(BaseScraper):
    source_id   = "efc"
    source_name = "eFinancialCareers"
    tier        = 1

    # Polite crawl rate — 3 seconds between requests
    _CRAWL_DELAY = 3.0

    # Realistic browser headers to avoid basic bot blocks
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.efinancialcareers.co.uk/",
    }

    def scrape(self) -> Iterator[dict]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            self.logger.error("beautifulsoup4 not installed — run: pip install beautifulsoup4")
            return

        seen_urls: set = set()

        for sector, hint_industries in SEARCH_PAGES:
            for page in range(1, MAX_PAGES + 1):
                url = (
                    f"{BASE_URL}/jobs/{sector}"
                    f"?contractType=2,3&datePosted={DATE_WINDOW_DAYS}&page={page}"
                )
                try:
                    raw_html = self.fetch(url, headers=self._HEADERS)
                    html = raw_html.decode("utf-8", errors="replace")
                except RequestError as e:
                    self.logger.warning(f"EFC fetch failed [{sector} p{page}]: {e}")
                    break
                except Exception as e:
                    self.logger.warning(f"EFC error [{sector} p{page}]: {e}")
                    break

                soup = BeautifulSoup(html, "html.parser")
                jobs_on_page = list(self._parse_cards(soup, hint_industries, seen_urls))

                if not jobs_on_page:
                    break  # empty page — stop paginating this search

                for job in jobs_on_page:
                    yield job

                time.sleep(self._CRAWL_DELAY)

    def _parse_cards(self, soup, hint_industries: list, seen_urls: set) -> Iterator[dict]:
        """Parse eFinancialCareers Angular job card structure (confirmed 2025)."""
        cards = soup.select("efc-job-card, .job-card")
        for card in cards:
            try:
                job = self._card_to_job(card, hint_industries, seen_urls)
                if job:
                    yield job
            except Exception as e:
                self.logger.debug(f"EFC card parse error: {e}")

    def _card_to_job(self, card, hint_industries: list, seen_urls: set) -> dict | None:
        # Title + URL — the main job link
        link_el = card.select_one("a[data-gtm-trackable='job']")
        if not link_el:
            link_el = card.select_one("a.job-title, a[efclink]")
        if not link_el:
            return None
        title = link_el.get("title", "").strip() or link_el.get_text(strip=True)
        href  = link_el.get("href", "")
        if not title or not href:
            return None
        url = href if href.startswith("http") else BASE_URL + href
        if url in seen_urls:
            return None
        seen_urls.add(url)

        # Company — Angular renders it into a plain div with class "company"
        co_el   = card.select_one("div.company, [class*='company']:not(efc-job-card)")
        company = co_el.get_text(strip=True) if co_el else ""
        if not company:
            # Fall back: employer img alt text
            img = card.select_one("img[itemprop='image']")
            company = img.get("alt", "").strip() if img else ""
        if not company:
            return None

        # Location
        loc_el   = card.select_one(".location, [class*='location']")
        location = loc_el.get_text(strip=True).split("·")[0].strip() if loc_el else "London"

        industries = infer_industries(title, "")
        if not industries:
            industries = hint_industries[:2]

        job = make_job(
            company_name    = company,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries[:3],
            seniority       = infer_seniority(title),
            employment_type = _infer_employment_type(title),
            region          = "UK",
            posted_date     = today_iso(),
        )
        job["opening_date"] = today_iso()
        job["location"]     = location
        return job
