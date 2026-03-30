"""
CCC Backend — Welcome to the Jungle Scraper

WTTJ exposes a public search API used by their frontend:
  GET https://api.welcometothejungle.com/api/v1/jobs?page=1&per_page=30&...

Note: WTTJ has Cloudflare. We use their official public search endpoint
which is less aggressively protected than their main site HTML.
"""
import json
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError
from scrapers.greenhouse import _infer_region, _infer_employment_type

logger = logging.getLogger(__name__)

API_BASE = "https://api.welcometothejungle.com/api/v1/jobs"

# Search term buckets mapped to industries
SEARCH_BUCKETS = [
    ("software engineer graduate",  ["Software Engineering", "Technology"],   "uk"),
    ("product manager",             ["Product Management", "Technology"],      "uk"),
    ("data analyst",                ["Data & Analytics", "Technology"],        "uk"),
    ("ux designer",                 ["Design & UX", "Technology"],             "uk"),
    ("marketing manager",           ["Marketing", "Growth"],                   "uk"),
    ("finance analyst",             ["Finance"],                               "uk"),
    ("software engineer",           ["Software Engineering", "Technology"],    "us"),
    ("product manager",             ["Product Management"],                    "us"),
    ("data scientist",              ["Data & Analytics"],                      "us"),
]


class WTTJScraper(BaseScraper):
    source_id   = "wttj"
    source_name = "Welcome to the Jungle"
    tier        = 2

    def scrape(self) -> Iterator[dict]:
        seen: set = set()

        for query, industries, country_code in SEARCH_BUCKETS:
            page = 1
            while page <= 3:  # max 3 pages per query
                url = (
                    f"{API_BASE}?query={query.replace(' ', '+')}"
                    f"&country_codes[]={country_code.upper()}"
                    f"&page={page}&per_page=30"
                )
                try:
                    data = self.fetch_json(url, headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://www.welcometothejungle.com/",
                    })
                except RequestError as e:
                    self.logger.warning(f"WTTJ fetch failed [{query}] p{page}: {e}")
                    break
                except Exception as e:
                    self.logger.warning(f"WTTJ error [{query}] p{page}: {e}")
                    break

                jobs_raw = []
                if isinstance(data, dict):
                    jobs_raw = data.get("jobs", data.get("results", []))
                elif isinstance(data, list):
                    jobs_raw = data

                if not jobs_raw:
                    break

                self.logger.info(f"WTTJ [{query} / {country_code}] p{page}: {len(jobs_raw)} jobs")

                for raw in jobs_raw:
                    try:
                        slug = raw.get("slug") or raw.get("reference", "")
                        if slug in seen:
                            continue
                        seen.add(slug)
                        job = self._parse_job(raw, industries, country_code)
                        if job:
                            yield job
                    except Exception as e:
                        self.logger.debug(f"WTTJ parse error: {e}")
                        continue

                meta = data.get("meta", {}) if isinstance(data, dict) else {}
                total_pages = meta.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

    def _parse_job(self, raw: dict, hint_industries: list, country_code: str) -> dict | None:
        title = raw.get("name", raw.get("title", "")).strip()

        # Company info
        company_obj  = raw.get("organization", raw.get("company", {}))
        company_name = ""
        if isinstance(company_obj, dict):
            company_name = company_obj.get("name", "")
        elif isinstance(company_obj, str):
            company_name = company_obj

        if not title or not company_name:
            return None

        # URL
        slug = raw.get("slug", raw.get("reference", ""))
        url  = f"https://www.welcometothejungle.com/jobs/{slug}" if slug else ""

        # Location
        offices = raw.get("offices", [])
        location = ""
        if offices and isinstance(offices, list):
            loc = offices[0]
            if isinstance(loc, dict):
                location = f"{loc.get('city', '')} {loc.get('country', '')}"

        region = "UK" if country_code.lower() == "uk" else "US"
        if location:
            region = _infer_region(location) or region

        # Date
        published_raw = raw.get("published_at", raw.get("created_at", ""))
        posted_date   = clean_date(published_raw) if published_raw else today_iso()

        # Description
        desc = raw.get("description", "") or ""
        if isinstance(desc, dict):
            desc = desc.get("body", "")

        industries = infer_industries(title, str(desc))
        if not industries or industries == ["Other"]:
            industries = hint_industries[:2]

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
