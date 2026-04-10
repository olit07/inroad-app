"""
inroad — Daily pipeline scheduler

06:00 UTC every day:
  1. Scrape jobs from Trackr
  2. Build leads (Groq email format lookup for new companies only)
  3. Generate 3 daily cards for every student

Usage:
    python scheduler/run.py              # run daemon (fires at 06:00 UTC daily)
    python scheduler/run.py --once       # run full pipeline immediately
    python scheduler/run.py --scrape     # scrape only
    python scheduler/run.py --leads      # lead builder only
    python scheduler/run.py --cards      # cards only
    python scheduler/run.py --email-formats  # print known company email formats
"""
import os
import sys
import time
import signal
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

from config.settings      import DB_PATH
from db.database          import init_db
from pipeline.ingest      import run_all_scrapers, expire_past_closing
from pipeline.daily_cards import generate_all_students_cards

PIPELINE_HOUR  = 6
SCRAPE_ENABLED = os.environ.get("SCRAPE_ENABLED", "false").lower() == "true"


# ── Individual jobs ───────────────────────────────────────────────────────────

def run_scrape_job():
    if not SCRAPE_ENABLED:
        logger.info("SCRAPE JOB skipped — SCRAPE_ENABLED is not set to true")
        return
    logger.info("─" * 60)
    logger.info("SCRAPE JOB starting")
    summaries = run_all_scrapers(DB_PATH)
    expire_past_closing(DB_PATH)
    new    = sum(s["jobs_new"] for s in summaries)
    errors = sum(1 for s in summaries if s["status"] == "error")
    logger.info(f"SCRAPE JOB done — {new} new jobs, {errors} errors")


def run_leads_job():
    from pipeline.lead_builder import build_leads
    logger.info("─" * 60)
    logger.info("LEADS JOB starting")
    n = build_leads()
    logger.info(f"LEADS JOB done — {n} leads upserted")


def run_cards_job():
    logger.info("─" * 60)
    logger.info("CARDS JOB starting")
    generate_all_students_cards(DB_PATH)
    logger.info("CARDS JOB done")


def run_notify_job():
    logger.info("─" * 60)
    logger.info("NOTIFY JOB starting")
    from db.database import fetchall
    from utils.notifications import send_daily_matches_ready
    today_is_monday = datetime.utcnow().weekday() == 0
    students = fetchall(
        "SELECT id, email, name, notify_frequency FROM students "
        "WHERE notify_matches = TRUE AND deactivated_at IS NULL"
    )
    sent = 0
    for s in students:
        if s["notify_frequency"] == "weekly" and not today_is_monday:
            continue
        try:
            send_daily_matches_ready(dict(s))
            sent += 1
        except Exception as e:
            logger.warning(f"Notify failed for {s['email']}: {e}")
    logger.info(f"NOTIFY JOB done — {sent} emails sent")


def run_full_pipeline():
    """Run the complete daily pipeline: scrape → leads → cards → notify."""
    logger.info("=" * 60)
    logger.info("FULL PIPELINE starting")
    start = time.time()
    try:
        run_scrape_job()
    except Exception as e:
        logger.error(f"Scrape job crashed: {e}", exc_info=True)
    try:
        run_leads_job()
    except Exception as e:
        logger.error(f"Leads job crashed: {e}", exc_info=True)
    try:
        run_cards_job()
    except Exception as e:
        logger.error(f"Cards job crashed: {e}", exc_info=True)
    try:
        run_notify_job()
    except Exception as e:
        logger.error(f"Notify job crashed: {e}", exc_info=True)
    elapsed = round((time.time() - start) / 60, 1)
    logger.info(f"FULL PIPELINE done in {elapsed} min")


# ── Daemon ────────────────────────────────────────────────────────────────────

def seconds_until(hour: int, minute: int = 0) -> float:
    now    = datetime.utcnow()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_daemon():
    init_db()
    logger.info(f"inroad scheduler started — pipeline fires daily at {PIPELINE_HOUR:02d}:00 UTC")

    stop = {"flag": False}

    def _handler(sig, frame):
        logger.info("Stopping scheduler...")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT,  _handler)

    while not stop["flag"]:
        wait = seconds_until(PIPELINE_HOUR)
        logger.info(f"Next pipeline run in {wait/3600:.1f}h (at {PIPELINE_HOUR:02d}:00 UTC)")

        elapsed = 0.0
        while elapsed < wait and not stop["flag"]:
            time.sleep(min(30, wait - elapsed))
            elapsed += 30

        if stop["flag"]:
            break

        if datetime.utcnow().hour == PIPELINE_HOUR:
            try:
                run_full_pipeline()
            except Exception as e:
                logger.error(f"Pipeline crashed: {e}", exc_info=True)

    logger.info("Scheduler stopped")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    init_db()

    if "--once" in args:
        run_full_pipeline()
    elif "--scrape" in args:
        run_scrape_job()
    elif "--leads" in args:
        run_leads_job()
    elif "--cards" in args:
        run_cards_job()
    elif "--email-formats" in args:
        from db.database import fetchall
        rows = fetchall("SELECT company, fmt_code, domain, source, created_at FROM company_email_formats ORDER BY company")
        print(f"\n{len(rows)} company email formats stored:\n")
        for r in rows:
            print(f"  {r['company']:40s}  {r['fmt_code']:6s}  {r['domain']:35s}  [{r['source']}]")
    elif "--notify" in args:
        run_notify_job()
    elif "--fix-emails" in args:
        from pipeline.lead_builder import fix_ats_email_formats
        n = fix_ats_email_formats()
        print(f"Fixed {n} ATS email format entries")
    else:
        run_daemon()
