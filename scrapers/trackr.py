"""
Trackr Scraper  (Gold Standard source #1)

Public JSON API — no auth required.
  GET https://api.the-trackr.com/programmes
    ?region=UK|NA|EU
    &industry=Finance|Technology|Law
    &season=2026|2027
    &type=summer-internships|spring-weeks|...

UK Finance has a dedicated scraper per programme type so each page can be
run, tested and monitored independently:

  TrackrSummerInternshipsScraper   → summer-internships
  TrackrSpringWeeksScraper         → spring-weeks
  TrackrOffCycleScraper            → off-cycle-internships
  TrackrIndustrialPlacementsScraper→ industrial-placements
  TrackrGradProgrammesScraper      → graduate-programmes
  TrackrEventsScraper              → events

TrackrScraper handles remaining buckets (pre-uni, Technology, Law, NA, EU).
"""
import logging
import urllib.request
from datetime import date
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority,
    clean_date, RequestError,
)
from config.settings import COMPANY_SIZE_LOOKUP

logger = logging.getLogger(__name__)

API_BASE = "https://api.the-trackr.com/programmes"

REGION_MAP = {"UK": "UK", "NA": "US", "EU": "EU"}

INDUSTRY_MAP = {
    "Finance":    ["Finance", "Investment Banking"],
    "Technology": ["Technology", "Software Engineering", "Data & Analytics"],
    "Law":        ["Law"],
}

TYPE_MAP = {
    "summer-internships":    "internship",
    "spring-weeks":          "internship",
    "insight-programmes":    "internship",
    "off-cycle-internships": "internship",
    "industrial-placements": "internship",
    "graduate-programmes":   "full-time",
    "full-time-programmes":  "full-time",
    "training-contracts":    "full-time",
    "pre-uni":               "internship",
    "events":                "event",
}

TRACKR_TYPE_LABEL = {
    "summer-internships":    "Summer Internship",
    "spring-weeks":          "Spring Week",
    "insight-programmes":    "Spring Week",
    "off-cycle-internships": "Off-Cycle Internship",
    "industrial-placements": "Industrial Placement",
    "graduate-programmes":   "Graduate Programme",
    "full-time-programmes":  "Graduate Programme",
    "training-contracts":    "Graduate Programme",
    "pre-uni":               "Pre-Uni",
    "events":                "Events",
}

_ACTIVE_SEASONS = ["2026", "2027"]


# ── URL helpers ───────────────────────────────────────────────────────────────

_careers_site_cache: dict[str, str] = {}

# Fallback careers URLs for companies where Trackr uses bit.ly or has no URL.
# Keys are lowercase company names exactly as Trackr returns them.
_CAREERS_SITE_OVERRIDES: dict[str, str] = {
    "ardian":              "https://www.ardian.com/join-us",
    "ubs":                 "https://www.ubs.com/global/en/careers.html",
    "rothschild & co":     "https://www.rothschildandco.com/en/careers/",
    "rothschild & co.":    "https://www.rothschildandco.com/en/careers/",
    "jpmorgan chase & co.":"https://careers.jpmorgan.com/",
    "barclays":            "https://home.barclays/careers/",
    "blackstone":          "https://www.blackstone.com/careers/",
    "citi":                "https://jobs.citi.com/",
    "ey":                  "https://www.ey.com/en_uk/careers",
    "deloitte":            "https://www2.deloitte.com/uk/en/pages/careers/",
    "deutsche bank":       "https://careers.db.com/",
    "goldman sachs":       "https://www.goldmansachs.com/careers/",
    "morgan stanley":      "https://www.morganstanley.com/people/careers",
    "hsbc":                "https://www.hsbc.com/careers",
    "bank of england":     "https://www.bankofengland.co.uk/careers",
    "wells fargo":         "https://www.wellsfargo.com/about/careers/",
    "pimco":               "https://www.pimco.com/en-us/our-firm/careers",
    "blackrock":           "https://careers.blackrock.com/",
    "citadel":             "https://www.citadel.com/careers/",
    "evercore":            "https://www.evercore.com/careers/",
    "lazard":              "https://www.lazard.com/careers/",
    "man group":           "https://www.man.com/careers",
    "marshall wace":       "https://www.marshallwace.com/careers",
    "mckinsey & company":  "https://www.mckinsey.com/careers",
    "millennium management":"https://www.mlp.com/careers/",
    "natwest markets":     "https://jobs.natwestgroup.com/",
}

def _resolve_url(url: str) -> str:
    """Follow redirects and return the final URL. Returns original on failure."""
    if not url:
        return url
    if url in _careers_site_cache:
        return _careers_site_cache[url]
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            resolved = resp.url
    except Exception:
        resolved = url
    _careers_site_cache[url] = resolved
    return resolved


_TRACKR_SOURCE_VALUES = {"trackr", "Trackr", "TRACKR"}

