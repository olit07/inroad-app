"""
pipeline/lead_builder.py
Systematic lead pre-fetch: crawls Google (via Serper) for LinkedIn profiles
for every (company, department, location) combination derived from active
Trackr jobs, and stores the results in the `leads` DB table.

Also writes a JSONL training file for prompt improvement:
  data/leads_training.jsonl

Usage:
    python pipeline/lead_builder.py                    # run for all companies
    python pipeline/lead_builder.py --company "Goldman Sachs"
    python pipeline/lead_builder.py --dry-run          # parse only, no DB writes
"""
import os
import re
import sys
import json
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings   import (
    DEPT_MAP, TITLE_DEPT_MAP, UNI_FULL_NAMES, REGION_LOCATION_FALLBACK,
    COMPANY_EMAIL_FORMATS,
)
from db.database       import fetchall, upsert_lead, USE_POSTGRES
from pipeline.matcher  import LinkedInMatcher, _extract_university, _extract_tenure, _extract_location, _infer_seniority_from_title

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Output path for training dataset
TRAINING_FILE = Path(__file__).parent.parent / "data" / "leads_training.jsonl"


# ── Query builders ────────────────────────────────────────────────────────────

def _infer_location_from_url(url: str) -> str:
    """Infer region from job URL when jobs.location is blank."""
    u = url.lower()
    if "uk.linkedin" in u or ".co.uk" in u or "/uk/" in u or "greenhouse.io" not in u and "uk" in u:
        return "UK"
    if "linkedin.com/jobs" in u and "uk" not in u:
        return "US"
    # Greenhouse/Lever/Ashby hosted roles default to US unless company is known UK
    if any(x in u for x in ["greenhouse.io", "lever.co", "ashbyhq.com", "workday.com"]):
        return "US"
    return "UK"  # safe default for Trackr which is primarily UK-focused


def _full_uni_name(university: str) -> str:
    """Expand a short university name to its full official name for Google search."""
    key = university.strip().lower()
    # Direct lookup
    if key in UNI_FULL_NAMES:
        return UNI_FULL_NAMES[key]
    # Already a full name (contains "University" or "College") — use as-is
    if "university" in key or "college" in key or "school of" in key:
        return university.strip()
    # No match — return as-is (better than silently dropping it)
    return university.strip()


def _dept_from_title(title: str) -> str:
    """
    Map a job title to a single DEPT_MAP key by scanning title keywords.
    Returns the first matching dept_tag from TITLE_DEPT_MAP, or
    'software_engineering' as a safe default.
    """
    title_lower = title.lower()
    for keywords, dept_tag in TITLE_DEPT_MAP:
        if any(kw in title_lower for kw in keywords):
            return dept_tag
    return "software_engineering"


def _guess_domain(company: str) -> str:
    """Derive email domain from company name for unknown companies."""
    name = company.lower()
    for suffix in [
        " capital management", " asset management", " investment management",
        " wealth management", " portfolio management",
        " & co.", " & co", " and co", " & company", " llp", " llc",
        " inc.", " inc", " ltd", " plc", " group", " partners",
        " advisors", " advisory", " associates", " holdings",
        " securities", " financial", " services", " ventures",
        " consulting", " international", " global", " management",
    ]:
        name = name.replace(suffix, "")
    name = re.sub(r"[^a-z0-9]", "", name)
    return f"{name}.com" if name else ""


def _infer_email(name: str, company: str) -> str:
    """Build expected email address from name + company using known format table."""
    key   = company.strip().lower()
    entry = COMPANY_EMAIL_FORMATS.get(key)
    if entry:
        fmt, domain = entry
    else:
        domain = _guess_domain(company)
        if not domain:
            return ""
        fmt = "FL"
    parts = name.strip().split()
    if len(parts) < 2:
        return ""
    first = re.sub(r"[^a-z]", "", parts[0].lower())
    last  = re.sub(r"[^a-z]", "", parts[-1].lower())
    if not first or not last:
        return ""
    if fmt == "FL":  return f"{first}.{last}@{domain}"
    if fmt == "fL":  return f"{first[0]}{last}@{domain}"
    if fmt == "f.L": return f"{first[0]}.{last}@{domain}"
    if fmt == "F_L": return f"{first}_{last}@{domain}"
    if fmt == "F":   return f"{first}@{domain}"
    return ""


def _build_query_alumni(company: str, location: str, university_full: str, dept_keyword: str) -> str:
    """site:linkedin.com/in "dept keyword" "Company" "City" "Full University Name" """
    return f'site:linkedin.com/in "{dept_keyword}" "{company}" "{location}" "{university_full}"'


