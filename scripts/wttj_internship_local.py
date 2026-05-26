"""
scripts/wttj_internship_local.py

Scrapes Welcome to the Jungle internship listings using their public XML sitemaps
and public organizations API. No auth required, no DB writes, local only.

Output: data/wttj_internships_<date>.csv

Run from the project root:
    python scripts/wttj_internship_local.py
"""

import csv
import gzip
import json
import os
import random
import re
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ── config ────────────────────────────────────────────────────────────────────

SITEMAP_BASE = "https://www.welcometothejungle.com/sitemaps/job-listings.{n}.xml.gz"
SITEMAP_SHARDS = 12
ORG_API_BASE   = "https://api.welcometothejungle.com/api/v1/organizations"

CUTOFF_HOURS = 72

INTERN_PATTERNS = re.compile(
    r"\b(intern|internship|placement|graduate|grad|spring-week|summer-analyst"
    r"|trainee|apprenti|alternance|stage-)\b",
    re.IGNORECASE,
)

EN_URL_PREFIX = "https://www.welcometothejungle.com/en/companies/"

OUTPUT_PATH = ROOT / "data" / f"wttj_internships_{datetime.now().strftime('%Y-%m-%d')}.csv"

CSV_COLS = [
    "company_name", "title", "sector", "region",
    "location", "country", "date_posted", "programme_type", "company_size",
    "logo_url", "job_url", "wttj_url", "company_url", "recruitment_process",
]


# ── curl helper (shell invocation bypasses Cloudflare TLS fingerprinting) ────

def _curl_to_file(url: str, tmp: str, timeout: int = 30) -> bool:
    """Fetch url to tmp file via os.system curl. Returns True on success."""
    ret = os.system(f'curl -s --max-time {timeout} -o "{tmp}" "{url}"')
    return ret == 0


def fetch_shard(n: int) -> bytes:
    url = SITEMAP_BASE.format(n=n)
    tmp = tempfile.mktemp(suffix=".xml.gz")
    try:
        if not _curl_to_file(url, tmp):
            raise RuntimeError("curl failed")
        with open(tmp, "rb") as f:
            raw = f.read()
        if not raw:
            raise RuntimeError("empty response")
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        if raw[:5] != b"<?xml":
            raise RuntimeError(f"unexpected content: {raw[:30]!r}")
        return raw
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fetch_json_url(url: str, timeout: int = 10) -> dict:
    """Fetch any JSON URL via curl. Returns {} on failure."""
    tmp = tempfile.mktemp(suffix=".json")
    try:
        if not _curl_to_file(url, tmp, timeout=timeout):
            return {}
        with open(tmp, "rb") as f:
            raw = f.read()
        if not raw or raw[:1] != b"{":
            return {}
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {}
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fetch_org(slug: str) -> dict:
    """Fetch company JSON from WTTJ public organizations API."""
    return fetch_json_url(f"{ORG_API_BASE}/{slug}")


# ── URL parser ────────────────────────────────────────────────────────────────

JOB_URL_RE = re.compile(
    r"https://www\.welcometothejungle\.com/en/companies/"
    r"(?P<company_slug>[^/]+)/jobs/"
    r"(?P<rest>[^\"< ]+)"
)


def _slug_to_label(slug: str) -> str:
    slug = re.sub(r"_[A-Z0-9]{4,10}$", "", slug)
    return slug.replace("-", " ").replace("_", " ").title().strip()


