"""
inroad Backend — Welcome to the Jungle Scraper  (Gold Standard source #2)

Searches WTTJ's public API for V0 scope: internships, graduate programmes,
and entry-level roles in Finance, Technology, and Law only.
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

# Ordered list of API base URLs to try. The scraper probes them in sequence
# and uses the first that returns HTTP 200. Add new candidates at the front
# when a new endpoint is discovered.
API_BASE_URLS = [
    "https://api.welcometothejungle.com/api/v1/jobs",   # original
    "https://www.welcometothejungle.com/api/v2/jobs",   # v2 candidate
]

# V0 scope: internships / graduate programmes / entry-level only
# Finance, Technology, Law
SEARCH_BUCKETS = [
    # Finance — UK
    ("finance internship",                   ["Finance"],                                "uk"),
    ("investment banking summer analyst",    ["Investment Banking", "Finance"],          "uk"),
    ("finance graduate programme",           ["Finance", "Investment Banking"],          "uk"),
    ("financial analyst graduate",           ["Finance"],                                "uk"),
    # Technology — UK
    ("software engineer internship",         ["Software Engineering", "Technology"],     "uk"),
    ("software engineer graduate scheme",    ["Software Engineering", "Technology"],     "uk"),
    ("data analyst graduate",                ["Data & Analytics", "Technology"],         "uk"),
    ("product manager graduate",             ["Product Management", "Technology"],       "uk"),
    ("technology graduate scheme",           ["Technology", "Software Engineering"],     "uk"),
    # Law — UK
    ("training contract",                    ["Law"],                                    "uk"),
    ("paralegal graduate",                   ["Law"],                                    "uk"),
    ("legal internship",                     ["Law"],                                    "uk"),
    # Finance — US
    ("finance internship",                   ["Finance"],                                "us"),
    ("investment banking analyst",           ["Investment Banking", "Finance"],          "us"),
    ("summer analyst finance",               ["Finance", "Investment Banking"],          "us"),
    # Technology — US
    ("software engineer intern",             ["Software Engineering", "Technology"],     "us"),
    ("data analyst entry level",             ["Data & Analytics", "Technology"],         "us"),
]


class WTTJScraper(BaseScraper):
    source_id   = "wttj"
    source_name = "Welcome to the Jungle"
    tier        = 1  # Gold Standard

    def scrape(self) -> Iterator[dict]:
        seen: set = set()

        active_base = self._probe_api_base()
        if active_base is None:
            self.logger.error(
                f"WTTJ: all API base URLs failed probe — skipping scraper. "
                f"Tried: {API_BASE_URLS}. "
                "Check DevTools Network on welcometothejungle.com to find the current endpoint."
            )
            return

        self.logger.info(f"WTTJ: using API base URL: {active_base}")

        for query, industries, country_code in SEARCH_BUCKETS:
            page = 1
            while page <= 3:  # max 3 pages per query
                url = (
                    f"{active_base}?query={query.replace(' ', '+')}"
                    f"&country_codes[]={country_code.upper()}"
                    f"&page={page}&per_page=30"
                )
                try:
                    data = self.fetch_json(url, headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://www.welcometothejungle.com/",
                    })
                except RequestError as e:
                    err_str = str(e)
                    if "HTTP 404" in err_str:
                        self.logger.warning(f"WTTJ 404 [{query}] p{page} — endpoint may have moved: {url}")
                    elif "HTTP 401" in err_str or "HTTP 403" in err_str:
                        self.logger.warning(f"WTTJ auth error [{query}] p{page} — endpoint now requires auth")
                    elif "HTTP 429" in err_str:
                        self.logger.warning(f"WTTJ rate limited [{query}] p{page}")
                    else:
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
                    self.logger.debug(
                        f"WTTJ [{query} / {country_code}] p{page}: empty — "
                        f"keys: {list(data.keys()) if isinstance(data, dict) else 'list'}"
                    )
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

    def _probe_api_base(self) -> str | None:
        """Try each URL in API_BASE_URLS with a minimal probe request.
        Returns the first base URL that responds HTTP 200, or None if all fail.
        """
        probe_params = "?query=graduate+internship&country_codes[]=UK&page=1&per_page=1"
        for base in API_BASE_URLS:
            probe_url = f"{base}{probe_params}"
            try:
                data = self.fetch_json(probe_url, headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.welcometothejungle.com/",
                })
                # A 200 response (even empty) means the endpoint is live
                self.logger.debug(f"WTTJ probe OK: {base}")
                return base
            except RequestError as e:
                err_str = str(e)
                if "HTTP 404" in err_str:
                    self.logger.info(f"WTTJ probe 404 at {base} — trying next")
                elif "HTTP 401" in err_str or "HTTP 403" in err_str:
                    self.logger.info(f"WTTJ probe auth error at {base} — trying next")
                else:
                    self.logger.info(f"WTTJ probe failed at {base}: {e} — trying next")
            except Exception as e:
                self.logger.info(f"WTTJ probe unexpected error at {base}: {e} — trying next")
        return None

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

        # Closing date — WTTJ may expose published_until, apply_before, or deadline
        closing_raw  = (raw.get("published_until") or raw.get("apply_before")
                        or raw.get("deadline") or raw.get("expires_at") or "")
        closing_date = clean_date(closing_raw) if closing_raw else ""

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
            closing_date    = closing_date,
        )
