"""
scripts/fetch_kcl_ucl_leads.py

Targeted KCL and UCL alumni search for the 30 most recent trackr/wttj listings.

Runs only the alumni Query A (2 pages each) for King's College London and
University College London against the 30 most recently added trackr/wttj companies.

Credit cost: 30 companies × 2 unis × 2 pages = 120 Serper searches.

Usage:
    python scripts/fetch_kcl_ucl_leads.py
    python scripts/fetch_kcl_ucl_leads.py --dry-run
    python scripts/fetch_kcl_ucl_leads.py --top-n 50
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import fetchall, upsert_lead, USE_POSTGRES
from pipeline.lead_builder import (
    _dept_from_title, _search_keyword_from_title,
    _infer_email, _parse_snippet, _dedup,
    _company_name_overlap, _snippet_role_is_past,
    _classify_lead_type,
)
from config.settings import REGION_LOCATION_FALLBACK
from pipeline.matcher import LinkedInMatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TARGET_UNIS = [
    "King's College London",
    "University College London",
]

_REGION_COUNTRY = {"UK": "united kingdom", "US": "united states"}


def run(dry_run: bool = False, top_n: int = 30, universities: list | None = None) -> dict:
    matcher = LinkedInMatcher()
    unis = universities or TARGET_UNIS

    # Most recent distinct companies from trackr/wttj (the sources shown in opportunities)
    if USE_POSTGRES:
        rows = fetchall(
            """
            SELECT DISTINCT ON (lower(company)) company, title, location, url, opening_date
            FROM jobs
            WHERE source IN ('trackr', 'wttj')
            AND company IS NOT NULL AND company != ''
            AND lower(company) != 'trackr'
            AND url IS NOT NULL AND url != ''
            ORDER BY lower(company), COALESCE(opening_date, created_at::text) DESC NULLS LAST
            """
        )
        rows = sorted(rows, key=lambda r: r.get("opening_date") or "", reverse=True)
    else:
        rows = fetchall(
            """
            SELECT company, title, location, url, MAX(COALESCE(opening_date, created_at)) AS opening_date
            FROM jobs
            WHERE source IN ('trackr', 'wttj')
            AND company IS NOT NULL AND company != ''
            AND lower(company) != 'trackr'
            AND url IS NOT NULL AND url != ''
            GROUP BY lower(company)
            ORDER BY MAX(COALESCE(opening_date, created_at)) DESC
            """
        )
    rows = rows[:top_n]
    companies = [r for r in rows if r.get("company")]
    logger.info(f"Running targeted alumni searches ({', '.join(unis)}) for {len(companies)} companies")

    totals = {u: 0 for u in unis}
    skipped_exhausted = False

    for row in companies:
        company      = (row.get("company") or "").strip()
        job_title    = (row.get("title") or "").strip()
        job_url      = (row.get("url") or "").strip()
        location     = (row.get("location") or "UK").strip() or "UK"
        opening_date = (row.get("opening_date") or "").strip()

        search_location = REGION_LOCATION_FALLBACK.get(location, location) or "London"
        dept_name    = _dept_from_title(job_title)
        dept_keyword = _search_keyword_from_title(job_title, dept_name)

        for uni in unis:
            query = (
                f'site:linkedin.com/in "{dept_keyword}" '
                f'"{company}" "{search_location}" "{uni}"'
            )
            logger.info(f"  {company} / {uni[:20]}: {query[:100]}")

            all_raw = []
            for page in [1, 2]:
                try:
                    raw = matcher._search(query, count=10, page=page)
                    for i, r in enumerate(raw):
                        r["_rank"] = (page - 1) * 10 + i + 1
                    all_raw.extend(raw)
                except RuntimeError as e:
                    if "SERPER_CREDITS_EXHAUSTED" in str(e):
                        logger.critical("SERPER CREDITS EXHAUSTED — stopping")
                        skipped_exhausted = True
                        break
                    logger.warning(f"  Search error: {e}")
                    break

            if skipped_exhausted:
                break

            all_raw = _dedup(all_raw)
            saved = 0
            for r in all_raw:
                lead = _parse_snippet(r, university=uni, dept_tag=dept_name)
                if not lead:
                    continue
                parsed_co = lead.get("company", "") or lead.get("title", "")
                if parsed_co and not _company_name_overlap(parsed_co, company):
                    continue
                if _snippet_role_is_past(lead.get("snippet", "")):
                    continue

                lead["company"]            = company
                lead["job_title"]          = job_title
                lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
                lead["job_opening_date"]   = opening_date
                lead["lead_type"]          = _classify_lead_type(lead.get("title", ""), dept_name)
                if not lead.get("location_country") and location in _REGION_COUNTRY:
                    lead["location_country"] = _REGION_COUNTRY[location]

                if not dry_run:
                    try:
                        upsert_lead(lead)
                        saved += 1
                    except Exception as exc:
                        logger.warning(f"  upsert_lead failed: {exc}")
                else:
                    logger.info(
                        f"    [dry-run] {lead.get('name')} | {lead.get('title')} "
                        f"| uni={lead.get('university')}"
                    )
                    saved += 1

            logger.info(f"    -> {saved} leads {'(dry-run)' if dry_run else 'saved'}")
            totals[uni] += saved

        if skipped_exhausted:
            break

    logger.info("Done. Summary:")
    for uni, count in totals.items():
        logger.info(f"  {uni}: {count} leads {'found' if dry_run else 'upserted'}")
    return totals


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Parse and print, no DB writes")
    parser.add_argument("--top-n", type=int, default=30, help="Number of most recent companies to process")
    args = parser.parse_args()
    run(dry_run=args.dry_run, top_n=args.top_n)