COUNTRY_CODE_MAP = {
    "us": "US", "uk": "UK", "gb": "UK",
    "fr": "France", "de": "Germany", "nl": "Netherlands",
    "es": "Spain", "be": "Belgium", "ch": "Switzerland",
    "it": "Italy", "pt": "Portugal", "se": "Sweden",
    "at": "Austria", "dk": "Denmark", "pl": "Poland",
    "ie": "Ireland", "no": "Norway", "fi": "Finland",
    "sg": "Singapore", "ae": "UAE", "in": "India",
    "ca": "Canada", "au": "Australia",
}
CITY_COUNTRY_MAP = {
    "london": "UK", "manchester": "UK", "edinburgh": "UK", "bristol": "UK",
    "new-york": "US", "san-francisco": "US", "los-angeles": "US",
    "chicago": "US", "boston": "US", "seattle": "US", "austin": "US",
    "miami": "US", "denver": "US", "atlanta": "US", "houston": "US",
    "palo-alto": "US", "mountain-view": "US", "menlo-park": "US",
    "fremont": "US", "santa-clara": "US", "new-haven": "US",
    "paris": "France", "lyon": "France", "bordeaux": "France",
    "toulouse": "France", "nantes": "France", "marseille": "France",
    "pantin": "France", "puteaux": "France", "boulogne": "France",
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany",
    "frankfurt": "Germany", "dusseldorf": "Germany",
    "amsterdam": "Netherlands", "rotterdam": "Netherlands",
    "barcelona": "Spain", "madrid": "Spain",
    "milan": "Italy", "rome": "Italy",
    "zurich": "Switzerland", "geneva": "Switzerland",
    "brussels": "Belgium", "antwerp": "Belgium",
    "stockholm": "Sweden", "copenhagen": "Denmark",
    "dublin": "Ireland",
    "singapore": "Singapore", "dubai": "UAE",
    "toronto": "Canada", "vancouver": "Canada",
    "sydney": "Australia", "melbourne": "Australia",
}
EU_COUNTRIES = {
    "France", "Germany", "Netherlands", "Spain", "Belgium", "Italy",
    "Switzerland", "Portugal", "Sweden", "Austria", "Denmark", "Poland",
    "Ireland", "Norway", "Finland",
}


def _infer_location(parts: list[str]) -> tuple[str, str, str]:
    """Return (location_raw, location_label, country_label)."""
    if len(parts) < 2:
        return "", "", ""
    raw_seg   = parts[1]
    candidate = raw_seg.lower()
    is_company_code = bool(re.search(r"[A-Z]", raw_seg))
    is_hash         = bool(re.search(r"\d", candidate)) and len(candidate) >= 6
    if is_company_code or is_hash:
        return "", "", ""
    location_raw   = candidate
    location_label = location_raw.replace("-", " ").title()
    country_label  = COUNTRY_CODE_MAP.get(location_raw, "")
    if not country_label:
        for city, ctry in CITY_COUNTRY_MAP.items():
            if city in location_raw:
                country_label = ctry
                break
    if not country_label and (location_raw.endswith("-ca") or location_raw.endswith("-ny")
                               or location_raw.endswith("-tx") or location_raw.endswith("-wa")
                               or location_raw.endswith("-ma") or location_raw.endswith("-co")):
        country_label = "US"
    return location_raw, location_label, country_label


def _parse_url(url: str) -> dict | None:
    m = JOB_URL_RE.match(url)
    if not m:
        return None
    company_slug = m.group("company_slug")
    rest         = m.group("rest")
    parts        = rest.split("_")
    title_slug   = parts[0]
    _, location_label, country_label = _infer_location(parts)
    return {
        "company_slug": company_slug,
        "company_name": _slug_to_label(company_slug),
        "title":        _slug_to_label(title_slug),
        "location":     location_label,
        "country":      country_label,
        "job_url":      url,
    }


# ── sector classifier ─────────────────────────────────────────────────────────
#
# Primary signal: WTTJ company sector tags from the organizations API.
# Fallback: job-title keyword matching (used when API sectors are absent or
# unrecognised, e.g. "Luxury", "Retail").
#
# Priority when multiple labels conflict: Law > Consulting > Finance > Tech > Other

