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
    DEPT_MAP, INDUSTRY_DEPT_MAP, REGION_LOCATION_FALLBACK,
)
from db.database       import fetchall, upsert_lead, USE_POSTGRES
from pipeline.matcher  import LinkedInMatcher, _extract_university, _extract_tenure, _extract_location

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Output path for training dataset
TRAINING_FILE = Path(__file__).parent.parent / "data" / "leads_training.jsonl"


# ── Query builders ────────────────────────────────────────────────────────────

def _build_query_alumni(company: str, university: str, dept_keywords: list[str]) -> str:
    """site:linkedin.com/in "Company" "University" ("kw1" OR "kw2")"""
    kw_str = " OR ".join(f'"{kw}"' for kw in dept_keywords[:4])
    return f'site:linkedin.com/in "{company}" "{university}" ({kw_str})'


def _build_query_broad(company: str, location: str, dept_keywords: list[str]) -> str:
    """site:linkedin.com/in "Company" "Location" ("kw1" OR "kw2")"""
    kw_str = " OR ".join(f'"{kw}"' for kw in dept_keywords[:4])
    return f'site:linkedin.com/in "{company}" "{location}" ({kw_str})'


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

def _depts_for_industry(industry: str) -> list[str]:
    """Return relevant DEPT_MAP keys for a given industry string."""
    for ind_key, depts in INDUSTRY_DEPT_MAP.items():
        if ind_key.lower() == (industry or "").lower():
            return depts
    # Fuzzy fallback
    ind_lower = (industry or "").lower()
    if "finance" in ind_lower or "banking" in ind_lower:
        return INDUSTRY_DEPT_MAP.get("Finance", [])
    if "tech" in ind_lower or "software" in ind_lower or "engineer" in ind_lower:
        return INDUSTRY_DEPT_MAP.get("Technology", [])
    if "law" in ind_lower or "legal" in ind_lower:
        return INDUSTRY_DEPT_MAP.get("Law", [])
    return ["software_engineering"]  # safe default


def build_leads(
    company_filter: str = "",
    university:     str = "",
    dry_run:        bool = False,
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

    # Fetch distinct companies from jobs table
    rows = fetchall(
        "SELECT DISTINCT company, location, industry FROM jobs"
        + (" WHERE lower(company) = lower('" + company_filter + "')" if company_filter else "")
    )

    if not rows:
        logger.warning("No jobs found in DB — run scraper first")
        return 0

    logger.info(f"Building leads for {len(rows)} company/industry rows")
    total_upserted = 0
    seen_pairs: set = set()  # (company, dept) to avoid duplicate crawl in same run

    for row in rows:
        company  = (row.get("company") or "").strip()
        location = (row.get("location") or "").strip()
        industry = (row.get("industry") or "").strip()

        if not company:
            continue

        # Use actual job location, fall back to region hint
        if not location:
            location = REGION_LOCATION_FALLBACK.get("UK", "London")

        depts = _depts_for_industry(industry)

        for dept_name in depts:
            pair = (company.lower(), dept_name)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            dept_keywords = DEPT_MAP.get(dept_name, [dept_name])

            # Query A — alumni first
            leads_a: list[dict] = []
            if university:
                query_a = _build_query_alumni(company, university, dept_keywords)
                logger.info(f"  Query A: {query_a[:80]}")
                raw_a_p1 = matcher._serper_search(query_a, count=10, page=1)
                raw_a_p2 = matcher._serper_search(query_a, count=10, page=2)
                raw_a    = _dedup(raw_a_p1 + raw_a_p2)
                parsed_a = [_parse_snippet(r, university=university, dept_tag=dept_name) for r in raw_a]
                leads_a  = [l for l in parsed_a if l]
                if not dry_run:
                    _save_training_records(company, dept_name, location, query_a, "alumni", 1, raw_a_p1, [_parse_snippet(r, university, dept_name) for r in raw_a_p1], university)
                    _save_training_records(company, dept_name, location, query_a, "alumni", 2, raw_a_p2, [_parse_snippet(r, university, dept_name) for r in raw_a_p2], university)

            # Query B — broad location search (always run, supplement A)
            query_b = _build_query_broad(company, location, dept_keywords)
            logger.info(f"  Query B: {query_b[:80]}")
            raw_b_p1 = matcher._serper_search(query_b, count=10, page=1)
            raw_b_p2 = matcher._serper_search(query_b, count=10, page=2)
            raw_b    = _dedup(raw_b_p1 + raw_b_p2)
            parsed_b = [_parse_snippet(r, university=university, dept_tag=dept_name) for r in raw_b]
            leads_b  = [l for l in parsed_b if l]
            if not dry_run:
                _save_training_records(company, dept_name, location, query_b, "broad", 1, raw_b_p1, [_parse_snippet(r, university, dept_name) for r in raw_b_p1], university)
                _save_training_records(company, dept_name, location, query_b, "broad", 2, raw_b_p2, [_parse_snippet(r, university, dept_name) for r in raw_b_p2], university)

            # Merge A + B, deduplicate by linkedin_url
            all_leads: dict[str, dict] = {}
            for lead in leads_a + leads_b:
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
    parser.add_argument("--dry-run",    action="store_true", help="Parse only, no DB writes")
    args = parser.parse_args()

    n = build_leads(
        company_filter = args.company,
        university     = args.university,
        dry_run        = args.dry_run,
    )
    print(f"Done — {n} leads upserted")
