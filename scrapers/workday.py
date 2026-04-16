"""
CCC — Workday ATS Scraper

Workday exposes a JSON API used by their frontend:
  POST https://{sub}.wd{n}.myworkdayjobs.com/wday/cxs/{sub}/{board}/jobs
  Body: {"limit":20,"offset":0,"searchText":"graduate","appliedFacets":{}}
"""
import json, logging
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import BaseScraper, make_job, infer_seniority, infer_industries, clean_date, today_iso, RequestError, fetch_url
from scrapers.greenhouse import _infer_region, _infer_employment_type

logger = logging.getLogger(__name__)

# (company_name, subdomain, board_name, wd_number)
WORKDAY_TARGETS = [
    ("Unilever",             "unilever",       "Unilever",          3),
    ("HSBC",                 "hsbc",           "ExternalCareerSite", 1),
    ("Barclays",             "barclays",       "barclays",          5),
    ("Deutsche Bank",        "db",             "deutschebank",      3),
    ("UBS",                  "ubs",            "UBSCareers",        3),
    ("Morgan Stanley",       "morganstanley",  "Careers",           1),
    ("Citigroup",            "citi",           "CG",                1),
    ("BNP Paribas",          "bnpparibas",     "BNP",               3),
    ("Deloitte",             "deloitte",       "DeloitteCareers",   2),
    ("KPMG",                 "kpmg",           "campus",            5),
    ("EY",                   "ey",             "EY",                1),
    ("PwC",                  "pwc",            "Global",            1),
    ("Accenture",            "accenture",      "AccentureCareers",  1),
    ("IBM",                  "ibm",            "ibm",               1),
    ("Capgemini",            "capgemini",      "Capgemini",         3),
    ("Johnson & Johnson",    "jnj",            "JNJCareers",        1),
    ("AstraZeneca",          "astrazeneca",    "AstraZenecaGlobal", 4),
    ("GlaxoSmithKline",      "gsk",            "gsk",               1),
    ("Pfizer",               "pfizer",         "PfizerCareers",     1),
    ("Rolls-Royce",          "rollsroyce",     "RollsRoyce",        3),
    ("BAE Systems",          "baesystems",     "BAE",               1),
    ("Shell",                "shell",          "ShellExternal",     1),
    ("BP",                   "bp",             "External",          3),
    ("Amazon",               "amazon",         "en-US-Corporate",   1),
    ("Microsoft",            "microsoft",      "MicrosoftCareers",  1),
]

SEARCH_TERMS = ["graduate", "intern", "analyst", "associate", "junior"]


class WorkdayScraper(BaseScraper):
    source_id   = "workday"
    source_name = "Workday ATS"
    tier        = 2

    def scrape(self) -> Iterator[dict]:
        for company_name, sub, board, wd_n in WORKDAY_TARGETS:
            url = f"https://{sub}.wd{wd_n}.myworkdayjobs.com/wday/cxs/{sub}/{board}/jobs"
            for term in SEARCH_TERMS[:2]:  # limit to 2 terms per company
                try:
                    yield from self._fetch_jobs(url, company_name, term)
                except RequestError as e:
                    self.logger.warning(f"Workday [{company_name}] '{term}': {e}")
                    break
                except Exception as e:
                    self.logger.debug(f"Workday [{company_name}] error: {e}")
                    break

    def _fetch_jobs(self, url: str, company_name: str, term: str) -> Iterator[dict]:
        import gzip, urllib.request, urllib.error
        raw_body = json.dumps({
            "limit": 20, "offset": 0,
            "searchText": term,
            "appliedFacets": {}
        }).encode()
        body = gzip.compress(raw_body)
        self._throttle(url)
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Encoding": "gzip",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise RequestError(str(e))

        postings = data.get("jobPostings", [])
        self.logger.info(f"Workday [{company_name}] '{term}': {len(postings)} postings")

        seen = set()
        for p in postings:
            title = p.get("title", "").strip()
            if not title or title in seen:
                continue
            seen.add(title)

            ext_path = p.get("externalPath", "")
            location = p.get("locationsText", "")
            posted_raw = p.get("postedOn", "")
            posted_date = clean_date(posted_raw) if posted_raw else today_iso()

            # Build apply URL
            parsed = url.split("/wday/")[0]
            apply_url = f"{parsed}/en-US/{url.split('/wday/cxs/')[-1].split('/jobs')[0].split('/')[-1]}{ext_path}" if ext_path else ""

            yield make_job(
                company_name    = company_name,
                title           = title,
                source_id       = self.source_id,
                source_name     = self.source_name,
                url             = apply_url,
                industries      = infer_industries(title),
                seniority       = infer_seniority(title),
                employment_type = _infer_employment_type(title),
                region          = _infer_region(location),
                posted_date     = posted_date,
            )