# Maps WTTJ sector/parent names → our four labels (used as last-resort fallback only)
WTTJ_SECTOR_MAP: dict[str, str] = {
    # Finance
    "Banking": "Finance", "Finance": "Finance", "Insurance": "Finance",
    "Audit": "Finance", "Asset Management": "Finance", "Investment": "Finance",
    "Private Equity": "Finance", "Venture Capital": "Finance",
    "Wealth Management": "Finance", "Treasury": "Finance",
    "Banking / Insurance / Finance": "Finance",
    "FinTech / InsurTech": "Finance",
    "Accounting / Audit": "Finance",
    # Consulting
    "Strategy": "Consulting", "Organization / Management": "Consulting",
    "Management Consulting": "Consulting", "Consulting": "Consulting",
    "Consulting / Audit": "Consulting",
    # Marketing
    "Marketing / Communications": "Marketing",
    "Advertising / Marketing / Agency": "Marketing",
    "Advertising": "Marketing",
    # Tech — core
    "Software": "Tech", "Artificial Intelligence / Machine Learning": "Tech",
    "Big Data": "Tech", "Information Technology": "Tech",
    "Cybersecurity / Security": "Tech", "Cyber Security": "Tech",
    "Cloud": "Tech", "SaaS / Cloud Services": "Tech",
    "Digital Marketing / Data Marketing": "Tech",
    "Internet / E-commerce": "Tech", "Hardware": "Tech",
    "Telecommunications": "Tech", "Tech": "Tech",
    # Tech — extended
    "IT / Digital": "Tech", "Mobile Apps": "Tech", "Digital": "Tech",
    "SocialTech / GreenTech": "Tech", "EdTech": "Tech",
    "AdTech / MarTech": "Tech", "Blockchain": "Tech",
    "Robotics": "Tech", "Electronics / Telecommunications": "Tech",
    "E-commerce": "Tech", "FoodTech": "Tech",
    # Law
    "Legal": "Law", "Law": "Law", "Compliance": "Law", "Regulatory": "Law",
}

SECTOR_PRIORITY = ["Law", "Finance", "Consulting", "Marketing", "Tech"]

# HR/TA keywords — jobs matching these are removed from results entirely.
HR_TA_EXCLUDE_KEYWORDS = {
    "hr ", " hr,", "human resource", "talent acquisition", "talent management",
    "recruiting", "recruiter", "people operations", "people & culture",
}

# Support-function keywords — titles matching these are classified Other
# (but kept in results, unlike HR/TA above).
SUPPORT_FUNCTION_KEYWORDS = {
    "sales intern", "sales development", "account executive", "account manager",
    "business development", "customer success", "customer experience",
    "customer support", "customer service",
    "graphic design", "visual design", "creative design",
    "supply chain", "procurement",
}

# Title-keyword rules: Law > Finance > Consulting > Marketing > Tech
TITLE_SECTOR_RULES: list[tuple[str, list[str]]] = [
    ("Law", [
        "law", "legal", "compliance", "paralegal", "regulatory", "solicitor",
        "barrister", "counsel", "juridique", "droit",
    ]),
    ("Finance", [
        "finance", "financial", "banking", "investment", "asset management",
        "hedge fund", "quant", "accounting", "treasury", "equity", "credit",
        "portfolio", "capital markets", "private equity", "wealth management",
        "insurance", "actuari", "risk analyst", "structuring",
        "mergers", "m&a",
    ]),
    ("Consulting", [
        "consult", "advisory", "management consulting", "strategy consulting",
        "business analyst", "management analyst",
    ]),
    ("Marketing", [
        "marketing", "brand", "social media", " communication",
        " pr ", "public relation", "seo", "content marketing", "content strategy",
    ]),
    ("Tech", [
        "software", "engineer", "developer", "data scientist", "data engineer",
        "data analyst", "product manager", "product design",
        "machine learning", "artificial intelligence", " ai ",
        "devops", "sre", "backend", "frontend", "fullstack", "full stack",
        "mobile", "cybersecurity", "cloud", "infrastructure", "platform",
        "ui ", "ux ", "information technology", "saas", "technical",
        "programmer", "scientist intern", "science intern",
    ]),
]


def _is_hr_ta(title: str) -> bool:
    t = " " + title.lower() + " "
    return any(k in t for k in HR_TA_EXCLUDE_KEYWORDS)


def classify_sector(title: str, wttj_sectors: list[str] | None = None) -> str:
    t = " " + title.lower() + " "

    # 1. Support-function check locks in Other regardless of company sector.
    if any(k in t for k in SUPPORT_FUNCTION_KEYWORDS):
        return "Other"

    # 2. Title keywords — Law > Finance > Consulting > Marketing > Tech.
    for sector, keywords in TITLE_SECTOR_RULES:
        if any(k in t for k in keywords):
            return sector

    # 3. Company sector as last resort (generic titles with no functional signal).
    if wttj_sectors:
        found: set[str] = set()
        for name in wttj_sectors:
            label = WTTJ_SECTOR_MAP.get(name)
            if label:
                found.add(label)
        for priority_label in SECTOR_PRIORITY:
            if priority_label in found:
                return priority_label

    return "Other"


