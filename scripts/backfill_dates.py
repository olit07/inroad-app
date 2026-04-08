#!/usr/bin/env python3
"""
scripts/backfill_dates.py

Re-fetches all Trackr programmes and updates opening_date / closing_date
for every matching job already in the database.

Usage:
    DATABASE_URL=postgresql://... python scripts/backfill_dates.py
"""

import os
import sys
from pathlib import Path

ROOT = str(Path(__file__).parent.parent)
sys.path.insert(0, ROOT)

from scrapers.trackr import TrackrScraper
from db.database import get_conn, USE_POSTGRES

PH = "%s" if USE_POSTGRES else "?"


def main():
    print("Fetching programmes from Trackr API...")
    scraper = TrackrScraper()

    jobs = []
    for job in scraper.scrape():
        jobs.append(job)

    print(f"Fetched {len(jobs)} open programmes from Trackr\n")

    updated = 0
    skipped = 0

    with get_conn() as conn:
        cur = conn.cursor()

        for job in jobs:
            url          = (job.get("url") or "").strip()
            company      = (job.get("company_name") or "").strip()
            title        = (job.get("title") or "").strip()
            opening_date = (job.get("opening_date") or "").strip() or None
            closing_date = (job.get("closing_date") or "").strip() or None

            if not opening_date and not closing_date:
                skipped += 1
                continue  # API returned no dates — nothing to write

            # Find existing job by URL first, then company+title
            row = None
            if url:
                cur.execute(
                    f"SELECT id, opening_date, closing_date FROM jobs WHERE url = {PH}",
                    (url,)
                )
                row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"SELECT id, opening_date, closing_date FROM jobs "
                    f"WHERE company = {PH} AND title = {PH}",
                    (company, title)
                )
                row = cur.fetchone()

            if row is None:
                skipped += 1
                continue  # not in DB yet

            job_id = row[0] if not isinstance(row, dict) else row["id"]

            cur.execute(
                f"UPDATE jobs SET opening_date = {PH}, closing_date = {PH} WHERE id = {PH}",
                (opening_date, closing_date, job_id)
            )
            updated += 1

            label = f"{company} — {title[:50]}"
            print(f"  [{job_id}] {label}")
            print(f"        open={opening_date}  close={closing_date}")

    print(f"\nDone. {updated} jobs updated, {skipped} skipped (no dates or not in DB).")


if __name__ == "__main__":
    if not USE_POSTGRES:
        print("WARNING: DATABASE_URL not set — writing to local SQLite (ccc.db)")
    main()
