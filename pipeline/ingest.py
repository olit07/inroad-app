"""
inroad Backend — Ingestion pipeline

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
from config.settings    import DB_PATH, FRESHNESS_DECAY_DAYS as MAX_JOB_AGE_DAYS
from db.database        import db_conn, init_db, upsert_job, log_scrape_run, db_stats, USE_POSTGRES

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

    jobs_found    = 0
    jobs_new      = 0
    jobs_updated  = 0
    new_companies: set = set()
    status        = "ok"
    error_msg     = ""

    try:
        raw_jobs = scraper.run()

        # Trackr already filters expired listings via closingDate — don't apply
        # a date cutoff or long-running programmes (off-cycle, spring weeks etc.)
        # get silently dropped and expire from the DB within 14 days.
        is_trackr = source_id.startswith("trackr")
        if not is_trackr:
            cutoff = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
            fresh_jobs = [j for j in raw_jobs if (j.get("posted_date") or j.get("opening_date") or "9999") >= cutoff]
            stale      = len(raw_jobs) - len(fresh_jobs)
            if stale:
                logger.info(f"[{source_id}] Skipped {stale} jobs older than 30 days")
            raw_jobs = fresh_jobs
        jobs_found = len(raw_jobs)

        with db_conn(db_path) as conn:
            for job in raw_jobs:
                try:
                    if USE_POSTGRES:
                        conn.cursor().execute("SAVEPOINT _sp")
                    _, is_new = upsert_job(conn, job)
                    if USE_POSTGRES:
                        conn.cursor().execute("RELEASE SAVEPOINT _sp")
                    if is_new:
                        jobs_new += 1
                        company = (job.get("company_name") or "").strip()
                        if company:
                            new_companies.add(company)
                    else:
                        jobs_updated += 1
                except Exception as e:
                    if USE_POSTGRES:
                        try:
                            conn.cursor().execute("ROLLBACK TO SAVEPOINT _sp")
                        except Exception:
                            pass
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
            conn, source_id, source_name, status,
            jobs_found, jobs_new, error_msg, duration
        )

    summary = {
        "source_id":     source_id,
        "source_name":   source_name,
        "jobs_found":    jobs_found,
        "jobs_new":      jobs_new,
        "jobs_updated":  jobs_updated,
        "new_companies": new_companies,
        "status":        status,
        "duration_secs": duration,
        "error":         error_msg,
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

    init_db()

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


def expire_stale_jobs(db_path=DB_PATH, max_age_days: int = 14) -> int:
    """
    Delete jobs not seen (created_at / upserted) in the last max_age_days.
    opening_date is intentionally excluded: many long-running programmes (off-cycle,
    graduate, etc.) have old opening dates but remain valid for months.
    Returns the number of rows deleted.
    """
    from db.database import USE_POSTGRES, execute as db_execute
    if USE_POSTGRES:
        sql = (
            f"DELETE FROM jobs WHERE id NOT IN (SELECT DISTINCT job_id FROM matches) "
            f"AND created_at < NOW() - INTERVAL '{max_age_days} days'"
        )
    else:
        sql = (
            f"DELETE FROM jobs WHERE id NOT IN (SELECT DISTINCT job_id FROM matches) "
            f"AND created_at < datetime('now', '-{max_age_days} days')"
        )
    deleted = db_execute(sql) or 0
    return deleted


def expire_past_closing(db_path=DB_PATH) -> int:
    """No-op — closing date filtering handled at query time."""
    return 0