# ── region classifier ─────────────────────────────────────────────────────────

def classify_region(country: str) -> str:
    if country == "US":
        return "US"
    if country == "UK":
        return "UK"
    if country in EU_COUNTRIES:
        return "EU"
    return ""


# ── programme type classifier ─────────────────────────────────────────────────
#
# Priority: Spring Week > Graduate Programme > Summer Internship >
#           Industrial Placement > Off-Cycle Internship

_SPRING_WEEK_KW = {
    "spring week", "spring insight", "insight week", "insight programme",
    "insight program", "insight day",
}
_GRAD_PROGRAMME_KW = {
    "graduate programme", "graduate program", "graduate scheme", "grad scheme",
    "training contract", "vacation scheme", "graduate trainee",
    "graduate rotation", "trainee programme", "trainee program",
}
_SUMMER_KW = {
    "summer", "été", "summer analyst", "summer associate",
    "summer intern", "summer program", "summer programme",
}
_PLACEMENT_KW = {
    "placement", "year in industry", "sandwich course", "sandwich year",
    "alternance", "apprenti", "work-study", "industrial placement",
}


def classify_programme_type(title: str) -> str:
    t = title.lower()
    if any(k in t for k in _SPRING_WEEK_KW):
        return "Spring Week"
    if any(k in t for k in _GRAD_PROGRAMME_KW):
        return "Graduate Programme"
    if any(k in t for k in _SUMMER_KW):
        return "Summer Internship"
    if any(k in t for k in _PLACEMENT_KW):
        return "Industrial Placement"
    return "Off-Cycle Internship"


# ── company size classifier ───────────────────────────────────────────────────

def classify_company_size(nb_employees) -> str:
    if not nb_employees:
        return "COMING SOON"
    try:
        n = int(nb_employees)
    except (TypeError, ValueError):
        return "COMING SOON"
    if n < 200:
        return "Startup"
    if n <= 2000:
        return "Mid-size"
    return "Large"


# ── XML sitemap parser ────────────────────────────────────────────────────────

BLOCK_RE   = re.compile(r"<url>(.*?)</url>", re.DOTALL)
LOC_RE     = re.compile(r"<loc>([^<]+)</loc>")
HREF_EN_RE = re.compile(r'href="(https://www\.welcometothejungle\.com/en/companies/[^"]+)"')
LASTMOD_RE = re.compile(r"<lastmod>([^<]+)</lastmod>")


def _parse_lastmod(raw: str) -> datetime | None:
    raw = raw.rstrip("Z").replace("+00:00", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 3], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


_INTERNATIONAL_RE = re.compile(r"\binternational", re.IGNORECASE)


def _intern_title_slug(url: str) -> bool:
    jobs_idx = url.find("/jobs/")
    if jobs_idx < 0:
        return False
    slug = url[jobs_idx + 6:].split("_")[0]
    if _INTERNATIONAL_RE.search(slug):
        return False
    return bool(INTERN_PATTERNS.search(slug))


def parse_entries(xml: bytes) -> list[dict]:
    text    = xml.decode("utf-8", errors="replace")
    results = []

    for block_m in BLOCK_RE.finditer(text):
        block = block_m.group(1)

        lastmod_m = LASTMOD_RE.search(block)
        date_str  = lastmod_m.group(1)[:10] if lastmod_m else ""

        candidates: list[str] = []
        loc_m = LOC_RE.search(block)
        if loc_m and loc_m.group(1).startswith(EN_URL_PREFIX):
            candidates.append(loc_m.group(1))
        for href_m in HREF_EN_RE.finditer(block):
            candidates.append(href_m.group(1))

        for url in candidates:
            if not _intern_title_slug(url):
                continue
            parsed = _parse_url(url)
            if not parsed:
                continue
            parsed["date_posted"] = date_str
            results.append(parsed)
            break

    return results


# ── apply URL fetcher ─────────────────────────────────────────────────────────

def _job_api_url(wttj_job_url: str) -> str:
    """Convert a WTTJ listing URL to its job API endpoint.

    https://www.welcometothejungle.com/en/companies/{co}/jobs/{slug}
    → https://api.welcometothejungle.com/api/v1/organizations/{co}/jobs/{slug}
    """
    m = re.search(r"/en/companies/([^/]+)/jobs/(.+)$", wttj_job_url)
    if not m:
        return ""
    return f"https://api.welcometothejungle.com/api/v1/organizations/{m.group(1)}/jobs/{m.group(2)}"


