"""
CCC Backend — Bristol Trackr Scraper

Trackr (https://www.bris.ac.uk/careers/jobsandwork/trackr/) requires a
logged-in session. Set the TRACKR_SESSION_COOKIE environment variable
with the value of your session cookie (e.g. from DevTools > Application > Cookies).

The scraper fetches each Trackr list page (Finance UK, Tech UK, Law UK, etc.)
and parses the HTML table structure.
"""
import os
import re
import json
import logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

logger = logging.getLogger(__name__)

# Trackr page URLs (already filtered views, logged in required)
TRACKR_PAGES = [
    {
        "url":        "https://www.bris.ac.uk/careers/jobsandwork/trackr/?filter=finance-uk",
        "industries": ["Finance", "Investment Banking"],
        "region":     "UK",
        "label":      "Finance UK",
    },
    {
        "url":        "https://www.bris.ac.uk/careers/jobsandwork/trackr/?filter=tech-uk",
        "industries": ["Technology", "Software Engineering", "Data & Analytics"],
        "region":     "UK",
        "label":      "Tech UK",
    },
    {
        "url":        "https://www.bris.ac.uk/careers/jobsandwork/trackr/?filter=law-uk",
        "industries": ["Law"],
        "region":     "UK",
        "label":      "Law UK",
    },
    {
        "url":        "https://www.bris.ac.uk/careers/jobsandwork/trackr/?filter=consulting-uk",
        "industries": ["Consulting", "Strategy"],
        "region":     "UK",
        "label":      "Consulting UK",
    },
    {
        "url":        "https://www.bris.ac.uk/careers/jobsandwork/trackr/?filter=finance-us",
        "industries": ["Finance", "Investment Banking"],
        "region":     "US",
        "label":      "Finance US",
    },
    {
        "url":        "https://www.bris.ac.uk/careers/jobsandwork/trackr/?filter=tech-us",
        "industries": ["Technology", "Software Engineering"],
        "region":     "US",
        "label":      "Tech US",
    },
]

# Column header patterns to detect in the table
COL_COMPANY   = re.compile(r"company|firm|employer", re.I)
COL_PROGRAMME = re.compile(r"programme|program|role|title|position|job", re.I)
COL_OPENING   = re.compile(r"open|posted|start", re.I)
COL_CLOSING   = re.compile(r"clos|deadline|end|expir", re.I)
COL_APPLY     = re.compile(r"apply|link|url", re.I)


class TrackrScraper(BaseScraper):
    source_id   = "trackr"
    source_name = "Bristol Trackr"
    tier        = 2

    def __init__(self):
        super().__init__()
        self.session_cookie = os.environ.get("TRACKR_SESSION_COOKIE", "")
        if not self.session_cookie:
            self.logger.warning("TRACKR_SESSION_COOKIE not set — Trackr scraper will be skipped")

    def _headers(self) -> dict:
        h = {}
        if self.session_cookie:
            h["Cookie"] = self.session_cookie
        return h

    def scrape(self) -> Iterator[dict]:
        if not BS4_AVAILABLE:
            self.logger.error("beautifulsoup4 not installed — Trackr scraper disabled")
            return
        if not self.session_cookie:
            return

        for page_cfg in TRACKR_PAGES:
            url        = page_cfg["url"]
            industries = page_cfg["industries"]
            region     = page_cfg["region"]
            label      = page_cfg["label"]

            try:
                raw_html = self.fetch(url, headers=self._headers())
            except RequestError as e:
                self.logger.warning(f"Trackr [{label}] fetch failed: {e}")
                continue

            try:
                soup   = BeautifulSoup(raw_html, "html.parser")
                tables = soup.find_all("table")
                if not tables:
                    self.logger.warning(f"Trackr [{label}]: no tables found")
                    continue

                for table in tables:
                    yield from self._parse_table(table, industries, region, label)

            except Exception as e:
                self.logger.error(f"Trackr [{label}] parse failed: {e}", exc_info=True)
                continue

    def _parse_table(self, table, industries: list, region: str, label: str) -> Iterator[dict]:
        rows = table.find_all("tr")
        if len(rows) < 2:
            return

        # Detect header columns
        header_row = rows[0]
        headers    = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

        col_idx: dict[str, int] = {}
        for i, h in enumerate(headers):
            if COL_COMPANY.search(h):
                col_idx.setdefault("company", i)
            elif COL_PROGRAMME.search(h):
                col_idx.setdefault("title", i)
            elif COL_OPENING.search(h):
                col_idx.setdefault("posted_date", i)
            elif COL_CLOSING.search(h):
                col_idx.setdefault("closing_date", i)
            elif COL_APPLY.search(h):
                col_idx.setdefault("url", i)

        if "company" not in col_idx or "title" not in col_idx:
            # Fallback: assume col 0 = company, col 1 = title
            col_idx["company"] = 0
            col_idx["title"]   = 1

        count = 0
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            def cell_text(key: str) -> str:
                idx = col_idx.get(key, -1)
                if idx < 0 or idx >= len(cells):
                    return ""
                return cells[idx].get_text(separator=" ", strip=True)

            def cell_link(key: str) -> str:
                idx = col_idx.get(key, -1)
                if idx < 0 or idx >= len(cells):
                    return ""
                a = cells[idx].find("a", href=True)
                return a["href"] if a else ""

            company_name = cell_text("company")
            title        = cell_text("title")

            if not company_name or not title:
                continue

            # Extract apply link — from title cell or dedicated link cell
            url = cell_link("url") or cell_link("title")
            if not url:
                # Try any <a> in the row
                a = row.find("a", href=True)
                url = a["href"] if a else ""

            posted_date  = clean_date(cell_text("posted_date"))
            closing_date = clean_date(cell_text("closing_date"))

            inferred_industries = infer_industries(title)
            merged_industries   = list(dict.fromkeys(industries + inferred_industries))[:3]

            yield make_job(
                company_name    = company_name,
                title           = title,
                source_id       = self.source_id,
                source_name     = self.source_name,
                url             = url,
                industries      = merged_industries,
                seniority       = infer_seniority(title),
                employment_type = "internship" if "intern" in title.lower() else "full-time",
                region          = region,
                posted_date     = posted_date or today_iso(),
                closing_date    = closing_date,
            )
            count += 1

        self.logger.info(f"Trackr [{label}]: parsed {count} jobs from table")
