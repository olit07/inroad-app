"""
inroad Backend — Base scraper
All source scrapers inherit from BaseScraper.
"""
import re
import time
import random
import logging
import urllib.request
import urllib.parse
import urllib.error
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    USER_AGENTS, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT,
    MAX_RETRIES, SENIORITY_KEYWORDS, INDUSTRIES,
)

logger = logging.getLogger(__name__)


# ── Job dict contract ────────────────────────────────────────────────────────

def make_job(
    company_name: str,
    title: str,
    source_id: str,
    source_name: str,
    url: str = "",
    industries: list[str] | None = None,
    seniority: str = "",
    employment_type: str = "",
    region: str = "UK",
    posted_date: str = "",
    closing_date: str = "",
) -> dict:
    """Construct a validated job dict."""
    return {
        "company_name":    company_name.strip(),
        "title":           title.strip(),
        "url":             url.strip(),
        "industries":      industries or [],
        "seniority":       seniority,
        "employment_type": employment_type,
        "role_type":       infer_role_type(title),
        "region":          region,
        "posted_date":     posted_date,
        "closing_date":    closing_date,
        "source_id":       source_id,
        "source_name":     source_name,
    }


# ── HTTP helpers ─────────────────────────────────────────────────────────────

class RequestError(Exception):
    pass