def _build_query_broad(company: str, location: str, dept_keyword: str) -> str:
    """site:linkedin.com/in "dept keyword" "Company" "City" """
    return f'site:linkedin.com/in "{dept_keyword}" "{company}" "{location}"'


# ── Snippet parser (standalone, mirrors matcher._parse_snippet) ───────────────

def _parse_snippet(result: dict, university: str = "", dept_tag: str = "") -> dict | None:
    """
    Parse a raw Serper organic result into a structured lead dict.
    Returns None if the result is not a valid LinkedIn profile URL.
    """
    import re as _re
    raw_name = result.get("name", "")
    url      = result.get("url", result.get("link", ""))
    snippet  = result.get("snippet", "")

    if "linkedin.com/in/" not in url:
        return None

    # Split on | · – -
    title_parts = _re.split(r"\s*[|·–\-]\s*", raw_name)
    title_parts = [p.strip() for p in title_parts if p.strip()]
    title_parts = [p for p in title_parts if p.lower() not in ("linkedin",)]

    name    = title_parts[0] if len(title_parts) >= 1 else ""
    title   = title_parts[1] if len(title_parts) >= 2 else ""
    company = title_parts[2] if len(title_parts) >= 3 else ""

    if not name or len(name.split()) < 1:
        return None

    found_uni = _extract_university(snippet)
    is_alumni = bool(
        university and (
            university.lower() in snippet.lower() or
            (found_uni and university.lower() in found_uni.lower())
        )
    )

    tenure_months            = _extract_tenure(snippet)
    location_city, location_country = _extract_location(snippet)
    linkedin_url             = _re.sub(r"\?.*$", "", url)

    return {
        "name":             name,
        "title":            title,
        "company":          company,
        "university":       found_uni,
        "linkedin_url":     linkedin_url,
        "snippet":          snippet[:300],
        "location_city":    location_city,
        "location_country": location_country,
        "tenure_months":    tenure_months,
        "is_alumni":        is_alumni,
        "dept_tag":         dept_tag,
        "seniority":        _infer_seniority_from_title(title),
        "scraped_rank":     result.get("_rank", 0),
    }


# ── Training file writer ──────────────────────────────────────────────────────

