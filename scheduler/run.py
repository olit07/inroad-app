"""
CCC Backend — Full daily scheduler

06:00 UTC — scrape all sources, ingest new jobs
07:00 UTC — generate daily 3-card matches for all students

Usage:
    python scheduler/run.py            # run daemon
    python scheduler/run.py --once     # single immediate full run
    python scheduler/run.py --scrape   # scrape only
    python scheduler/run.py --cards    # cards only
"""
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

from config.settings     import DB_PATH
from db.database         import init_db
from pipeline.ingest     import run_all_scrapers, expire_past_closing
from pipeline.daily_cards import generate_all_students_cards


SCRAPE_HOUR = 6
CARDS_HOUR  = 7


def run_scrape_job():
    logger.info("─" * 50)
    logger.info("SCRAPE JOB starting")
    summaries = run_all_scrapers(DB_PATH)
    expired   = expire_past_closing(DB_PATH)
    new       = sum(s["jobs_new"] for s in summaries)
    errors    = sum(1 for s in summaries if s["status"] == "error")
    logger.info(f"SCRAPE JOB done — {new} new jobs, {errors} errors, {expired} expired")


def run_cards_job():
    logger.info("─" * 50)
    logger.info("CARDS JOB starting")
    generate_all_students_cards(DB_PATH)
    logger.info("CARDS JOB done")


def seconds_until(hour: int, minute: int = 0) -> float:
    now      = datetime.utcnow()
    target   = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_daemon():
    init_db(DB_PATH)
    logger.info(f"CCC Scheduler started — scrape@{SCRAPE_HOUR:02d}:00  cards@{CARDS_HOUR:02d}:00 UTC")

    stop = {"flag": False}
    def _handler(sig, frame):
        logger.info("Stopping scheduler...")
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT,  _handler)

    while not stop["flag"]:
        now  = datetime.utcnow()
        hour = now.hour

        # Determine which job to wait for next
        scrape_wait = seconds_until(SCRAPE_HOUR)
        cards_wait  = seconds_until(CARDS_HOUR)
        next_wait   = min(scrape_wait, cards_wait)

        logger.info(
            f"Next run in {next_wait/3600:.1f}h  "
            f"(scrape in {scrape_wait/3600:.1f}h, cards in {cards_wait/3600:.1f}h)"
        )

        elapsed = 0.0
        while elapsed < next_wait and not stop["flag"]:
            time.sleep(min(30, next_wait - elapsed))
            elapsed += 30

        if stop["flag"]:
            break

        now_h = datetime.utcnow().hour
        if now_h == SCRAPE_HOUR:
            try:
                run_scrape_job()
            except Exception as e:
                logger.error(f"Scrape job crashed: {e}", exc_info=True)
        elif now_h == CARDS_HOUR:
            try:
                run_cards_job()
            except Exception as e:
                logger.error(f"Cards job crashed: {e}", exc_info=True)

    logger.info("Scheduler stopped")


if __name__ == "__main__":
    args = sys.argv[1:]
    init_db(DB_PATH)

    if "--once" in args:
        run_scrape_job()
        run_cards_job()
    elif "--scrape" in args:
        run_scrape_job()
    elif "--cards" in args:
        run_cards_job()
    else:
        run_daemon()
