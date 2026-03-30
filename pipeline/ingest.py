"""
CCC Backend — Ingestion pipeline

Orchestrates all scrapers → normalises → deduplicates → writes to DB.
Called by the scheduler daily, or via the CLI for manual runs.
"""
import time
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings    import DB_PATH, MAX_JOB_AGE_DAYS
from db.database        import db_conn, init_db, upsert_job, log_scrape_run, db_stats

logger = logging.getLogger(__name__)


def run_single_scraper(scraper, db_path=DB_PATH) -> dict:
    """
    Run one scraper and write results to DB.
    Returns a summary dict.
    """
    source_id   = scraper.source_id
    source_name = scraper.source_name
    start       = time.time()

    logger.info(f"[{source_id}] Starting scrape...")

    jobs_found   = 0
    jobs_new     = 0
    jobs_updated = 0
    status       = "ok"
    error_msg    = ""

    try:
        raw_jobs = scraper.run()
        jobs_found = len(raw_jobs)

        with db_conn(db_path) as conn:
            for job in raw_jobs:
                try:
                    _, is_new = upsert_job(conn, job)
                    if is_new:
                        jobs_new += 1
                    else:
                        jobs_updated += 1
                except Exception as e:
                    logger.debug(f"[{source_id}] upsert error: {e}")
                    continue

        if jobs_found == 0:
            status = "empty"

    except Exception as e:
        status    = "error"
        error_msg = str(e)
        logger.error(f"[{source_id}] Pipeline error: {e}", exc_info=True)

    duration = round(time.time() - start, 2)

    with db_conn(db_path) as conn:
        log_scrape_run(
            conn, source_id, jobs_found, jobs_new, jobs_updated,
            status, error_msg, duration
        )

    summary = {
        "source_id":    source_id,
        "source_name":  source_name,
        "jobs_found":   jobs_found,
        "jobs_new":     jobs_new,
        "jobs_updated": jobs_updated,
        "status":       status,
        "duration_secs": duration,
        "error":        error_msg,
    }

    icon = "✓" if status == "ok" else ("⚠" if status == "empty" else "✗")
    logger.info(
        f"{icon} [{source_id}] done in {duration}s — "
        f"{jobs_found} found, {jobs_new} new, {jobs_updated} updated"
    )
    return summary


def run_all_scrapers(db_path=DB_PATH, source_ids: list[str] | None = None) -> list[dict]:
    """
    Run all (or a subset of) scrapers sequentially.
    Returns list of per-scraper summaries.
    """
    from scrapers import get_all_scrapers, get_scraper_by_id

    init_db(db_path)

    if source_ids:
        scrapers = [get_scraper_by_id(sid) for sid in source_ids]
    else:
        scrapers = get_all_scrapers()

    logger.info(f"Starting ingestion run — {len(scrapers)} scrapers")
    summaries = []

    for scraper in scrapers:
        summary = run_single_scraper(scraper, db_path)
        summaries.append(summary)

    # Expire stale jobs
    expired = expire_stale_jobs(db_path)
    logger.info(f"Expired {expired} stale jobs (>{MAX_JOB_AGE_DAYS} days unseen)")

    # Log totals
    total_new     = sum(s["jobs_new"]     for s in summaries)
    total_found   = sum(s["jobs_found"]   for s in summaries)
    total_errors  = sum(1 for s in summaries if s["status"] == "error")

    logger.info(
        f"Ingestion complete — "
        f"{total_found} total found, {total_new} new, "
        f"{total_errors} errors across {len(scrapers)} scrapers"
    )
    return summaries


def expire_stale_jobs(db_path=DB_PATH) -> int:
    """Mark jobs as inactive if not seen in MAX_JOB_AGE_DAYS days."""
    cutoff = (datetime.utcnow() - timedelta(days=MAX_JOB_AGE_DAYS)).strftime("%Y-%m-%d")
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET is_active=0 WHERE last_seen_at < ? AND is_active=1",
            (cutoff,),
        )
        return cur.rowcount


def expire_past_closing(db_path=DB_PATH) -> int:
    """Mark jobs as inactive if their closing date has passed."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """UPDATE jobs SET is_active=0
               WHERE closing_date != '' AND closing_date < ?
               AND is_active=1""",
            (today,),
        )
        return cur.rowcount
