"""
inroad — Ashby ATS Scraper

Ashby exposes a free public API for any company using their ATS:
  GET https://api.ashbyhq.com/posting-api/job-board/{slug}

No auth required. Response: {"jobs": [...]}
We maintain a curated list of verified company slugs below.
"""
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority, infer_industries,
    clean_date, today_iso, RequestError,
)
from scrapers.greenhouse import _infer_region, _infer_employment_type

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

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


# Curated list of (company_name, ashby_slug) — all verified live boards
DEFAULT_TARGETS = [
    # ── AI / Technology ───────────────────────────────────────────────────────
    ("OpenAI",          "openai"),          # SF/London, top AI employer
    ("Cohere",          "cohere"),          # AI/NLP startup, US/UK
    ("Harvey",          "harvey"),          # Legal AI, US/UK
    ("Linear",          "linear"),          # Product management SaaS
    ("ElevenLabs",      "elevenlabs"),      # Voice AI, US/global
    ("Replit",          "replit"),          # Online IDE / AI coding
    # ── Finance / Fintech / Payments ──────────────────────────────────────────
    ("Ramp",            "ramp"),            # Corporate cards / fintech, US
    ("Airwallex",       "airwallex"),       # Global payments, London/HK
    ("Deel",            "deel"),            # Global HR / payments
    # ── Data / Analytics ──────────────────────────────────────────────────────
    ("Snowflake",       "snowflake"),       # Data cloud, global
    # ── Product / Design ──────────────────────────────────────────────────────
    ("Notion",          "notion"),          # Productivity SaaS, US/global
    # ── Operations / UK ───────────────────────────────────────────────────────
    ("Deliveroo",       "deliveroo"),       # UK food delivery / operations
]


class AshbyScraper(BaseScraper):
    source_id   = "ashby_ats"
    source_name = "Ashby ATS"
    tier        = 1

    def __init__(self, extra_targets: list[tuple[str, str]] | None = None):
        super().__init__()
        self.targets = list(DEFAULT_TARGETS)
        if extra_targets:
            self.targets.extend(extra_targets)

    def scrape(self) -> Iterator[dict]:
        for company_name, slug in self.targets:
            url = BASE_URL.format(slug=slug)
            try:
                data = self.fetch_json(url)
            except RequestError as e:
                self.logger.warning(f"Ashby [{company_name}] fetch failed: {e}")
                continue
            except Exception as e:
                self.logger.warning(f"Ashby [{company_name}] unexpected error: {e}")
                continue

            # Ashby wraps jobs in a top-level {"jobs": [...]} object
            jobs_raw = data.get("jobs", []) if isinstance(data, dict) else []
            self.logger.info(f"Ashby [{company_name}]: {len(jobs_raw)} listings")

            for raw in jobs_raw:
                try:
                    job = self._parse_job(raw, company_name)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"Ashby parse error [{company_name}]: {e}")
                    continue

    def _parse_job(self, raw: dict, company_name: str) -> dict | None:
        title = (raw.get("title") or "").strip()
        if not title:
            return None
        # V0 scope: skip anything that isn't intern / grad / entry-level
        if not _is_entry_level(title):
            return None

        url      = raw.get("jobUrl") or raw.get("applyUrl") or ""
        location = raw.get("location") or ""

        # secondaryLocations fallback
        if not location:
            sec = raw.get("secondaryLocations") or []
            location = sec[0] if sec else ""

        region = _infer_region(location)

        # publishedAt is ISO 8601 datetime string
        published_at = raw.get("publishedAt") or ""
        posted_date  = clean_date(published_at) if published_at else today_iso()

        # Ashby employmentType values: "FullTime", "PartTime", "Intern", "Contractor"
        emp_raw = (raw.get("employmentType") or "").lower()
        if "intern" in emp_raw:
            emp_type = "internship"
        elif "contract" in emp_raw or "contractor" in emp_raw:
            emp_type = "contract"
        else:
            emp_type = _infer_employment_type(title)

        dept        = raw.get("department") or raw.get("team") or ""
        description = raw.get("descriptionPlain") or ""
        industries  = infer_industries(title, f"{dept} {description}")
        seniority   = infer_seniority(title)

        job = make_job(
            company_name    = company_name,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = url,
            industries      = industries,
            seniority       = seniority,
            employment_type = emp_type,
            region          = region,
            posted_date     = posted_date,
        )
        job["opening_date"] = posted_date
        job["location"]     = location
        return job
