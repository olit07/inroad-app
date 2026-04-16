"""
CCC Backend — Reed.co.uk API Scraper

Reed has a free developer API (requires free registration):
  https://www.reed.co.uk/developers/jobseeker

GET https://www.reed.co.uk/api/1.0/search?keywords=graduate+finance&locationName=London&resultsToTake=100

Auth: HTTP Basic with API key as username, blank password.
Set REED_API_KEY environment variable.
"""
import os
import base64
import json
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError
from scrapers.greenhouse import _infer_employment_type

logger = logging.getLogger(__name__)

SEARCH_URL  = "https://www.reed.co.uk/api/1.0/search"
DETAIL_URL  = "https://www.reed.co.uk/api/1.0/jobs/{job_id}"

# Search queries to run — (keywords, location, label)
SEARCH_QUERIES = [
    # Finance
    ("graduate finance analyst",          "London",     "Finance"),
    ("investment banking analyst intern",  "London",     "Investment Banking"),
    ("graduate quant researcher",          "London",     "Finance"),
    ("venture capital analyst",            "London",     "Venture Capital"),
    # Technology
    ("graduate software engineer",         "London",     "Software Engineering"),
    ("junior software developer",          "Manchester", "Software Engineering"),
    ("data analyst graduate",              "London",     "Data & Analytics"),
    ("data scientist graduate",            "London",     "Data & Analytics"),
    ("product manager graduate",           "London",     "Product Management"),
    # Consulting
    ("management consultant graduate",     "London",     "Consulting"),
    ("strategy analyst graduate",          "London",     "Strategy"),
    # Marketing
    ("graduate marketing executive",       "London",     "Marketing"),
    # Law
    ("trainee solicitor",                  "London",     "Law"),
    ("paralegal graduate",                 "London",     "Law"),
    # Healthcare
    ("healthcare analyst graduate",        "London",     "Healthcare"),
    # Design
    ("UX designer graduate",               "London",     "Design & UX"),
    ("product designer graduate",          "London",     "Design & UX"),
    # Non-profit / Policy
    ("policy analyst graduate",            "London",     "Non-profit & Policy"),
    ("charity programme officer",          "London",     "Non-profit & Policy"),
    # Real Estate
    ("real estate graduate programme",     "London",     "Real Estate"),
    # Media
    ("journalist trainee",                 "London",     "Media & Journalism"),
    # US queries
    ("software engineer new grad",         "New York",   "Software Engineering"),
    ("investment banking analyst",         "New York",   "Investment Banking"),
    ("management consultant analyst",      "New York",   "Consulting"),
]


class ReedUKScraper(BaseScraper):
    source_id   = "reed_uk"
    source_name = "Reed.co.uk"
    tier        = 1

    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("REED_API_KEY", "")
        if not self.api_key:
            self.logger.warning("REED_API_KEY not set — Reed scraper will be skipped")

    def _auth_header(self) -> dict:
        creds = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    def scrape(self) -> Iterator[dict]:
        if not self.api_key:
            return

        seen_ids: set = set()

        for keywords, location, hint_industry in SEARCH_QUERIES:
            url = (
                f"{SEARCH_URL}?keywords={keywords.replace(' ', '+')}"
                f"&locationName={location.replace(' ', '+')}"
                f"&distancefromlocation=15&resultsToTake=100&graduate=true"
            )
            try:
                data = self.fetch_json(url, headers=self._auth_header())
            except RequestError as e:
                self.logger.warning(f"Reed fetch failed [{keywords}]: {e}")
                continue
            except Exception as e:
                self.logger.warning(f"Reed unexpected error [{keywords}]: {e}")
                continue

            results = data.get("results", []) if isinstance(data, dict) else []
            self.logger.info(f"Reed [{keywords} / {location}]: {len(results)} results")

            for raw in results:
                job_id = raw.get("jobId")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                try:
                    job = self._parse_result(raw, hint_industry)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"Reed parse error: {e}")
                    continue

    def _parse_result(self, raw: dict, hint_industry: str) -> dict | None:
        title        = raw.get("jobTitle", "").strip()
        company_name = raw.get("employerName", "Unknown").strip()
        if not title or not company_name:
            return None

        job_id   = raw.get("jobId", "")
        url      = f"https://www.reed.co.uk/jobs/{job_id}" if job_id else ""
        location = raw.get("locationName", "")

        region = "UK"
        if any(k in location.lower() for k in ["new york", "san francisco", "chicago", "us", "usa"]):
            region = "US"

        posted_raw  = raw.get("date", "")
        posted_date = clean_date(posted_raw) if posted_raw else today_iso()

        desc = raw.get("jobDescription", "") or raw.get("jobTitle", "")
        industries = infer_industries(title, desc)
        if hint_industry and hint_industry not in industries:
            industries = [hint_industry] + industries[:2]

        seniority = infer_seniority(title)
        salary    = raw.get("minimumSalary")
        if salary and salary < 25000:
            seniority = "intern"

        job = make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries[:3],
            seniority       = seniority,
            employment_type = _infer_employment_type(title),
            region          = region,
            posted_date     = posted_date,
        )
        job["opening_date"] = posted_date
        job["location"]     = location
        return job