def fetch_apply_urls(jobs: list[dict]) -> dict[str, dict]:
    """Return {wttj_url: {"apply_url": str, "published_at": str}} for each job."""
    results: dict[str, dict] = {}
    total = len(jobs)
    print(f"\nFetching apply URLs + published dates for {total} listings…")
    for i, job in enumerate(jobs, 1):
        wttj_url  = job["job_url"]
        api_url   = _job_api_url(wttj_url)
        apply_url = ""
        published_at = ""
        if api_url:
            data = fetch_json_url(api_url)
            try:
                job_data     = data.get("job", {})
                apply_url    = job_data.get("apply_url") or ""
                published_at = (job_data.get("published_at") or job_data.get("created_at") or "")[:10]
            except Exception:
                pass
        results[wttj_url] = {
            "apply_url":    apply_url or wttj_url,
            "published_at": published_at,
        }
        status = "✓" if apply_url else "↩ (wttj)"
        print(f"  [{i:>3}/{total}] {job['company_name'][:28]:<28} {status}", flush=True)
        time.sleep(0.2)
    found = sum(1 for v in results.values() if not v["apply_url"].startswith("https://www.welcometothejungle.com"))
    print(f"External apply URLs: {found}/{total}")
    return results


# ── company data fetcher ──────────────────────────────────────────────────────

CompanyData = dict   # {"logo_url": str, "wttj_sectors": list[str], "nb_employees": int|None}


LOGOS_DIR = ROOT / "static" / "logos"


def _download_logo(slug: str, remote_url: str) -> str:
    """Return the WTTJ CDN URL directly. Railway's filesystem is ephemeral so
    local caching is not viable; CDN URLs are permanent and always reachable."""
    return remote_url


def fetch_company_data(slugs: list[str]) -> dict[str, CompanyData]:
    """Fetch logo, sector tags, and employee count for each slug."""
    results: dict[str, CompanyData] = {}
    total = len(slugs)
    print(f"\nFetching company data for {total} unique companies…")
    for i, slug in enumerate(slugs, 1):
        data        = fetch_org(slug)
        logo_url    = ""
        company_url = ""
        nb_employees = None
        sectors: list[str] = []
        recruitment_process = ""
        try:
            org          = data.get("organization", {})
            logo         = org.get("logo") or {}
            logo_url     = logo.get("url", "")
            company_url  = (
                org.get("media_website_url")
                or (org.get("expansion_metadata") or {}).get("expansion_company_url")
                or ""
            )
            nb_employees = org.get("nb_employees") or None
            sectors = [s["name"] for s in (org.get("sectors") or [])]
            for s in (org.get("sectors") or []):
                pn = s.get("parent_name", "")
                if pn and pn not in sectors:
                    sectors.append(pn)
            rp = org.get("recruitment_process") or ""
            if isinstance(rp, list):
                recruitment_process = " → ".join(
                    (step.get("name") or step.get("title") or str(step)).strip()
                    for step in rp if step
                )
            elif isinstance(rp, str):
                recruitment_process = rp.strip()
        except Exception:
            pass
        logo_url = _download_logo(slug, logo_url)
        results[slug] = {
            "logo_url": logo_url, "company_url": company_url,
            "wttj_sectors": sectors, "nb_employees": nb_employees,
            "recruitment_process": recruitment_process,
        }
        size_label = classify_company_size(nb_employees)
        status = f"✓  {size_label}  {', '.join(sectors[:2]) or '—'}"
        print(f"  [{i:>3}/{total}] {slug:<35} {status}", flush=True)
        time.sleep(0.25)
    found = sum(1 for v in results.values() if v["logo_url"])
    print(f"Logos found: {found}/{total}")
    return results


# ── scrape pipeline (importable) ─────────────────────────────────────────────

