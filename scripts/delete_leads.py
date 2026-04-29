#!/usr/bin/env python3
"""
scripts/delete_leads.py
Delete specific leads from the database by ID.

Usage:
    DATABASE_URL=postgresql://... python scripts/delete_leads.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db.database import execute, fetchall  # noqa: E402

IDS_TO_DELETE = [27114, 27117]

def main():
    placeholders = ','.join(['%s'] * len(IDS_TO_DELETE))
    rows = fetchall(
        f"SELECT id, name, current_company FROM leads WHERE id IN ({placeholders})",
        tuple(IDS_TO_DELETE)
    )
    if not rows:
        print("No matching leads found.")
        return

    print("Leads to delete:")
    for r in rows:
        print(f"  id={r[0]}  name={r[1]}  company={r[2]}")

    confirm = input("\nDelete these leads? [y/N] ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        return

    execute(
        f"DELETE FROM leads WHERE id IN ({placeholders})",
        tuple(IDS_TO_DELETE)
    )
    print(f"Deleted {len(IDS_TO_DELETE)} lead(s).")

if __name__ == '__main__':
    main()
