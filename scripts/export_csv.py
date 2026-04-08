#!/usr/bin/env python3
"""
scripts/export_csv.py
Pull students, jobs, and leads from Postgres and write them to data/*.csv.

Usage:
    DATABASE_URL=postgresql://... python scripts/export_csv.py

Writes:
    data/students.csv
    data/jobs.csv
    data/leads.csv
"""

import csv
import os
import sys

# ── resolve project root so db.database imports cleanly ──────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db.database import fetchall  # noqa: E402
from pipeline.lead_builder import _dept_from_title  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

EXPORTS = {
    "students": {
        "file": os.path.join(DATA_DIR, "students.csv"),
        "sql": """
            SELECT id, email, name, age, status, industries, company_size,
                   bio, university, created_at, last_seen, deactivated_at
            FROM students
            ORDER BY id
        """,
    },
    "jobs": {
        "file": os.path.join(DATA_DIR, "jobs.csv"),
        "sql": """
            SELECT id, title, company, url, location, industry, company_size,
                   posted_at, opening_date, closing_date, source, created_at
            FROM jobs
            ORDER BY id
        """,
    },
    "leads": {
        "file": os.path.join(DATA_DIR, "leads.csv"),
        "sql": """
            SELECT company, dept_tag,
                   COALESCE(NULLIF(location_city,''), location_country, '') AS job_location,
                   name, title, linkedin_url, snippet,
                   fetched_at, stale_after
            FROM leads
            ORDER BY company, dept_tag
        """,
    },
}


LEADS_COLUMNS = [
    "job_company", "job_department", "job_location",
    "scraped_name", "scraped_title", "matched_department",
    "scraped_linkedin", "scraped_snippet",
    "fetched_at", "stale_after",
]


def _transform_lead(row: dict) -> dict:
    return {
        "job_company":        row.get("company", ""),
        "job_department":     row.get("dept_tag", ""),
        "job_location":       row.get("job_location", ""),
        "scraped_name":       row.get("name", ""),
        "scraped_title":      row.get("title", ""),
        "matched_department": _dept_from_title(row.get("title") or ""),
        "scraped_linkedin":   row.get("linkedin_url", ""),
        "scraped_snippet":    row.get("snippet", ""),
        "fetched_at":         row.get("fetched_at", ""),
        "stale_after":        row.get("stale_after", ""),
    }


def export(name: str, cfg: dict) -> int:
    try:
        rows = fetchall(cfg["sql"])
    except Exception as e:
        print(f"  {name}: skipped — {e}")
        return 0
    if not rows:
        print(f"  {name}: 0 rows — CSV not written")
        return 0
    if name == "leads":
        rows = [_transform_lead(r) for r in rows]
        fieldnames = LEADS_COLUMNS
    else:
        fieldnames = list(rows[0].keys())
    with open(cfg["file"], "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {name}: {len(rows)} rows → {cfg['file']}")
    return len(rows)


if __name__ == "__main__":
    from db.database import USE_POSTGRES
    if not USE_POSTGRES:
        print("WARNING: DATABASE_URL not set — reading from local SQLite (ccc.db)")
    else:
        print("Connected to Postgres")

    print()
    for name, cfg in EXPORTS.items():
        export(name, cfg)
    print("\nDone.")