def scrape_jobs() -> list[dict]:
    """Run the full WTTJ scrape pipeline and return filtered job dicts.

    Importable by the scheduler so it can upsert results into the DB.
    """
    from collections import Counter
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)
    print(f"WTTJ Internship Scraper  |  last {CUTOFF_HOURS}h  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    all_jobs: list[dict] = []
    seen_urls: set[str]  = set()

    for n in range(SITEMAP_SHARDS):
        try:
            xml  = fetch_shard(n)
            jobs = parse_entries(xml)
            new  = [j for j in jobs if j["job_url"] not in seen_urls]
            for j in new:
                seen_urls.add(j["job_url"])
                all_jobs.append(j)
            print(f"  shard {n:>2}: {len(new):>4} new listings  (running total: {len(all_jobs)})")
        except Exception as e:
            print(f"  shard {n:>2}: ERROR — {e}")

    if not all_jobs:
        print("\nNo internship listings found in the last 72 hours.")
        return

    all_jobs.sort(key=lambda j: j.get("date_posted", ""), reverse=True)

    # Region (country is known from sitemap; sector deferred until API data is fetched below)
    for j in all_jobs:
        j["region"] = classify_region(j["country"])

    # City allowlist filter
    ALLOWED_CITIES = {
        "paris", "new york", "new-york", "los angeles", "los-angeles",
        "chicago", "san francisco", "san-francisco", "london",
        "amsterdam", "boston", "berlin",
    }

    def _in_allowed_city(job: dict) -> bool:
        loc = job.get("location", "").lower()
        return any(city in loc for city in ALLOWED_CITIES)

    before = len(all_jobs)
    filtered_out = [j for j in all_jobs if not _in_allowed_city(j)]
    all_jobs     = [j for j in all_jobs if _in_allowed_city(j)]
    print(f"\nCity filter: {before} → {len(all_jobs)} listings kept")

    from collections import Counter
    excluded_cities = Counter(
        j.get("location", "unknown").lower() or "unknown"
        for j in filtered_out
    )
    print(f"\nTop cities excluded by allowlist ({len(filtered_out)} listings):")
    for city, count in excluded_cities.most_common(20):
        print(f"  {city:<30} {count}")

    if not all_jobs:
        print("No listings remain after city filter.")
        return

    # Preserve original WTTJ listing URL before replacing with external apply URL
    for j in all_jobs:
        j["wttj_url"] = j["job_url"]

    # Fetch apply URLs + real published_at — one request per job listing
    apply_map = fetch_apply_urls(all_jobs)
    for j in all_jobs:
        rec = apply_map.get(j["job_url"], {})
        j["job_url"] = rec.get("apply_url", j["job_url"])
        if rec.get("published_at"):
            j["date_posted"] = rec["published_at"]

    # Drop jobs with no external apply URL (WTTJ URL is not a useful apply link)
    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if j.get("job_url") and not j["job_url"].startswith("https://www.welcometothejungle.com")]
    print(f"No external apply URL: removed {before - len(all_jobs)}, {len(all_jobs)} remain")

    # Re-apply 72h cutoff using real published_at (sitemap lastmod can be stale)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if j.get("date_posted", "") >= cutoff_str]
    print(f"\nDate filter (published_at): removed {before - len(all_jobs)} stale listings, "
          f"{len(all_jobs)} remain")

    # Fetch company data (logo + WTTJ sector tags + employee count) — one request per unique slug
    unique_slugs = sorted({j["company_slug"] for j in all_jobs})
    company_data = fetch_company_data(unique_slugs)
    for j in all_jobs:
        cd               = company_data.get(j["company_slug"], {})
        j["logo_url"]             = cd.get("logo_url", "")
        j["company_url"]          = cd.get("company_url", "")
        j["sector"]               = classify_sector(j["title"], cd.get("wttj_sectors"))
        j["company_size"]         = classify_company_size(cd.get("nb_employees"))
        j["programme_type"]       = classify_programme_type(j["title"])
        j["recruitment_process"]  = cd.get("recruitment_process", "")

    # Remove HR/TA roles entirely
    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if not _is_hr_ta(j["title"])]
    print(f"\nHR/TA filter: removed {before - len(all_jobs)} roles, {len(all_jobs)} remain")

    # Paris filter: only keep Finance, Consulting (Large), Law, and 5 random Tech
    paris     = [j for j in all_jobs if "paris" in j.get("location", "").lower()]
    non_paris = [j for j in all_jobs if "paris" not in j.get("location", "").lower()]
    # Paris Finance: 1 role per company, any size
    p_finance_all = [j for j in paris if j["sector"] == "Finance"]
    _seen_co: set[str] = set()
    p_finance = []
    for j in p_finance_all:
        if j["company_slug"] not in _seen_co:
            p_finance.append(j)
            _seen_co.add(j["company_slug"])
    # Paris Consulting: Large only, max 1 per company per day
    _seen_cons: set[str] = set()
    p_cons_lg = []
    for j in [j for j in paris if j["sector"] == "Consulting" and j["company_size"] == "Large"]:
        key = f"{j['company_slug']}|{j['date_posted']}"
        if key not in _seen_cons:
            p_cons_lg.append(j)
            _seen_cons.add(key)
    p_law      = [j for j in paris if j["sector"] == "Law"]
    p_tech     = [j for j in paris if j["sector"] == "Tech"]
    p_tech_5   = random.sample(p_tech, min(5, len(p_tech)))
    kept_paris = p_finance + p_cons_lg + p_law + p_tech_5
    all_jobs   = non_paris + kept_paris
    all_jobs.sort(key=lambda j: j.get("date_posted", ""), reverse=True)
    print(f"Paris filter: {len(paris)} → {len(kept_paris)} kept "
          f"(Finance {len(p_finance)}/{len(p_finance_all)} 1-per-co, "
          f"Consulting/Large {len(p_cons_lg)}, Law {len(p_law)}, Tech 5/{len(p_tech)})")

    # Cap Paris at 50% of all EU jobs
    eu_jobs    = [j for j in all_jobs if j["region"] == "EU"]
    paris_jobs = [j for j in eu_jobs if "paris" in j.get("location", "").lower()]
    max_paris  = len(eu_jobs) // 2
    if len(paris_jobs) > max_paris:
        keep = set(id(j) for j in random.sample(paris_jobs, max_paris))
        all_jobs = [j for j in all_jobs if "paris" not in j.get("location", "").lower() or id(j) in keep]
        all_jobs.sort(key=lambda j: j.get("date_posted", ""), reverse=True)
        print(f"Paris 50% EU cap: trimmed {len(paris_jobs)} → {max_paris} Paris jobs "
              f"({max_paris}/{len(eu_jobs)} EU)")

    print(f"\nTotal: {len(all_jobs)} internship listings in last 72h")
    return all_jobs