def _clean_url(url: str) -> str:
    """
    Strip Trackr tracking parameters from job listing URLs.
    Only removes a param when its value is a known Trackr identifier, so the
    function is idempotent. Preserves all functional query parameters.
    """
    if not url:
        return url

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    if not params:
        return url

    def _is_trackr(values):
        return any(v in _TRACKR_SOURCE_VALUES for v in values)

    had_trackr_source   = _is_trackr(params.get("utm_source", []))
    had_trackr_campaign = _is_trackr(params.get("utm_campaign", []))

    cleaned_params = {}
    for k, v in params.items():
        if k in ("utm_source", "utm_medium") and had_trackr_source:
            continue
        if k == "utm_campaign" and (had_trackr_source or had_trackr_campaign):
            continue
        if k in ("gh_src", "source", "codes") and _is_trackr(v):
            continue
        cleaned_params[k] = v

    cleaned = parsed._replace(query=urlencode(cleaned_params, doseq=True))
    return urlunparse(cleaned)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_programme(raw: dict, region: str, industry: str, prog_type: str) -> dict | None:
    title = (raw.get("name") or "").strip()
    if not title:
        return None

    company_obj  = raw.get("company") or {}
    company_name = (company_obj.get("name") or raw.get("companyId") or "").strip()
    if not company_name:
        return None

    careers_bitly = (company_obj.get("careersSite") or "").strip()
    careers_site  = _resolve_url(careers_bitly) if careers_bitly else ""
    if "bit.ly" in careers_site:
        careers_site = ""
    if not careers_site:
        careers_site = _CAREERS_SITE_OVERRIDES.get(company_name.lower().strip(), "")

    url          = _clean_url((raw.get("url") or "").strip())
    opening_date = clean_date(raw.get("openingDate") or "")
    closing_date = clean_date(raw.get("closingDate") or "")
    locations    = raw.get("locations") or []

    trackr_id = (raw.get("id") or "").strip()

    job = make_job(
        company_name    = company_name,
        title           = title,
        source_id       = "trackr",
        source_name     = "Trackr",
        url             = url,
        industries      = INDUSTRY_MAP.get(industry, [industry]),
        seniority       = infer_seniority(title),
        employment_type = TYPE_MAP.get(prog_type, "internship"),
        region          = REGION_MAP.get(region, region),
        posted_date     = opening_date or None,
        closing_date    = closing_date,
    )
    job["opening_date"]      = opening_date
    job["company_size"]      = COMPANY_SIZE_LOOKUP.get(company_name.lower().strip(), "")
    job["location"]          = ", ".join(locations) if locations else ""
    job["trackr_type"]       = prog_type
    job["source_identifier"] = trackr_id
    job["careers_site"]      = careers_site
    categories = raw.get("categories") or []
    job["trackr_categories"] = categories  # stored in raw JSON; used to derive vertical
    return job


def _scrape_bucket(
    scraper: "BaseScraper",
    region: str,
    industry: str,
    prog_type: str,
    seasons: list[str] = _ACTIVE_SEASONS,
    list_id: str = "",
) -> Iterator[dict]:
    """Yield open jobs for one (region, industry, prog_type) across all seasons."""
    seen: set = set()
    today = date.today().isoformat()

    for season in seasons:
        url = f"{API_BASE}?region={region}&industry={industry}&season={season}&type={prog_type}"
        if list_id:
            url += f"&listId={list_id}"
        try:
            data = scraper.fetch_json(url, headers={
                "Accept":  "application/json",
                "Referer": "https://app.the-trackr.com/",
                "Origin":  "https://app.the-trackr.com",
            })
        except RequestError as e:
            scraper.logger.warning(f"Trackr fetch failed [{region}/{industry}/{season}/{prog_type}]: {e}")
            continue
        except Exception as e:
            scraper.logger.error(f"Trackr error [{region}/{industry}/{season}/{prog_type}]: {e}", exc_info=True)
            continue

        if not isinstance(data, list):
            scraper.logger.warning(f"Trackr: unexpected response for {url}: {type(data)}")
            continue

        scraper.logger.info(f"Trackr [{region}/{industry}/{season}/{prog_type}]: {len(data)} records")
        count = 0
        for raw in data:
            try:
                prog_id = raw.get("id") or ""
                if prog_id in seen:
                    continue
                seen.add(prog_id)

                closing_date = clean_date(raw.get("closingDate") or "")
                if closing_date and closing_date < today:
                    continue

                job = _parse_programme(raw, region, industry, prog_type)
                if job:
                    yield job
                    count += 1
            except Exception as e:
                scraper.logger.debug(f"Trackr parse error: {e}", exc_info=True)

        scraper.logger.info(f"Trackr [{region}/{industry}/{season}/{prog_type}]: yielded {count} open")


# ── Dedicated UK Finance scrapers (one per page) ──────────────────────────────

class _TrackrUKFinanceBase(BaseScraper):
    source_name = "Trackr"
    tier        = 1
    PROG_TYPE: str  # set by each subclass

    def scrape(self) -> Iterator[dict]:
        yield from _scrape_bucket(self, "UK", "Finance", self.PROG_TYPE)


class TrackrSummerInternshipsScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/summer-internships"""
    source_id = "trackr_summer_internships"
    PROG_TYPE = "summer-internships"


class TrackrSpringWeeksScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/spring-weeks"""
    source_id = "trackr_spring_weeks"
    PROG_TYPE = "spring-weeks"


class TrackrOffCycleScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/off-cycle-internships"""
    source_id = "trackr_off_cycle"
    PROG_TYPE = "off-cycle-internships"


class TrackrIndustrialPlacementsScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/industrial-placements"""
    source_id = "trackr_industrial_placements"
    PROG_TYPE = "industrial-placements"


class TrackrGradProgrammesScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/graduate-programmes"""
    source_id = "trackr_grad_programmes"
    PROG_TYPE = "graduate-programmes"


class TrackrEventsScraper(_TrackrUKFinanceBase):
    """https://app.the-trackr.com/uk-finance/events"""
    source_id = "trackr_events"
    PROG_TYPE = "events"


# ── UK Finance 2027 early-access scrapers ────────────────────────────────────
# https://app.the-trackr.com/uk-finance-2027-early-access-x/<prog_type>
# Same API endpoint; listId param future-proofs against exclusive early-access content.

_EA27_LIST_ID = "uk-finance-2027-early-access-x"
_EA27_SEASONS = ["2027"]


class _TrackrEarlyAccess2027Base(BaseScraper):
    source_name = "Trackr"
    tier        = 1
    PROG_TYPE: str

    def scrape(self) -> Iterator[dict]:
        yield from _scrape_bucket(self, "UK", "Finance", self.PROG_TYPE,
                                   seasons=_EA27_SEASONS, list_id=_EA27_LIST_ID)


class TrackrEA27SummerInternshipsScraper(_TrackrEarlyAccess2027Base):
    """https://app.the-trackr.com/uk-finance-2027-early-access-x/summer-internships"""
    source_id = "trackr_ea27_summer_internships"
    PROG_TYPE = "summer-internships"


class TrackrEA27SpringWeeksScraper(_TrackrEarlyAccess2027Base):
    """https://app.the-trackr.com/uk-finance-2027-early-access-x/spring-weeks"""
    source_id = "trackr_ea27_spring_weeks"
    PROG_TYPE = "spring-weeks"


class TrackrEA27OffCycleScraper(_TrackrEarlyAccess2027Base):
    """https://app.the-trackr.com/uk-finance-2027-early-access-x/off-cycle-internships"""
    source_id = "trackr_ea27_off_cycle"
    PROG_TYPE = "off-cycle-internships"


class TrackrEA27IndustrialPlacementsScraper(_TrackrEarlyAccess2027Base):
    """https://app.the-trackr.com/uk-finance-2027-early-access-x/industrial-placements"""
    source_id = "trackr_ea27_industrial_placements"
    PROG_TYPE = "industrial-placements"


class TrackrEA27GradProgrammesScraper(_TrackrEarlyAccess2027Base):
    """https://app.the-trackr.com/uk-finance-2027-early-access-x/graduate-programmes"""
    source_id = "trackr_ea27_grad_programmes"
    PROG_TYPE = "graduate-programmes"


class TrackrEA27EventsScraper(_TrackrEarlyAccess2027Base):
    """https://app.the-trackr.com/uk-finance-2027-early-access-x/events"""
    source_id = "trackr_ea27_events"
    PROG_TYPE = "events"


# ── General scraper (non-UK-Finance buckets) ──────────────────────────────────

_OTHER_BUCKETS = [
    # UK Finance: pre-uni only (rest handled by dedicated scrapers above)
    ("UK", "Finance",    "2026", "pre-uni"),
    ("UK", "Finance",    "2027", "pre-uni"),
    # UK other industries
    ("UK", "Technology", "2026", "summer-internships"),
    ("UK", "Technology", "2027", "summer-internships"),
    ("UK", "Technology", "2026", "off-cycle-internships"),
    ("UK", "Technology", "2026", "graduate-programmes"),
    ("UK", "Law",        "2026", "training-contracts"),
    # North America — Finance 2027  (API uses region=US)
    ("US", "Finance",    "2027", "summer-internships"),
    ("US", "Finance",    "2027", "graduate-programmes"),
    ("US", "Finance",    "2027", "full-time-programmes"),
    ("US", "Finance",    "2027", "spring-weeks"),
    ("US", "Finance",    "2027", "insight-programmes"),
    # Europe — Finance
    ("EU", "Finance",    "2026", "summer-internships"),
    ("EU", "Finance",    "2027", "summer-internships"),
    ("EU", "Finance",    "2026", "off-cycle-internships"),
    ("EU", "Finance",    "2027", "off-cycle-internships"),
    ("EU", "Finance",    "2026", "graduate-programmes"),
    ("EU", "Finance",    "2027", "graduate-programmes"),
]


class TrackrScraper(BaseScraper):
    """
    Handles non-UK-Finance Trackr buckets (pre-uni, Technology, Law, NA, EU).
    UK Finance pages are scraped by the dedicated scrapers above.
    """
    source_id   = "trackr"
    source_name = "Trackr"
    tier        = 1

    def scrape(self) -> Iterator[dict]:
        for region, industry, season, prog_type in _OTHER_BUCKETS:
            yield from _scrape_bucket(self, region, industry, prog_type, seasons=[season])
