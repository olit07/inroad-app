"""
inroad — Lever ATS Scraper

Lever exposes a free public JSON feed per company:
  GET https://api.lever.co/v0/postings/{slug}?mode=json

No auth required. Response is a root-level JSON array of posting objects.
We maintain a curated list of verified company slugs below.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, today_iso, RequestError
from scrapers.greenhouse import _infer_region, _infer_employment_type

logger = logging.getLogger(__name__)

BASE_URL = "https://api.lever.co/v0/postings/{token}?mode=json"

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


def _ts_to_iso(ts_ms) -> str:
    """Convert Lever's Unix timestamp (milliseconds) to ISO date string."""
    if not ts_ms:
        return today_iso()
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return today_iso()


# Curated list of (company_name, lever_slug) — all verified live boards
DEFAULT_TARGETS = [
    # ── Finance / Fintech ─────────────────────────────────────────────────────
    ("Zopa",             "zopa"),           # UK digital bank / lender
    ("ION Group",        "ion"),            # Financial software, London/NY
    ("Binance",          "binance"),        # Crypto exchange
    ("Compass Lexecon",  "compasslexecon"), # Economic consulting, London/NY
    # ── Technology ────────────────────────────────────────────────────────────
    ("Palantir",         "palantir"),       # Data analytics, NY/London
    ("Spotify",          "spotify"),        # Music tech, London/NY
    ("Plaid",            "plaid"),          # Fintech infrastructure, US/UK
]


class LeverScraper(BaseScraper):
    source_id   = "lever_feed"
    source_name = "Lever ATS"
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
                self.logger.warning(f"Lever [{company_name}] fetch failed: {e}")
                continue
            except Exception as e:
                self.logger.warning(f"Lever [{company_name}] unexpected error: {e}")
                continue

            postings = data if isinstance(data, list) else []
            self.logger.info(f"Lever [{company_name}]: {len(postings)} listings")

            for raw in postings:
                try:
                    job = self._parse_posting(raw, company_name)
                    if job:
                        yield job
                except Exception as e:
                    self.logger.debug(f"Lever parse error [{company_name}]: {e}")
                    continue

    def _parse_posting(self, raw: dict, company_name: str) -> dict | None:
        title = (raw.get("text") or "").strip()
        if not title:
            return None
        # V0 scope: skip anything that isn't intern / grad / entry-level
        if not _is_entry_level(title):
            return None

        url = raw.get("hostedUrl") or raw.get("applyUrl") or ""

        # Location from categories
        cats     = raw.get("categories") or {}
        location = cats.get("location") or ""
        if not location:
            all_locs = cats.get("allLocations") or []
            location = all_locs[0] if all_locs else ""

        region = _infer_region(location)

        # Posted date — createdAt is ms epoch
        posted_date = _ts_to_iso(raw.get("createdAt"))

        # Employment type: check categories.commitment first, fall back to title
        commitment = (cats.get("commitment") or "").lower()
        if any(k in commitment for k in ["intern", "placement", "summer"]):
            emp_type = "internship"
        elif "contract" in commitment:
            emp_type = "contract"
        else:
            emp_type = _infer_employment_type(title)

        # Description for industry inference
        description = raw.get("descriptionPlain") or raw.get("description") or ""
        for lst in raw.get("lists") or []:
            if isinstance(lst, dict):
                description += " " + (lst.get("content") or "")

        industries = infer_industries(title, str(description))
        seniority  = infer_seniority(title)

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
