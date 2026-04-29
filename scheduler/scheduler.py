"""
inroad Backend — Scheduler

Runs the full ingestion pipeline daily at 06:00 UTC.
Also runs closing-date expiry check.

Usage:
    python scheduler/scheduler.py          # run scheduler daemon
    python scheduler/scheduler.py --once   # single immediate run then exit
"""
import sys
import logging
import time
import signal
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.ingest import run_all_scrapers, expire_past_closing
from config.settings import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

RUN_HOUR   = 6   # UTC hour to run daily
RUN_MINUTE = 0


def run_daily_job():
    logger.info("=" * 60)
    logger.info("Daily ingestion job started")
    logger.info("=" * 60)

    summaries = run_all_scrapers(DB_PATH)
    expired   = expire_past_closing(DB_PATH)

    ok      = sum(1 for s in summaries if s["status"] == "ok")
    empty   = sum(1 for s in summaries if s["status"] == "empty")
    errors  = sum(1 for s in summaries if s["status"] == "error")
    new     = sum(s["jobs_new"] for s in summaries)

    logger.info(
        f"Daily job complete — {new} new jobs | "
        f"{ok} scrapers OK | {empty} empty | {errors} errors | "
        f"{expired} past-closing expired"
    )
    return summaries


def _seconds_until_next_run() -> float:
    """Return seconds until next RUN_HOUR:RUN_MINUTE UTC."""
    now  = datetime.utcnow()
    next_run = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if next_run <= now:
        from datetime import timedelta
        next_run += timedelta(days=1)
    delta = (next_run - now).total_seconds()
    logger.info(
        f"Next run scheduled for {next_run.strftime('%Y-%m-%d %H:%M')} UTC "
        f"({delta/3600:.1f} hours from now)"
    )
    return delta


def run_daemon():
    """Run the scheduler forever — sleep until next run time, then execute."""
    logger.info("inroad Scheduler starting — will run daily at %02d:%02d UTC", RUN_HOUR, RUN_MINUTE)

    # Graceful shutdown on SIGTERM/SIGINT
    stop = {"flag": False}
    def _handler(sig, frame):
        logger.info("Shutdown signal received")
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT,  _handler)

    while not stop["flag"]:
        wait_secs = _seconds_until_next_run()
        # Sleep in small chunks so we can react to stop signal quickly
        elapsed = 0.0
        while elapsed < wait_secs and not stop["flag"]:
            time.sleep(min(30, wait_secs - elapsed))
            elapsed += 30

        if not stop["flag"]:
            try:
                run_daily_job()
            except Exception as e:
                logger.error(f"Daily job failed: {e}", exc_info=True)

    logger.info("Scheduler stopped")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_daily_job()
    else:
        run_daemon()
