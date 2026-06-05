"""
inroad Backend — Jorb.ai Scraper

Primary: Supabase REST API (authenticated via stored refresh token).
         Returns every active London/New York grad-level job instantly,
         with no indexing lag.
Fallback: SSR category pages + sitemap if no token is available.

Token lifecycle: the JWT expires every hour. On each run the scraper
exchanges the stored refresh_token for a fresh JWT (rotating tokens),
then writes the new refresh_token back to the DB config table.
"""
import re
import json
import time
import shutil
import struct
import datetime
import logging
import urllib.request
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import (
    BaseScraper, make_job, infer_seniority, infer_industries,
    is_too_senior, RequestError,
)

logger = logging.getLogger(__name__)

# ── Structured-programme title filter ────────────────────────────────────────
# Jobs must contain at least one of these signals in the title to be kept.
# This mirrors the curation done by trackr / wttj (which only surface structured
# programmes) rather than any early-career job tagged by jorb.
_PROG_SUBSTRINGS: frozenset[str] = frozenset({
    # Internship (multi-word and "internship" safe as substring; "intern" alone
    # uses _PROG_WORD_RE to avoid "international"/"internet" false positives)
    "internship",
    # Summer programmes
    "summer intern", "summer analyst", "summer associate", "summer programme",
    "summer scheme", "summer placement", "summer",
    # Spring / Insight
    "spring week", "spring insight", "spring analyst", "spring associate",
    "insight programme", "insight experience", "insight week",
    # Off-cycle / Fixed-term / Seasonal
    "off-cycle", "off cycle", "offcycle",
    "seasonal",
    # Graduate / Rotational
    "graduate programme", "graduate scheme", "graduate analyst",
    "graduate rotational", "graduate trainee",
    "grad scheme", "grad programme", "graduate",
    "training contract", "vacation scheme",
    "rotational programme", "returnship",
    # Placement / Industrial year
    "industrial placement", "placement year", "year in industry",
    "sandwich year", "placement",
    # Apprenticeship
    "apprenticeship", "degree apprenticeship", "higher apprenticeship",
    # Fellows / Fellowship
    "fellows program", "fellows programme", "fellowship", "fellow",
    # Co-op / Student worker
    "co-op", "co op", "coop", "student worker", "student placement",
    # Sponsored degree / scheme / trainee / mentorship
    "sponsored degree", "scheme", "trainee", "mentorship",
    # Events
    "open day", "webinar", "networking", "hackathon", "bootcamp",
    "discovery day", "information session", "conference",
    "register your interest", "emerging talent network",
})

# Word-boundary regex for short tokens prone to substring false positives:
#   "intern"  → would match "international", "internet" without boundary
#   "event"   → would match "seventh" without boundary
_PROG_WORD_RE = re.compile(r'\bintern\b', re.IGNORECASE)


def _is_structured_programme(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in _PROG_SUBSTRINGS) or bool(_PROG_WORD_RE.search(t))


# ── Supabase constants (publishable — safe to commit) ────────────────────────
_SB_URL      = "https://optsvxrgzocfuyyrbkqd.supabase.co"
_SB_ANON_KEY = "sb_publishable_bJw9zTxyqsiE83gw8kV6Cw_tXyCXGLc"
_DB_CONFIG_KEY = "jorb_refresh_token"

# level_tags that map to grad/intern programme types
_GRAD_TAGS = [
    "graduate_entry_level", "early_career", "summer_internships",
    "off_cycle_internships", "insight_programmes",
    "industrial_placements",
]

_TAG_TO_PROGRAMME = {
    "summer_internships":    "Summer Internship",
    "insight_programmes":    "Spring Week",
    "off_cycle_internships": "Off-Cycle Internship",
    "industrial_placements": "Industrial Placement",
    "pre_university":        "Pre-Uni",
    "graduate_entry_level":  "Graduate Programme",
    "early_career":          "Graduate Programme",
}

_LOCATIONS = ["London", "New York"]

# Jorb category string → industry tags (used when title alone returns ["Other"])
_JORB_CAT_TO_INDUSTRY: dict[str, list[str]] = {
    "sales & trading":                                             ["Finance"],
    "esg & sustainability":                                        ["Finance"],
    "operations":                                                  ["Finance"],
    "generalist tech internships / programmes / graduate schemes": ["Technology"],
    "generalist tech internships":                                 ["Technology"],
    "research / applied science":                                  ["Technology"],
    "electrical engineering":                                      ["Technology"],
    "computer engineering":                                        ["Technology"],
    "internal transformation / pmo":                               ["Technology"],
}

