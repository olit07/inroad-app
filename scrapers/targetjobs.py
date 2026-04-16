"""
inroad — TargetJobs.co.uk Scraper

Scrapes graduate and internship listings from targetjobs.co.uk.
No API key required. Respects robots.txt crawl-delay of 10 seconds.

Extracts structured data from JSON-LD <script> blocks where available,
falling back to HTML card parsing.
"""
import json
import logging
import re
import time
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority, infer_industries,
    clean_date, today_iso, RequestError,
)
from scrapers.greenhouse import _infer_employment_type

logger = logging.getLogger(__name__)

BASE_URL = "https://targetjobs.co.uk"

# Search pages to crawl — (path, hint_industries)
SEARCH_PAGES = [
    # Internships — verified slugs
    ("/internships/finance",                ["Finance"]),
    ("/internships/investment-banking",     ["Investment Banking"]),
    ("/internships/accounting",             ["Finance"]),
    ("/internships/it",                     ["Technology", "Software Engineering"]),
    # Graduate jobs — verified slugs
    ("/graduate-jobs/finance",              ["Finance"]),
    ("/graduate-jobs/banking",              ["Investment Banking"]),
    ("/graduate-jobs/accounting",           ["Finance"]),
    ("/graduate-jobs/law",                  ["Law"]),
    ("/graduate-jobs/it",                   ["Technology", "Software Engineering"]),
    ("/graduate-jobs/consulting",           ["Consulting"]),
    ("/graduate-jobs/business-management",  ["Strategy", "Consulting"]),
    ("/graduate-jobs/marketing",            ["Marketing"]),
    ("/graduate-jobs/creative-arts-design", ["Design & UX"]),
    ("/graduate-jobs/london",               ["Finance", "Technology", "Consulting"]),
]


def _parse_relative_date(text: str) -> str:
    """
    Parse relative date strings like 'Posted 3 days ago', 'Posted today'.
    Returns ISO date string or empty string.
    """
    from datetime import date, timedelta
    text = text.lower().strip()
    if "today" in text or "just" in text or "hour" in text:
        return today_iso()
    m = re.search(r"(\d+)\s+day", text)
    if m:
        days = int(m.group(1))
        return (date.today() - timedelta(days=days)).isoformat()
    m = re.search(r"(\d+)\s+week", text)
    if m:
        weeks = int(m.group(1))
        return (date.today() - timedelta(weeks=weeks)).isoformat()
    m = re.search(r"(\d+)\s+month", text)
    if m:
        months = int(m.group(1))
        return (date.today() - timedelta(days=months * 30)).isoformat()
    return ""


class TargetJobsScraper(BaseScraper):
    source_id   = "targetjobs"
    source_name = "TargetJobs"
    tier        = 1

    # Override base throttle — robots.txt requires 10s crawl delay
    _CRAWL_DELAY = 10.0

    def scrape(self) -> Iterator[dict]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            self.logger.error("beautifulsoup4 not installed — run: pip install beautifulsoup4")
            return

        seen_urls: set = set()

        for path, hint_industries in SEARCH_PAGES:
            url = BASE_URL + path
            for page in range(1, 6):  # up to 5 pages per section
                page_url = f"{url}?page={page}" if page > 1 else url
                try:
                    raw_html = self.fetch(page_url)
                    html = raw_html.decode("utf-8", errors="replace")
                except RequestError as e:
                    self.logger.warning(f"TargetJobs fetch failed [{page_url}]: {e}")
                    break
                except Exception as e:
                    self.logger.warning(f"TargetJobs error [{page_url}]: {e}")
                    break

                soup = BeautifulSoup(html, "html.parser")

                # Try JSON-LD blocks first (most reliable)
                jobs_from_page = list(self._parse_jsonld(soup, hint_industries, seen_urls))

                # Fall back to HTML card parsing if JSON-LD yielded nothing
                if not jobs_from_page:
                    jobs_from_page = list(self._parse_cards(soup, hint_industries, seen_urls, base_url=BASE_URL))

                if not jobs_from_page:
                    break  # no jobs on this page — stop paginating

                for job in jobs_from_page:
                    yield job

                time.sleep(self._CRAWL_DELAY)

    def _parse_jsonld(self, soup, hint_industries: list, seen_urls: set) -> Iterator[dict]:
        """Extract jobs from JSON-LD JobPosting blocks."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") != "JobPosting":
                    continue
                try:
                    job = self._jsonld_to_job(item, hint_industries, seen_urls)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"TargetJobs JSON-LD parse error: {e}")

    def _jsonld_to_job(self, item: dict, hint_industries: list, seen_urls: set) -> dict | None:
        title = (item.get("title") or item.get("name") or "").strip()
        if not title:
            return None

        org   = item.get("hiringOrganization") or {}
        company = (org.get("name") if isinstance(org, dict) else str(org) or "").strip()
        if not company:
            return None

        url = (item.get("url") or item.get("identifier", {}).get("value") or "").strip()
        if not url or url in seen_urls:
            return None
        seen_urls.add(url)

        # Location
        loc_obj  = item.get("jobLocation") or {}
        address  = loc_obj.get("address") or {} if isinstance(loc_obj, dict) else {}
        location = ""
        if isinstance(address, dict):
            location = address.get("addressLocality") or address.get("addressRegion") or ""
        elif isinstance(address, str):
            location = address

        # Date
        opening_date = clean_date(item.get("datePosted") or "") or today_iso()

        industries = infer_industries(title, item.get("description") or "")
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
            posted_date     = opening_date,
        )
        job["opening_date"] = opening_date
        job["location"]     = location
        return job

    def _parse_cards(self, soup, hint_industries: list, seen_urls: set, base_url: str) -> Iterator[dict]:
        """Fallback: parse job cards from HTML."""
        # TargetJobs uses various card selectors depending on page type
        cards = (
            soup.select("article.job-card") or
            soup.select("div.job-listing") or
            soup.select("li.job-result") or
            soup.select("[data-job-id]") or
            soup.select(".vacancy-item")
        )
        for card in cards:
            try:
                job = self._card_to_job(card, hint_industries, seen_urls, base_url)
                if job:
                    yield job
            except Exception as e:
                self.logger.debug(f"TargetJobs card parse error: {e}")

    def _card_to_job(self, card, hint_industries: list, seen_urls: set, base_url: str) -> dict | None:
        # Title
        title_el = card.select_one("h2, h3, .job-title, [class*='title']")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            return None

        # Company
        co_el   = card.select_one(".employer, .company, [class*='employer'], [class*='company']")
        company = co_el.get_text(strip=True) if co_el else ""
        if not company:
            return None

        # URL
        link = card.select_one("a[href]")
        href = link["href"] if link else ""
        url  = href if href.startswith("http") else base_url + href
        if not url or url in seen_urls:
            return None
        seen_urls.add(url)

        # Location
        loc_el   = card.select_one(".location, [class*='location']")
        location = loc_el.get_text(strip=True) if loc_el else "London"

        # Date — look for relative date text
        date_el      = card.select_one(".date, .posted, [class*='date'], time")
        date_text    = date_el.get_text(strip=True) if date_el else ""
        opening_date = _parse_relative_date(date_text) or today_iso()

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
            posted_date     = opening_date,
        )
        job["opening_date"] = opening_date
        job["location"]     = location
        return job
