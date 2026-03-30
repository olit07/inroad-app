"""
CCC Backend — Adzuna API Scraper

Free developer API (register at https://developer.adzuna.com):
  GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?app_id=...&app_key=...

Set ADZUNA_APP_ID and ADZUNA_APP_KEY environment variables.
Free tier: 250 API calls/month.
"""
import os
import json
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError
from scrapers.greenhouse import _infer_employment_type

logger = logging.getLogger(__name__)

API_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

SEARCH_QUERIES = [
    # (what, where, country_code, hint_industries)
    ("graduate finance",           "London",        "gb", ["Finance"]),
    ("graduate investment banking", "London",        "gb", ["Investment Banking"]),
    ("graduate software engineer", "London",        "gb", ["Software Engineering"]),
    ("graduate data analyst",      "London",        "gb", ["Data & Analytics"]),
    ("graduate product manager",   "London",        "gb", ["Product Management"]),
    ("graduate consultant",        "London",        "gb", ["Consulting"]),
    ("trainee solicitor",          "London",        "gb", ["Law"]),
    ("graduate marketing",         "London",        "gb", ["Marketing"]),
    ("graduate ux designer",       "London",        "gb", ["Design & UX"]),
    ("graduate venture capital",   "London",        "gb", ["Venture Capital"]),
    ("new grad software engineer", "New York",      "us", ["Software Engineering"]),
    ("entry level finance analyst", "New York",     "us", ["Finance"]),
    ("associate consultant",       "New York",      "us", ["Consulting"]),
    ("graduate data scientist",    "San Francisco", "us", ["Data & Analytics"]),
]


class AdzunaScraper(BaseScraper):
    source_id   = "adzuna"
    source_name = "Adzuna"
    tier        = 1

    def __init__(self):
        super().__init__()
        self.app_id  = os.environ.get("ADZUNA_APP_ID", "")
        self.app_key = os.environ.get("ADZUNA_APP_KEY", "")
        if not self.app_id or not self.app_key:
            self.logger.warning("ADZUNA_APP_ID / ADZUNA_APP_KEY not set — Adzuna will be skipped")

    def scrape(self) -> Iterator[dict]:
        if not self.app_id or not self.app_key:
            return

        seen_ids: set = set()

        for what, where, country, hint_industries in SEARCH_QUERIES:
            url = (
                API_BASE.format(country=country, page=1)
                + f"?app_id={self.app_id}&app_key={self.app_key}"
                + f"&what={what.replace(' ', '+')}"
                + f"&where={where.replace(' ', '+')}"
                + f"&results_per_page=50&content-type=application/json"
                + f"&sort_by=date&max_days_old=30"
            )
            try:
                data = self.fetch_json(url)
            except RequestError as e:
                self.logger.warning(f"Adzuna fetch failed [{what}]: {e}")
                continue
            except Exception as e:
                self.logger.warning(f"Adzuna error [{what}]: {e}")
                continue

            results = data.get("results", []) if isinstance(data, dict) else []
            self.logger.info(f"Adzuna [{what} / {where}]: {len(results)} results")

            for raw in results:
                try:
                    job_id = raw.get("id", "")
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    job = self._parse_result(raw, hint_industries, country)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"Adzuna parse error: {e}")
                    continue

    def _parse_result(self, raw: dict, hint_industries: list, country: str) -> dict | None:
        title        = raw.get("title", "").strip()
        company_obj  = raw.get("company", {})
        company_name = company_obj.get("display_name", "") if isinstance(company_obj, dict) else str(company_obj)

        if not title or not company_name:
            return None

        url         = raw.get("redirect_url", "")
        description = raw.get("description", "")
        region      = "UK" if country == "gb" else "US"

        posted_raw  = raw.get("created", "")
        posted_date = clean_date(posted_raw) if posted_raw else today_iso()

        industries = infer_industries(title, description)
        if not industries or industries == ["Other"]:
            industries = hint_industries

        return make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries[:3],
            seniority       = infer_seniority(title),
            employment_type = _infer_employment_type(title),
            region          = region,
            posted_date     = posted_date,
        )
