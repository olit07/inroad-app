"""
One-off script: regenerate today's email drafts using Claude.
Run via: railway run python pipeline/regen_today.py
"""
import sys
import os
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import fetchall, execute as db_execute, USE_POSTGRES, fetchone as db_fetchone
from pipeline.daily_cards import generate_email_draft

ph = "%s" if USE_POSTGRES else "?"

today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
print(f"Regenerating drafts for match_date = {today_str}")

rows = fetchall(
    f"""
    SELECT m.id, m.student_id, m.person_name, m.person_title, m.person_company,
           m.is_alumni, m.email_subject, m.email_body,
           j.title as job_title, j.url as job_url, j.industry,
           s.name as student_name, s.university, s.bio, s.industries
    FROM matches m
    JOIN jobs j ON j.id = m.job_id
    JOIN students s ON s.id = m.student_id
    WHERE m.match_date = {ph}
    """,
    (today_str,)
)

print(f"Found {len(rows)} matches for today")

if not rows:
    print("Nothing to regenerate.")
    sys.exit(0)

updated = 0
failed  = 0

for row in rows:
    student = {
        "name":       row.get("student_name", ""),
        "university": row.get("university", ""),
        "bio":        row.get("bio", ""),
        "industries": row.get("industries", "[]"),
    }
    lead = {
        "name":     row.get("person_name", ""),
        "title":    row.get("person_title", ""),
        "company":  row.get("person_company", ""),
        "is_alumni": bool(row.get("is_alumni", False)),
    }
    job = {
        "title":        row.get("job_title", ""),
        "company_name": row.get("person_company", ""),
        "url":          row.get("job_url", ""),
        "industries":   json.loads(row.get("industries") or "[]"),
        "industry":     row.get("industry", ""),
    }

    try:
        subject, body = generate_email_draft(student, lead, job)
        db_execute(
            f"UPDATE matches SET email_subject = {ph}, email_body = {ph} WHERE id = {ph}",
            (subject, body, row["id"]),
        )
        updated += 1
        print(f"  ✓ [{row['id']}] {row.get('student_name')} → {row.get('person_name')} @ {row.get('person_company')}")
    except Exception as e:
        failed += 1
        print(f"  ✗ [{row['id']}] {row.get('student_name')} — error: {e}")

print(f"\nDone: {updated} updated, {failed} failed")