# ── main (local CSV output) ───────────────────────────────────────────────────

def main() -> None:
    from collections import Counter
    print(f"WTTJ Internship Scraper  |  last {CUTOFF_HOURS}h  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    all_jobs = scrape_jobs()

    if not all_jobs:
        print("\nNo matching jobs found.")
        return

    # Preview table
    print()
    print(f"{'#':<5} {'Company':<24} {'Title':<34} {'Sector':<12} {'Programme':<22} {'Size':<12} {'Date':>10}")
    print("-" * 125)
    for i, j in enumerate(all_jobs[:60], 1):
        print(
            f"{i:<5} {j['company_name'][:23]:<24} {j['title'][:33]:<34} "
            f"{j['sector']:<12} {j['programme_type']:<22} {j['company_size']:<12} {j['date_posted']:>10}"
        )
    if len(all_jobs) > 60:
        print(f"  … and {len(all_jobs) - 60} more rows in the CSV")

    # Summaries
    sectors    = Counter(j["sector"] for j in all_jobs)
    regions    = Counter(j["region"] for j in all_jobs)
    prog_types = Counter(j["programme_type"] for j in all_jobs)
    sizes      = Counter(j["company_size"] for j in all_jobs)
    print(f"\nSector breakdown:")
    for s, n in sectors.most_common():
        print(f"  {s:<14} {n}")
    print(f"\nProgramme type breakdown:")
    for p, n in prog_types.most_common():
        print(f"  {p:<25} {n}")
    print(f"\nCompany size breakdown:")
    for sz, n in sizes.most_common():
        print(f"  {sz:<14} {n}")
    print(f"\nRegion breakdown:")
    for r, n in regions.most_common():
        print(f"  {(r or 'Other/Unknown'):<12} {n}")

    # Save CSV
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS)
        writer.writeheader()
        for j in all_jobs:
            writer.writerow({k: j.get(k, "") for k in CSV_COLS})
    print(f"\nSaved {len(all_jobs)} rows → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