def fetch_url(url: str, headers: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> bytes:
    """Fetch a URL with retries and rotating user agents."""
    import gzip as _gzip
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    if headers:
        h.update(headers)

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = resp.headers.get("Content-Encoding", "")
                if enc == "gzip":
                    raw = _gzip.decompress(raw)
                elif enc == "deflate":
                    import zlib
                    raw = zlib.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < MAX_RETRIES - 1:
                wait = (2 ** attempt) * 3
                logger.warning(f"Rate limited ({e.code}) fetching {url}, waiting {wait}s")
                time.sleep(wait)
            else:
                raise RequestError(f"HTTP {e.code} fetching {url}") from e
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                raise RequestError(f"URL error fetching {url}: {e}") from e
    raise RequestError(f"All retries exhausted for {url}")


def fetch_json(url: str, headers: dict | None = None) -> dict | list:
    """Fetch and decode JSON from a URL."""
    import json
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    raw = fetch_url(url, headers=h)
    return json.loads(raw.decode("utf-8", errors="replace"))


# ── Normalisation helpers ─────────────────────────────────────────────────────

def infer_seniority(title: str) -> str:
    """Infer seniority band from job title."""
    t = title.lower()
    for band, keywords in SENIORITY_KEYWORDS.items():
        if any(k in t for k in keywords):
            return band
    return "mid"  # default


SENIOR_EXCLUDE_KEYWORDS = {
    "senior", "director", "president", "vice president", " vp ",
    "head of", "managing director", " md ", "chief", " partner",
    "principal", " manager", " lead", "staff engineer",
    "associate director", "associate vp",
}


def is_too_senior(title: str) -> bool:
    """Return True if the title indicates a role too senior for entry-level targeting."""
    t = title.lower()
    return any(k in t for k in SENIOR_EXCLUDE_KEYWORDS)


_INTERNSHIP_GRAD_KEYWORDS = {
    "intern", "internship", "placement", "summer analyst", "summer intern",
    "spring week", "spring intern", "spring analyst", "graduate", "grad",
    "grad scheme", "graduate scheme", "training contract", "vacation scheme",
    "insight programme", "insight program", "work experience", "sponsored degree",
}


def infer_role_type(title: str) -> str:
    """
    Classify a job that has already passed the entry-level gate as either:
      'internship_grad' — explicitly programme-based (intern, placement, grad scheme, etc.)
      'entry_level'     — permanent junior position (analyst, associate, junior, etc.)
    """
    t = title.lower()
    if any(k in t for k in _INTERNSHIP_GRAD_KEYWORDS):
        return "internship_grad"
    return "entry_level"


INDUSTRY_KEYWORD_MAP: dict[str, list[str]] = {
    "Finance":              ["finance", "financial", "treasury", "accounting", "asset management", "hedge fund", "quant",
                             "trading", "securities", "fixed income", "fic "],
    "Investment Banking":   ["investment bank", "m&a", "mergers", "acquisition", "capital markets", "ipo", "equity research", "dcm", "ecm"],
    # Software Engineering checked BEFORE Technology so coding roles aren't swallowed by the broader bucket
    "Software Engineering": ["software engineer", "software developer", "developer", "swe",
                             "backend", "front-end", "frontend", "full stack", "fullstack",
                             "devops", "sre", "site reliability", "mobile engineer",
                             "ml engineer", "machine learning engineer", "ai engineer",
                             "platform engineer", "data engineer", "infrastructure engineer",
                             "cloud engineer", "embedded", "firmware",
                             "ai infrastructure", "algorithm researcher"],
    # Technology = non-coding roles at tech companies (ops, analysts, consultants in tech context)
    "Technology":           ["technology analyst", "it analyst", "digital analyst",
                             "technical analyst", "tech operations", "it operations",
                             "digital transformation", "solutions analyst", "systems analyst",
                             "technology consultant", "tech consultant", "it consultant",
                             "saas", "enterprise technology", "technology program",
                             "tech grad", "electrical engineer", "computer engineer",
                             "photonics", "manufacturing engineer", "process integration",
                             "semiconductor", "hardware engineer", "vlsi", "fpga"],
    "Product Management":   ["product manager", "product management", "pm ", "product owner", "product lead"],
    "Consulting":           ["consulting", "consultant", "advisory", "management consulting", "strategy consulting"],
    "Strategy":             ["strategy", "strategic", "corporate development", "business development", "biz dev"],
    "Marketing":            ["marketing", "brand", "communications", "pr ", "public relations", "content", "seo", "sem"],
    "Law":                  ["law", "legal", "solicitor", "barrister", "paralegal", "compliance", "regulatory"],
    "Healthcare":           ["healthcare", "health", "medical", "clinical", "pharma", "biotech", "life sciences", "nhs"],
    "Media & Journalism":   ["media", "journalism", "journalist", "editorial", "publishing", "broadcast", "news"],
    "Data & Analytics":     ["data", "analytics", "analyst", "data science", "data engineer", "bi ", "business intelligence", "sql", "python"],
    "Real Estate":          ["real estate", "property", "reits", "asset management", "facilities"],
    "Non-profit & Policy":  ["non-profit", "nonprofit", "ngo", "policy", "government", "public sector", "charity", "third sector"],
}


def infer_industries(title: str, description: str = "") -> list[str]:
    """Infer industry tags from title and description."""
    text = (title + " " + description).lower()
    found = []
    for industry, keywords in INDUSTRY_KEYWORD_MAP.items():
        if any(k in text for k in keywords):
            found.append(industry)
    return found[:3] if found else ["Other"]


def clean_date(raw: str) -> str:
    """Try to parse a date string into ISO format YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    # ISO datetime: "2025-09-21T00:00:00.000Z" or "2025-09-21T..." — take first 10 chars
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    # Try common formats
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        from dateutil import parser as dparser
        return dparser.parse(raw, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def today_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


# ── Base class ───────────────────────────────────────────────────────────────

class BaseScraper(ABC):
    source_id:   str = ""
    source_name: str = ""
    tier:        int = 2

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._last_request: dict[str, float] = {}  # domain → timestamp

    def _throttle(self, url: str) -> None:
        """Enforce per-domain rate limiting."""
        domain = urllib.parse.urlparse(url).netloc
        last = self._last_request.get(domain, 0)
        wait = REQUEST_DELAY_SECONDS - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request[domain] = time.time()

    def fetch(self, url: str, **kwargs) -> bytes:
        self._throttle(url)
        return fetch_url(url, **kwargs)

    def fetch_json(self, url: str, **kwargs) -> dict | list:
        self._throttle(url)
        return fetch_json(url, **kwargs)

    @abstractmethod
    def scrape(self) -> Iterator[dict]:
        """Yield job dicts. Each must pass make_job() contract."""
        ...

    def run(self) -> list[dict]:
        """Run the scraper and return all jobs as a list."""
        jobs = []
        try:
            for job in self.scrape():
                if job.get("company_name") and job.get("title"):
                    jobs.append(job)
        except Exception as e:
            self.logger.error(f"Scraper {self.source_id} failed: {e}", exc_info=True)
        self.logger.info(f"{self.source_id}: collected {len(jobs)} jobs")
        return jobs