def _save_training_records(
    company:   str,
    dept_name: str,
    location:  str,
    query:     str,
    query_type: str,
    page:      int,
    raw_results: list[dict],
    parsed:    list[dict | None],
    university: str = "",
) -> None:
    TRAINING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRAINING_FILE, "a", encoding="utf-8") as f:
        for raw, parsed_lead in zip(raw_results, parsed):
            record = {
                "company":    company,
                "dept":       dept_name,
                "job_location": location,
                "query_type": query_type,
                "university": university,
                "query":      query,
                "page":       page,
                "raw":        raw,
                "parsed":     parsed_lead,
                "verified":   None,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Main build loop ───────────────────────────────────────────────────────────


def build_leads(
    company_filter: str = "",
    university:     str = "",
    dry_run:        bool = False,
    max_companies:  int = 0,
) -> int:
    """
    Main entry point. Fetches leads for all active jobs (or filtered by company).
    Returns total leads upserted.

    - Gets distinct (company, location, industry) from active jobs table.
    - For each: runs Query A (alumni) → if < 2 leads, also runs Query B (broad).
    - Parses snippets, stores in leads table, appends to training JSONL.
    """
    matcher = LinkedInMatcher()
    if not matcher.serper_key:
        logger.error("SERPER_API_KEY not set — cannot build leads")
        return 0

    # Fetch distinct (company, title, location) from jobs table
    where = " WHERE source='trackr'"
    if company_filter:
        where += f" AND lower(company) = lower('{company_filter}')"
    rows = fetchall(
        f"SELECT DISTINCT ON (company, title, location) company, title, location, url, opening_date "
        f"FROM jobs{where} "
        f"ORDER BY company, title, location, opening_date DESC NULLS LAST"
    )
    # Re-sort the deduplicated rows by most recent opening_date first
    rows = sorted(rows, key=lambda r: r.get("opening_date") or "", reverse=True)

    if not rows:
        logger.warning("No jobs found in DB — run scraper first")
        return 0

    logger.info(f"Building leads for {len(rows)} company/title rows")
    total_upserted = 0
    seen_pairs: set = set()  # (company, dept) to avoid duplicate crawl in same run

    uni_full = _full_uni_name(university) if university else ""

    for row in rows:
        company      = (row.get("company") or "").strip()
        job_title    = (row.get("title") or "").strip()
        job_url      = (row.get("url") or "").strip()
        location     = (row.get("location") or "").strip()
        opening_date = (row.get("opening_date") or "").strip()

        if not company:
            continue

        # Use raw jobs.location; infer from URL if blank
        if not location:
            location = _infer_location_from_url(job_url)
        # Translate region codes to cities for Serper query (needed for search quality)
        search_location = REGION_LOCATION_FALLBACK.get(location, location) or "London"

        dept_name     = _dept_from_title(job_title)
        dept_keywords = DEPT_MAP.get(dept_name, [dept_name])
        dept_keyword  = dept_keywords[0]  # most specific / descriptive keyword

        pair = (company.lower(), dept_name)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        if max_companies and len(seen_pairs) > max_companies:
            logger.info(f"Reached max_companies={max_companies}, stopping.")
            break

        # Query A — alumni first (only when university provided)
        leads_a: list[dict] = []
        if uni_full:
            query_a = _build_query_alumni(company, search_location, uni_full, dept_keyword)
            logger.info(f"  Query A: {query_a[:100]}")
            raw_a_p1 = matcher._serper_search(query_a, count=10, page=1)
            raw_a_p2 = matcher._serper_search(query_a, count=10, page=2)
            for i, r in enumerate(raw_a_p1): r["_rank"] = i + 1
            for i, r in enumerate(raw_a_p2): r["_rank"] = i + 11
            raw_a    = _dedup(raw_a_p1 + raw_a_p2)
            parsed_a = [_parse_snippet(r, university=university, dept_tag=dept_name) for r in raw_a]
            leads_a  = [l for l in parsed_a if l]
            if not dry_run:
                _save_training_records(company, dept_name, location, query_a, "alumni", 1, raw_a_p1, [_parse_snippet(r, university, dept_name) for r in raw_a_p1], university)
                _save_training_records(company, dept_name, location, query_a, "alumni", 2, raw_a_p2, [_parse_snippet(r, university, dept_name) for r in raw_a_p2], university)

        # Query B — broad (dept + company + city, no university)
        query_b = _build_query_broad(company, search_location, dept_keyword)
        logger.info(f"  Query B: {query_b[:100]}")
        raw_b_p1 = matcher._serper_search(query_b, count=10, page=1)
        raw_b_p2 = matcher._serper_search(query_b, count=10, page=2)
        for i, r in enumerate(raw_b_p1): r["_rank"] = i + 1
        for i, r in enumerate(raw_b_p2): r["_rank"] = i + 11
        raw_b    = _dedup(raw_b_p1 + raw_b_p2)
        parsed_b = [_parse_snippet(r, university=university, dept_tag=dept_name) for r in raw_b]
        leads_b  = [l for l in parsed_b if l]
        if not dry_run:
            _save_training_records(company, dept_name, location, query_b, "broad", 1, raw_b_p1, [_parse_snippet(r, university, dept_name) for r in raw_b_p1], university)
            _save_training_records(company, dept_name, location, query_b, "broad", 2, raw_b_p2, [_parse_snippet(r, university, dept_name) for r in raw_b_p2], university)

        # Merge A + B, deduplicate by linkedin_url
        # Stamp job metadata onto each lead — snippet parsing can't reliably extract these
        all_leads: dict[str, dict] = {}
        for lead in leads_a + leads_b:
            lead["company"]            = company       # always the company we searched for
            lead["location_city"]      = location      # raw jobs.location (UK/US/EU) or URL-inferred
            lead["job_title"]          = job_title     # the job title that triggered this search
            lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
            lead["job_opening_date"]   = opening_date  # opening date from jobs table
            url = lead.get("linkedin_url", "")
            if url and url not in all_leads:
                all_leads[url] = lead

        logger.info(f"  {company} / {dept_name}: {len(all_leads)} unique leads")

        if not dry_run:
            for lead in all_leads.values():
                try:
                    upsert_lead(lead)
                    total_upserted += 1
                except Exception as e:
                    logger.warning(f"  upsert_lead failed: {e}")

    logger.info(f"Lead build complete — {total_upserted} leads upserted")
    return total_upserted


def _dedup(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            out.append(r)
    return out


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build leads pool from Google/Serper")
    parser.add_argument("--company",    default="", help="Filter to a single company name")
    parser.add_argument("--university", default="", help="University to use for alumni Query A")
    parser.add_argument("--dry-run",       action="store_true", help="Parse only, no DB writes")
    parser.add_argument("--max-companies", type=int, default=0, help="Stop after N unique company/dept pairs")
    args = parser.parse_args()

    n = build_leads(
        company_filter = args.company,
        university     = args.university,
        dry_run        = args.dry_run,
        max_companies  = args.max_companies,
    )
    print(f"Done — {n} leads upserted")
