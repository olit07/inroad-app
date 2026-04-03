"""
queue_builder.py — nightly pre-generation of the card queue.
Run at 23:00 UTC to score and enqueue tomorrow's top-3 jobs per student.
"""
import json
import logging
from datetime import date, timedelta

from config.settings import DAILY_MATCH_QUOTA
from db import database as db
from pipeline.daily_cards import score_job

logger = logging.getLogger(__name__)


def _get_seen_history(student_id: int) -> set:
    """Return set of job_ids already shown to this student."""
    rows = db.fetchall(
        "SELECT job_id FROM matches WHERE student_id = ?", (student_id,)
    )
    return {r["job_id"] for r in rows}


def build_queue_for_student(student_id: int, target_date: date) -> int:
    """Score jobs and enqueue top DAILY_MATCH_QUOTA for a student. Returns count queued."""
    student = db.get_student_by_id(student_id)
    if not student or student.get("deactivated_at"):
        return 0

    seen_job_ids = _get_seen_history(student_id)
    student_industries = json.loads(student.get("industries") or "[]")

    # Fetch recently active jobs
    if db.USE_POSTGRES:
        all_jobs = db.fetchall(
            "SELECT * FROM jobs WHERE created_at > NOW() - INTERVAL '21 days'", []
        )
    else:
        all_jobs = db.fetchall(
            "SELECT * FROM jobs WHERE created_at > datetime('now', '-21 days')", []
        )

    eligible = [
        j for j in all_jobs
        if j["id"] not in seen_job_ids
        and (not student_industries or j.get("industry") in student_industries)
    ]
    if not eligible:
        return 0

    # Filter by company_size preference if the student has one set
    if student.get("company_size"):
        student_size = student["company_size"].lower().strip()
        filtered = []
        for job in eligible:
            job_size = (job.get("company_size") or "").lower().strip()
            if not job_size:
                filtered.append(job)
            else:
                if any(kw in job_size for kw in ("startup", "small", "under 200", "seed", "early")):
                    norm = "startup"
                elif any(kw in job_size for kw in ("mid", "medium", "200", "500", "1000", "2000")):
                    norm = "mid"
                elif any(kw in job_size for kw in ("large", "enterprise", "2000+", "10000")):
                    norm = "large"
                else:
                    norm = job_size
                if norm == student_size:
                    filtered.append(job)
        eligible = filtered

    # Greedy diverse selection
    scored = sorted(
        [(j, score_job(j, student, [])) for j in eligible],
        key=lambda x: x[1],
        reverse=True,
    )

    selected = []
    companies_used: set = set()
    industries_count: dict = {}
    target_str = target_date.isoformat()

    for job, score in scored:
        if len(selected) >= DAILY_MATCH_QUOTA:
            break
        company = job.get("company") or job.get("company_name", "")
        industry = job.get("industry", "")
        if company in companies_used:
            continue
        if industries_count.get(industry, 0) >= 2:
            continue
        selected.append((job, score))
        companies_used.add(company)
        industries_count[industry] = industries_count.get(industry, 0) + 1

    for job, score in selected:
        breakdown = json.dumps({"job_score": round(score, 2)})
        db.enqueue_card(student_id, job["id"], round(score, 2), target_str, score_breakdown=breakdown)

    return len(selected)


def build_queue_all_students(target_date: date = None) -> dict:
    """Build queue for all active students. Returns summary dict."""
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    students = db.fetchall(
        "SELECT id FROM students WHERE deactivated_at IS NULL", []
    )
    total_queued = 0
    errors = 0
    for row in students:
        try:
            total_queued += build_queue_for_student(row["id"], target_date)
        except Exception as e:
            logger.error(f"Queue build failed for student {row['id']}: {e}")
            errors += 1

    summary = {
        "date": target_date.isoformat(),
        "students": len(students),
        "cards_queued": total_queued,
        "errors": errors,
    }
    logger.info(f"Queue build complete: {summary}")
    return summary