# Company name slug → industry (last-resort for sitemap-categorised jobs)
_JORB_COMPANY_INDUSTRY: dict[str, list[str]] = {
    "bcg":      ["Consulting"],
    "ey":       ["Consulting"],
    "deloitte": ["Consulting"],
    "kpmg":     ["Consulting"],
    "pwc":      ["Consulting"],
}

# ── Logo matching ─────────────────────────────────────────────────────────────
_ROOT            = Path(__file__).parent.parent
_LOGOS_SRC       = _ROOT / "data"   / "logos"   # trackr logos (not web-served)
_LOGOS_STATIC    = _ROOT / "static" / "logos"   # web-served at /static/logos/

# Lazy index: built once on first call to _find_logo_url()
_LOGO_INDEX: dict[str, str] | None = None  # slug → abs path in data/logos


def _logo_index() -> dict[str, str]:
    global _LOGO_INDEX
    if _LOGO_INDEX is None:
        _LOGO_INDEX = {p.stem: str(p) for p in _LOGOS_SRC.glob("*.png")}
    return _LOGO_INDEX


def _slugify(name: str) -> str:
    """company name → filename-safe slug (matches trackr logo naming convention)."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _find_logo_url(company_name: str) -> str:
    """
    Return a web-accessible logo URL for company_name, or ''.
    Checks data/logos/ (trackr) and static/logos/ (wttj).
    Copies from data/logos/ to static/logos/ so it becomes web-accessible.
    """
    idx  = _logo_index()
    slug = _slugify(company_name)

    def _serve(stem: str, src_path: str) -> str:
        dst = _LOGOS_STATIC / f"{stem}.png"
        if not dst.exists():
            _LOGOS_STATIC.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)
        return f"/static/logos/{stem}.png"

    # 1. Exact slug match in data/logos/
    if slug in idx:
        return _serve(slug, idx[slug])

    # 2. Exact slug already in static/logos/ (wttj logos)
    if (_LOGOS_STATIC / f"{slug}.png").exists():
        return f"/static/logos/{slug}.png"

    # 3. Try stripping common company-name suffixes and re-matching
    for suffix in ("-group", "-capital", "-management", "-co", "-company",
                   "-plc", "-ltd", "-inc", "-bank", "-partners",
                   "-securities", "-asset-management", "-networks", "-markets"):
        if slug.endswith(suffix):
            short = slug[: -len(suffix)]
            if short in idx:
                return _serve(short, idx[short])
            if (_LOGOS_STATIC / f"{short}.png").exists():
                return f"/static/logos/{short}.png"

    # 4. Hyphen-insensitive match (handles "j-p-morgan" ↔ "jpmorgan" etc.)
    compact = slug.replace("-", "")
    for stem, path in idx.items():
        if stem.replace("-", "") == compact:
            return _serve(stem, path)

    # 5. Explicit aliases for companies whose canonical logo file uses a different name
    _ALIASES = {
        "lseg":          "london-stock-exchange-group",
        "natwest":       "natwest-markets",
        "natwest-group": "natwest-markets",
        "rbc":           "rbc-capital-markets",
        "td-bank":       "td-securities",
        "rothschild-co": "rothschild-co",
        "guggenheim":    "guggenheim-partners",
    }
    alias = _ALIASES.get(slug)
    if alias and alias in idx:
        return _serve(alias, idx[alias])

    return ""


# ── Company URL extraction ────────────────────────────────────────────────────
_ATS_DOMAINS = frozenset({
    "myworkdayjobs.com", "greenhouse.io", "lever.co",
    "fa.oraclecloud.com", "oraclecloud.com",
    "smartrecruiters.com", "icims.com", "taleo.net",
    "successfactors.com", "jobvite.com", "bamboohr.com",
    "brassring.com", "ultipro.com", "kenexa.com",
    "workable.com", "ashbyhq.com", "recruitee.com",
    "boards.greenhouse.io", "jobs.lever.co", "app.bamboohr.com",
    "wd1.myworkdayjobs.com", "wd3.myworkdayjobs.com",
    "wd103.myworkdayjobs.com", "wd5.myworkdayjobs.com",
})


def _careers_site(company_name: str, apply_url: str) -> str:
    """
    Return the company's own website/careers URL.
    Priority: existing DB entry → apply_url domain (if not an ATS) → ''.
    """
    # 1. Re-use careers_site already stored for this company in the DB
    try:
        from db.database import fetchone
        row = fetchone(
            "SELECT careers_site FROM jobs "
            "WHERE lower(company) = lower(?) AND careers_site IS NOT NULL AND careers_site != '' "
            "LIMIT 1",
            (company_name,),
        )
        if row and row.get("careers_site"):
            return row["careers_site"]
    except Exception:
        pass

    # 2. Extract from apply_url domain, skip known ATS providers
    if not apply_url:
        return ""
    try:
        host = urllib.parse.urlparse(apply_url).netloc.lower()
        for ats in _ATS_DOMAINS:
            if host == ats or host.endswith("." + ats):
                return ""
        return f"https://{host}"
    except Exception:
        return ""


# ── SSR fallback constants ───────────────────────────────────────────────────
BASE_URL    = "https://www.jorb.ai"
SITEMAP_URL = "https://www.jorb.ai/sitemap.xml"

CATEGORY_PAGES: list[tuple[str, list[str], bool]] = [
    ("/jobs/generalist-internships-programmes-graduate-schemes",
     ["Finance", "Consulting"],                              True),
    ("/jobs/generalist-tech-internships-programmes-graduate-schemes",
     ["Technology", "Software Engineering"],                 True),
    ("/jobs/investment-banking",
     ["Investment Banking", "Finance"],                      False),
    ("/jobs/sales-trading",
     ["Finance", "Investment Banking"],                      False),
    ("/jobs/asset-management",
     ["Finance"],                                            False),
    ("/jobs/hedge-fund-quant",
     ["Finance"],                                            False),
    ("/jobs/private-market",
     ["Finance"],                                            False),
    ("/jobs/retail-banking",
     ["Finance"],                                            False),
    ("/jobs/risk-compliance",
     ["Finance", "Law"],                                     False),
    ("/jobs/accounting-finance",
     ["Finance"],                                            False),
    ("/jobs/corporate-commercial-institutional-banking",
     ["Finance"],                                            False),
    ("/jobs/insurance-actuarial",
     ["Finance"],                                            False),
    ("/jobs/research",
     ["Finance"],                                            False),
    ("/jobs/backend",
     ["Software Engineering", "Technology"],                 False),
    ("/jobs/frontend",
     ["Software Engineering", "Technology"],                 False),
    ("/jobs/full-stack",
     ["Software Engineering", "Technology"],                 False),
    ("/jobs/machine-learning-engineering",
     ["Software Engineering", "Technology"],                 False),
    ("/jobs/data-engineering",
     ["Data & Analytics", "Technology"],                     False),
    ("/jobs/analytics-bi",
     ["Data & Analytics"],                                   False),
    ("/jobs/product-management",
     ["Product Management", "Technology"],                   False),
    ("/jobs/research-applied-science",
     ["Technology", "Software Engineering"],                 False),
    ("/jobs/platform-engineering",
     ["Software Engineering", "Technology"],                 False),
    ("/jobs/site-reliability-engineering-sre",
     ["Software Engineering", "Technology"],                 False),
    ("/jobs/technology",
     ["Technology"],                                         False),
    ("/jobs/strategy-consulting",
     ["Consulting", "Strategy"],                             False),
    ("/jobs/operations",
     ["Finance"],                                            False),
    ("/jobs/sales-marketing",
     ["Marketing"],                                          False),
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_CARD_RE = re.compile(
    r'<a href="(/jobs/([a-f0-9]{24}))"[^>]*>(.*?)</a>'
    r'.*?'
    r'<a href="/firms/([^"]+)"[^>]*>(.*?)</a>\s*·\s*<!--\s*-->(.*?)<!--\s*-->',
    re.DOTALL,
)

_LOCATION_MAP = {
    "london":         "UK",
    "united kingdom": "UK",
    "new york":       "US",
    "singapore":      "Asia",
    "hong kong":      "Asia",
}

_EXCLUDED_LOCATIONS = {"singapore", "hong kong"}

_TITLE_RE  = re.compile(r'<title>([^<]+)</title>')
_LDJSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# First external apply link on a jorb.ai job page (not jorb.ai itself)
_APPLY_LINK_RE = re.compile(
    r'href="(https?://(?!(?:www\.)?jorb\.ai(?:/|$))[^"]{15,})"[^>]*target="_blank"',
    re.IGNORECASE,
)


def _extract_direct_apply_url(html: str) -> str:
    """Return the first external apply URL from a jorb.ai job page, or ''."""
    m = _APPLY_LINK_RE.search(html)
    return m.group(1) if m else ""


def _resolve_apply_url(jorb_url: str, fetched_html: str | None = None) -> str:
    """
    Return the direct company apply URL for a jorb.ai job page.
    If html is already fetched, use it; otherwise fetch the page.
    Falls back to jorb_url only when extraction truly fails.
    """
    if fetched_html is not None:
        direct = _extract_direct_apply_url(fetched_html)
        return direct if direct else jorb_url
    try:
        time.sleep(0.15)
        req  = urllib.request.Request(jorb_url, headers=_HEADERS)
        html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="replace")
        direct = _extract_direct_apply_url(html)
        return direct if direct else jorb_url
    except Exception:
        return jorb_url


# ── Helpers ──────────────────────────────────────────────────────────────────

def _objectid_to_dt(oid: str) -> datetime.datetime:
    ts = struct.unpack(">I", bytes.fromhex(oid[:8]))[0]
    return datetime.datetime.utcfromtimestamp(ts)


def _clean(html_str: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html_str)).strip()


def _infer_region(location: str) -> str:
    loc = location.lower()
    for key, region in _LOCATION_MAP.items():
        if key in loc:
            return region
    return "UK"


def _infer_programme_type(title: str, is_programme_page: bool) -> str:
    t = title.lower()
    if any(k in t for k in ["spring week", "spring insight", "spring into", "spring programme"]):
        return "Spring Week"
    if any(k in t for k in ["insight programme", "insight experience", "insight event", "insight week"]):
        return "Spring Week"
    if any(k in t for k in ["off-cycle", "off cycle", "offcycle", "fixed term", "ftc", "fixed-term"]):
        return "Off-Cycle Internship"
    if any(k in t for k in ["industrial placement", "placement year", "year in industry", "sandwich year", "year placement"]):
        return "Industrial Placement"
    if any(k in t for k in ["pre-uni", "pre uni", "school leaver", "year 12", "year 13", "sixth form", "work experience", "work placement", "pioneer"]):
        return "Pre-Uni"
    if any(k in t for k in [
        "open day", "webinar", "networking", "register your interest",
        "emerging talent network", "discovery day", "information session",
        "conference", "bootcamp", "hackathon",
    ]):
        return "Events"
    if any(k in t for k in ["apprentice", "apprenticeship", "degree apprenticeship", "higher apprenticeship"]):
        return "Apprenticeship"
    if any(k in t for k in ["summer analyst", "summer associate", "summer intern"]):
        return "Summer Internship"
    # Word-boundary match for "intern" to avoid "international", "internet", etc.
    if re.search(r'\bintern(ship)?\b', t):
        return "Summer Internship"
    if any(k in t for k in ["graduate programme", "grad scheme", "graduate scheme", "grad programme",
                              "training contract", "returnship", "rotational programme", "graduate rotational",
                              "graduate analyst", "veterans", "athletes programme"]):
        return "Graduate Programme"
    if "graduate" in t or ("grad" in t and "internship" not in t):
        return "Graduate Programme"
    return "Summer Internship" if is_programme_page else "Graduate Programme"


def _programme_type_from_tags(level_tags: list[str]) -> str:
    """Map Supabase level_tags to inroad programme_type (first matching tag wins)."""
    for tag in level_tags:
        if tag in _TAG_TO_PROGRAMME:
            return _TAG_TO_PROGRAMME[tag]
    return "Graduate Programme"


def _extract_recent_oids(sitemap_xml: str, since_dt: datetime.datetime) -> list[str]:
    oid_re = re.compile(r'/jobs/([a-f0-9]{24})')
    result = []
    for oid in oid_re.findall(sitemap_xml):
        try:
            if _objectid_to_dt(oid) >= since_dt:
                result.append(oid)
        except Exception:
            pass
    return result


def _parse_job_page(html: str) -> tuple[str, str, str] | None:
    for raw in _LDJSON_RE.findall(html):
        try:
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                    continue
                title   = item.get("title", "").strip()
                company = (item.get("hiringOrganization") or {}).get("name", "").strip()
                loc_obj = item.get("jobLocation") or {}
                if isinstance(loc_obj, list):
                    loc_obj = loc_obj[0] if loc_obj else {}
                addr = (loc_obj.get("address") or {}) if isinstance(loc_obj, dict) else {}
                location = (
                    (addr.get("addressLocality") or addr.get("addressRegion") or "").strip()
                    if isinstance(addr, dict) else str(addr).strip()
                )
                if title and company:
                    return title, company, location
        except Exception:
            pass

    m = _TITLE_RE.search(html)
    if not m:
        return None
    raw_title = unescape(m.group(1))
    parts = re.split(r"\s+[—–-]\s+", raw_title)
    if len(parts) < 2:
        return None
    role_company = parts[0]
    location     = parts[1]
    at_idx       = role_company.rfind(" at ")
    if at_idx == -1:
        return None
    title   = role_company[:at_idx].strip()
    company = role_company[at_idx + 4:].strip()
    return title, company, location


# ── Scraper ──────────────────────────────────────────────────────────────────

class JorbScraper(BaseScraper):
    source_id   = "jorb"
    source_name = "Jorb.ai"
    tier        = 2

    def __init__(
        self,
        cutoff_hours: int | None = None,
        sitemap_days_back: int | None = None,
    ):
        super().__init__()
        self.cutoff: datetime.datetime | None = (
            datetime.datetime.utcnow() - datetime.timedelta(hours=cutoff_hours)
            if cutoff_hours else None
        )
        self.sitemap_days_back = sitemap_days_back

    # ── Token management ─────────────────────────────────────────────────────

    def _sb_post(self, path: str, body: dict) -> dict:
        """POST to a Supabase auth endpoint and return the parsed JSON."""
        req = urllib.request.Request(
            f"{_SB_URL}{path}",
            data=json.dumps(body).encode(),
            headers={"apikey": _SB_ANON_KEY, "Content-Type": "application/json"},
            method="POST",
        )
        return json.loads(urllib.request.urlopen(req, timeout=15).read())

    def _get_jwt(self) -> str | None:
        """
        Return a fresh Supabase JWT.

        Strategy:
          1. Exchange the stored rotating refresh_token (fast, no credentials).
          2. If that returns 400 (token rotated/expired), fall back to
             password re-auth using JORB_EMAIL / JORB_PASSWORD (env vars take
             priority, then DB config keys).  Stores the new refresh_token so
             subsequent runs use strategy 1 again automatically.
          3. If neither works, return None and the caller falls back to SSR.
        """
        import os
        from db.database import get_config, set_config

        def _save_and_return(data: dict) -> str | None:
            new_refresh = data.get("refresh_token")
            if new_refresh:
                set_config(_DB_CONFIG_KEY, new_refresh)
            return data.get("access_token")

        # ── Strategy 1: rotate existing refresh token ────────────────────────
        refresh_token = get_config(_DB_CONFIG_KEY)
        if refresh_token:
            try:
                data = self._sb_post(
                    "/auth/v1/token?grant_type=refresh_token",
                    {"refresh_token": refresh_token},
                )
                return _save_and_return(data)
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    self.logger.warning(f"Jorb: token refresh error {e.code}: {e}")
                    return None
                self.logger.info("Jorb: refresh token stale, re-authenticating with password")
            except Exception as e:
                self.logger.warning(f"Jorb: token refresh failed: {e}")
                return None

        # ── Strategy 2: password re-auth ─────────────────────────────────────
        email    = os.environ.get("JORB_EMAIL")    or get_config("jorb_email")
        password = os.environ.get("JORB_PASSWORD") or get_config("jorb_password")
        if not email or not password:
            self.logger.warning(
                "Jorb: refresh token stale and no credentials stored. "
                "Set JORB_EMAIL + JORB_PASSWORD env vars (or jorb_email / "
                "jorb_password in the config table) to enable auto re-auth."
            )
            return None
        try:
            data = self._sb_post(
                "/auth/v1/token?grant_type=password",
                {"email": email, "password": password},
            )
            self.logger.info("Jorb: password re-auth succeeded, refresh token updated")
            return _save_and_return(data)
        except Exception as e:
            self.logger.warning(f"Jorb: password re-auth failed: {e}")
            return None

    # ── Public entry point ───────────────────────────────────────────────────

    def scrape(self) -> Iterator[dict]:
        jwt = self._get_jwt()
        if jwt:
            self.logger.info("Jorb: using Supabase API (authenticated)")
            yield from self._scrape_api(jwt)
        else:
            self.logger.info("Jorb: no token — falling back to SSR scraper")
            yield from self._scrape_ssr()

    # ── API path ─────────────────────────────────────────────────────────────

    def _scrape_api(self, jwt: str) -> Iterator[dict]:
        """Query Supabase REST API for all grad-level London/New York jobs."""
        since = self.cutoff or (
            datetime.datetime.utcnow() - datetime.timedelta(days=30)
        )
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        tags_filter = "{" + ",".join(_GRAD_TAGS) + "}"
        locs_filter = "(" + ",".join(loc.replace(" ", "%20") for loc in _LOCATIONS) + ")"

        base_params = (
            f"status=eq.active"
            f"&location=in.{locs_filter}"
            f"&level_tags=ov.{tags_filter}"
            f"&posted_date=gte.{since_str}"
            f"&order=posted_date.desc"
            f"&select=id,company,role,location,posted_date,level_tags,category,industry,link"
        )

        headers = {
            "apikey":        _SB_ANON_KEY,
            "Authorization": f"Bearer {jwt}",
            "Prefer":        "count=exact",
        }

        offset       = 0
        total        = None
        seen         : set[str] = set()
        pending_rows : list[dict] = []

        while True:
            url = f"{_SB_URL}/rest/v1/jobs?{base_params}&limit=1000&offset={offset}"
            try:
                req  = urllib.request.Request(url, headers=headers)
                resp = urllib.request.urlopen(req, timeout=20)
                rows = json.loads(resp.read())

                if total is None:
                    cr = resp.headers.get("Content-Range", "")
                    m  = re.search(r"/(\d+)$", cr)
                    total = int(m.group(1)) if m else None
                    self.logger.info(f"Jorb API: {total} matching jobs")
            except urllib.error.HTTPError as e:
                self.logger.warning(f"Jorb API error {e.code}: {e.read().decode()[:200]}")
                return
            except Exception as e:
                self.logger.warning(f"Jorb API error: {e}")
                return

            if not rows:
                break

            for row in rows:
                oid = row.get("id", "")
                if oid in seen:
                    continue
                seen.add(oid)
                pending_rows.append(row)

            offset += len(rows)
            if total is not None and offset >= total:
                break

        # Batch-fetch jorb.ai job pages to resolve direct company apply URLs
        self.logger.info(f"Jorb API: fetching apply URLs for {len(pending_rows)} jobs")
        direct_urls: dict[str, str] = {}
        for i, row in enumerate(pending_rows):
            oid = row.get("id", "")
            jorb_url = f"{BASE_URL}/jobs/{oid}"
            try:
                time.sleep(0.15)
                page_html = self.fetch(jorb_url, headers=_HEADERS).decode("utf-8", errors="replace")
                direct = _extract_direct_apply_url(page_html)
                direct_urls[oid] = direct if direct else jorb_url
            except Exception:
                direct_urls[oid] = jorb_url
            if (i + 1) % 20 == 0:
                self.logger.info(f"  [{i+1}/{len(pending_rows)}] apply URLs fetched")

        for row in pending_rows:
            job = self._api_row_to_job(row, direct_url=direct_urls.get(row.get("id", ""), ""))
            if job:
                yield job

        self.logger.info(f"Jorb API: {len(pending_rows)} jobs yielded")

    def _api_row_to_job(self, row: dict, direct_url: str = "") -> dict | None:
        oid      = row.get("id", "")
        title    = (row.get("role") or "").strip()
        company  = (row.get("company") or "").strip()
        location = (row.get("location") or "").strip()

        if not title or not company:
            return None
        if any(excl in location.lower() for excl in _EXCLUDED_LOCATIONS):
            return None
        if is_too_senior(title):
            return None

        level_tags     = row.get("level_tags") or []
        programme_type = _programme_type_from_tags(level_tags)
        # Title-based override for more specific types
        title_pt = _infer_programme_type(title, is_programme_page=False)
        if title_pt not in ("Graduate Programme",):
            programme_type = title_pt

        # Drop pre-uni and any job whose title lacks an explicit structured-
        # programme signal. This mirrors trackr/wttj curation: keep only
        # interns, summer/seasonal programmes, grad schemes, fellowships,
        # off-cycle / fixed-term contracts, events, etc. Regular entry-level
        # jobs (tellers, receptionists, junior brokers) are excluded even if
        # jorb tags them early_career / graduate_entry_level.
        if programme_type == "Pre-Uni":
            return None
        if not _is_structured_programme(title):
            return None

        region     = _infer_region(location)
        industries = infer_industries(title)
        jorb_cat   = row.get("category") or row.get("industry") or ""
        if industries == ["Other"] and jorb_cat:
            cat_key    = jorb_cat.lower().strip()
            industries = (
                _JORB_CAT_TO_INDUSTRY.get(cat_key)
                or infer_industries(jorb_cat)
                or industries
            )
        if industries == ["Other"]:
            co_slug    = _slugify(company)
            industries = _JORB_COMPANY_INDUSTRY.get(co_slug) or industries

        posted_raw  = (row.get("posted_date") or "")[:10]
        posted_date = posted_raw or datetime.datetime.utcnow().strftime("%Y-%m-%d")

        jorb_url   = f"{BASE_URL}/jobs/{oid}"
        apply_url  = direct_url if direct_url and not direct_url.startswith(BASE_URL) else (row.get("link") or jorb_url).strip()
        logo_url   = _find_logo_url(company)
        co_url     = _careers_site(company, apply_url)

        job = make_job(
            company_name    = company,
            title           = title,
            source_id       = self.source_id,
            source_name     = self.source_name,
            url             = apply_url,
            industries      = industries,
            seniority       = infer_seniority(title),
            employment_type = (
                "internship" if programme_type in (
                    "Summer Internship", "Spring Week", "Off-Cycle Internship",
                    "Industrial Placement", "Pre-Uni", "Events",
                ) else "full-time"
            ),
            region          = region,
            posted_date     = posted_date,
        )
        job["opening_date"]   = posted_date
        job["programme_type"] = programme_type
        job["location"]       = location
        job["jorb_category"]  = jorb_cat
        job["apply_url"]      = apply_url
        job["logo_url"]       = logo_url
        job["company_url"]    = co_url
        job["careers_site"]   = co_url
        return job

    # ── SSR fallback path ────────────────────────────────────────────────────

    def _scrape_ssr(self) -> Iterator[dict]:
        seen: set = set()

        for path, hint_industries, is_programme_page in CATEGORY_PAGES:
            count    = 0
            seen_urls: set = set()

            while True:
                url = f"{BASE_URL}{path}"
                try:
                    raw_bytes = self.fetch(url, headers=_HEADERS)
                    html = raw_bytes.decode("utf-8", errors="replace")
                except RequestError as e:
                    self.logger.warning(f"Jorb SSR: failed to fetch {url}: {e}")
                    break
                except Exception as e:
                    self.logger.warning(f"Jorb SSR: error fetching {url}: {e}")
                    break

                page_jobs = list(self._parse_page(html, path, hint_industries, is_programme_page))
                page_urls = {j.get("url") for j in page_jobs}
                if not page_jobs or page_urls.issubset(seen_urls):
                    break
                seen_urls |= page_urls

                for job in page_jobs:
                    jorb_url = job.get("url", "")
                    key = jorb_url or f"{job['company_name']}|{job['title']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    count += 1
                    # Resolve to direct company URL (SSR cards only have jorb.ai links)
                    direct = _resolve_apply_url(jorb_url)
                    if direct != jorb_url:
                        job["url"] = direct
                        job["apply_url"] = direct
                    yield job

                if len(page_jobs) < 100:
                    break

            self.logger.info(f"Jorb SSR [{path}]: {count} jobs")

        if self.sitemap_days_back:
            extra = 0
            for job in self._scrape_sitemap_recent(seen):
                key = job.get("url") or f"{job['company_name']}|{job['title']}"
                seen.add(key)
                extra += 1
                yield job
            self.logger.info(f"Jorb SSR [sitemap/{self.sitemap_days_back}d]: {extra} additional jobs")

    def _parse_page(
        self, html: str, page_path: str, hint_industries: list[str], is_programme_page: bool
    ) -> Iterator[dict]:
        for job_path, oid, title_raw, _firm_slug, company_raw, loc_raw in _CARD_RE.findall(html):
            try:
                posted_dt = _objectid_to_dt(oid)
            except Exception:
                continue

            if self.cutoff and posted_dt < self.cutoff:
                continue

            title   = _clean(title_raw)
            company = _clean(company_raw)
            loc     = _clean(loc_raw).strip().strip("·").strip()

            if not title or not company:
                continue
            if any(excl in loc.lower() for excl in _EXCLUDED_LOCATIONS):
                continue
            if not is_programme_page and is_too_senior(title):
                continue
            if not _is_structured_programme(title):
                continue

            programme_type = _infer_programme_type(title, is_programme_page)
            region         = _infer_region(loc)
            industries     = infer_industries(title) or hint_industries[:2]
            if industries == ["Other"]:
                industries = hint_industries[:2]
            posted_date    = posted_dt.strftime("%Y-%m-%d")

            job = make_job(
                company_name    = company,
                title           = title,
                source_id       = self.source_id,
                source_name     = self.source_name,
                url             = f"{BASE_URL}{job_path}",
                industries      = industries,
                seniority       = infer_seniority(title),
                employment_type = (
                    "internship" if programme_type in ("Summer Internship", "Spring Week",
                                                        "Off-Cycle Internship", "Industrial Placement",
                                                        "Pre-Uni", "Events")
                    else "full-time"
                ),
                region          = region,
                posted_date     = posted_date,
            )
            job["opening_date"]   = posted_date
            job["programme_type"] = programme_type
            job["location"]       = loc
            job["jorb_category"]  = page_path
            yield job

    def _scrape_sitemap_recent(self, already_seen: set) -> Iterator[dict]:
        since_dt = datetime.datetime.utcnow() - datetime.timedelta(days=self.sitemap_days_back)
        try:
            sitemap_bytes = self.fetch(SITEMAP_URL, headers=_HEADERS)
            sitemap_xml   = sitemap_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            self.logger.warning(f"Jorb sitemap: failed to fetch: {e}")
            return

        oids = _extract_recent_oids(sitemap_xml, since_dt)
        self.logger.info(f"Jorb sitemap: {len(oids)} OIDs in last {self.sitemap_days_back}d")

        for oid in oids:
            job_url = f"{BASE_URL}/jobs/{oid}"
            if job_url in already_seen:
                continue
            try:
                time.sleep(0.25)
                page_html = self.fetch(job_url, headers=_HEADERS).decode("utf-8", errors="replace")
            except Exception as e:
                self.logger.debug(f"Jorb sitemap: skip {oid}: {e}")
                continue

            parsed = _parse_job_page(page_html)
            if not parsed:
                continue
            title, company, location = parsed

            if not title or not company:
                continue
            if any(excl in location.lower() for excl in _EXCLUDED_LOCATIONS):
                continue
            if is_too_senior(title):
                continue
            if not _is_structured_programme(title):
                continue

            posted_dt      = _objectid_to_dt(oid)
            posted_date    = posted_dt.strftime("%Y-%m-%d")
            programme_type = _infer_programme_type(title, is_programme_page=False)
            region         = _infer_region(location)
            industries     = infer_industries(title)

            apply_url = _resolve_apply_url(job_url, fetched_html=page_html)

            job = make_job(
                company_name    = company,
                title           = title,
                source_id       = self.source_id,
                source_name     = self.source_name,
                url             = apply_url,
                industries      = industries,
                seniority       = infer_seniority(title),
                employment_type = (
                    "internship" if programme_type in (
                        "Summer Internship", "Spring Week", "Off-Cycle Internship",
                        "Industrial Placement", "Pre-Uni", "Events",
                    ) else "full-time"
                ),
                region          = region,
                posted_date     = posted_date,
            )
            job["opening_date"]   = posted_date
            job["programme_type"] = programme_type
            job["location"]       = location
            job["jorb_category"]  = "sitemap"
            yield job
