"""
CCC — Handshake Scraper

Handshake exposes a public job search API.
Searches both UK and US endpoints for internships and full-time roles.
"""
import json, logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError
from scrapers.greenhouse import _infer_region, _infer_employment_type

logger = logging.getLogger(__name__)

ENDPOINTS = [
    ("https://app.joinhandshake.co.uk/edu/jobs", "UK"),
    ("https://app.joinhandshake.com/edu/jobs",   "US"),
]
JOB_TYPES = [("1", "internship"), ("2", "full-time")]


class HandshakeScraper(BaseScraper):
    source_id   = "handshake"
    source_name = "Handshake"
    tier        = 2

    def scrape(self) -> Iterator[dict]:
        seen: set = set()
        for base_url, region in ENDPOINTS:
            for type_id, type_name in JOB_TYPES:
                url = (
                    f"{base_url}?page=1&per_page=25"
                    f"&sort_direction=desc&sort_column=posted_date"
                    f"&job_types[]={type_id}"
                )
                try:
                    raw = self.fetch(url, headers={
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    })
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except RequestError as e:
                    self.logger.warning(f"Handshake [{region}/{type_name}]: {e}")
                    continue
                except Exception as e:
                    self.logger.debug(f"Handshake [{region}/{type_name}] error: {e}")
                    continue

                jobs_raw = data if isinstance(data, list) else data.get("jobs", data.get("results", []))
                self.logger.info(f"Handshake [{region}/{type_name}]: {len(jobs_raw)} jobs")

                for raw_job in jobs_raw:
                    try:
                        job_id = raw_job.get("id", "")
                        if job_id in seen:
                            continue
                        seen.add(job_id)
                        job = self._parse(raw_job, region, type_name)
                        if job:
                            yield job
                    except Exception as e:
                        self.logger.debug(f"Handshake parse error: {e}")

    def _parse(self, raw: dict, region: str, employment_type: str) -> dict | None:
        title = (raw.get("title") or raw.get("name") or "").strip()
        employer = raw.get("employer_name") or raw.get("employer", {})
        if isinstance(employer, dict):
            employer = employer.get("name", "")
        company_name = str(employer).strip()

        if not title or not company_name:
            return None

        url = raw.get("apply_url") or raw.get("url") or ""
        if not url and raw.get("id"):
            url = f"https://app.joinhandshake.com/jobs/{raw['id']}"

        exp_raw = raw.get("expiration_date") or raw.get("expires_at") or ""
        loc     = raw.get("location") or raw.get("city") or ""

        return make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = infer_industries(title),
            seniority       = infer_seniority(title),
            employment_type = employment_type,
            region          = _infer_region(loc) or region,
            posted_date     = today_iso(),
            closing_date    = clean_date(exp_raw),
        )
