"""
CCC Backend — Greenhouse ATS Scraper

Greenhouse exposes a free public API for any company using their ATS:
  GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

No auth required. We maintain a curated list of board tokens below,
plus any stored in the ats_targets DB table.
"""
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError

logger = logging.getLogger(__name__)

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

# V0 scope: only internships, grad programmes, and entry-level roles
ENTRY_LEVEL_KEYWORDS = {
    "intern", "internship", "placement", "summer analyst", "spring week",
    "graduate", "grad", "entry level", "entry-level", "new grad", "trainee",
    "analyst", "associate", "junior", "apprentice", "scheme", "programme",
    "program", "training contract",
}

def _is_entry_level(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in ENTRY_LEVEL_KEYWORDS)

# Curated list of (company_name, board_token) — all known to hire UK/US grads
DEFAULT_TARGETS = [
    # ── Finance / IB / Quant ──────────────────────────────────────────────────
    ("Jane Street",          "janestreet"),
    ("Point72",              "point72"),
    ("Virtu Financial",      "virtu"),
    # IB Boutiques — add verified tokens here once confirmed

    # ── Technology — US/Global ────────────────────────────────────────────────
    ("Stripe",               "stripe"),
    ("Anthropic",            "anthropic"),
    ("Cloudflare",           "cloudflare"),
    ("Databricks",           "databricks"),
    ("Airbnb",               "airbnb"),
    ("Figma",                "figma"),
    ("Brex",                 "brex"),
    ("Coinbase",             "coinbase"),
    ("Scale AI",             "scaleai"),
    ("Robinhood",            "robinhood"),
    ("Klaviyo",              "klaviyo"),
    ("Intercom",             "intercom"),
    ("Asana",                "asana"),
    ("Airtable",             "airtable"),
    ("Attentive",            "attentive"),
    ("Ripple",               "ripple"),
    ("Vercel",               "vercel"),
    ("Mercury",              "mercury"),
    ("Amplitude",            "amplitude"),
    # ── Technology — AI ───────────────────────────────────────────────────────
    ("DeepMind",             "deepmind"),
    ("Stability AI",         "stabilityai"),
    # ── Technology — UK ───────────────────────────────────────────────────────
    ("Monzo",                "monzo"),
    ("GoCardless",           "gocardless"),
    ("Skyscanner",           "skyscanner"),
    # ── VC ────────────────────────────────────────────────────────────────────
    ("Andreessen Horowitz",  "a16z"),
    # ── Media ─────────────────────────────────────────────────────────────────
    ("Vox Media",            "voxmedia"),
    ("BuzzFeed",             "buzzfeed"),
]


class GreenhouseScraper(BaseScraper):
    source_id   = "greenhouse_feed"
    source_name = "Greenhouse ATS"
    tier        = 1

    def __init__(self, extra_targets: list[tuple[str, str]] | None = None):
        super().__init__()
        self.targets = list(DEFAULT_TARGETS)
        if extra_targets:
            self.targets.extend(extra_targets)

    def scrape(self) -> Iterator[dict]:
        for company_name, token in self.targets:
            url = BASE_URL.format(token=token)
            try:
                data = self.fetch_json(url)
            except RequestError as e:
                self.logger.warning(f"Greenhouse [{company_name}] fetch failed: {e}")
                continue
            except Exception as e:
                self.logger.warning(f"Greenhouse [{company_name}] unexpected error: {e}")
                continue

            jobs_raw = data if isinstance(data, list) else data.get("jobs", [])
            self.logger.info(f"Greenhouse [{company_name}]: {len(jobs_raw)} listings")

            for raw in jobs_raw:
                try:
                    job = self._parse_job(raw, company_name)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"Parse error [{company_name}]: {e}")
                    continue

    def _parse_job(self, raw: dict, company_name: str) -> dict | None:
        title = raw.get("title", "").strip()
        if not title:
            return None
        # V0 scope: skip anything that isn't intern / grad / entry-level
        if not _is_entry_level(title):
            return None

        url = raw.get("absolute_url", "")

        # Location / region
        location = ""
        loc_obj = raw.get("location", {})
        if isinstance(loc_obj, dict):
            location = loc_obj.get("name", "")
        elif isinstance(loc_obj, str):
            location = loc_obj

        region = _infer_region(location)

        # Posted date
        posted_raw = raw.get("updated_at", raw.get("first_published_at", ""))
        posted_date = clean_date(posted_raw) if posted_raw else today_iso()

        # Description for industry inference
        content = raw.get("content", "") or ""
        if isinstance(content, dict):
            content = content.get("body", "")

        industries = infer_industries(title, str(content))
        seniority   = infer_seniority(title)

        # Employment type
        dept = ""
        dept_obj = raw.get("departments", [])
        if dept_obj and isinstance(dept_obj, list):
            dept = dept_obj[0].get("name", "") if isinstance(dept_obj[0], dict) else str(dept_obj[0])

        job = make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries,
            seniority       = seniority,
            employment_type = _infer_employment_type(title),
            region          = region,
            posted_date     = posted_date,
        )
        job["opening_date"] = posted_date
        job["location"]     = location
        return job


def _infer_region(location: str) -> str:
    loc = location.lower()
    if any(k in loc for k in ["london", "manchester", "edinburgh", "birmingham", "uk", "united kingdom", "england", "scotland", "wales"]):
        return "UK"
    if any(k in loc for k in ["new york", "san francisco", "chicago", "boston", "los angeles", "seattle", "us", "usa", "united states", "remote"]):
        return "US"
    if any(k in loc for k in ["berlin", "paris", "amsterdam", "dublin", "zurich", "frankfurt", "europe", "eu"]):
        return "EU"
    return "Global"


def _infer_employment_type(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["intern", "internship", "placement", "summer"]):
        return "internship"
    if any(k in t for k in ["contract", "freelance", "temporary"]):
        return "contract"
    return "full-time"
