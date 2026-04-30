"""
inroad — Daily pipeline scheduler

06:00 UTC every day:
  1. Scrape jobs from Trackr
  2. Build leads (Groq email format lookup for new companies only)
  3. Generate 3 daily cards for every student

Usage:
    python scheduler/run.py              # run daemon (fires at 06:00 UTC daily)
    python scheduler/run.py --once       # run full pipeline immediately
    python scheduler/run.py --trackr     # run Trackr scrape + leads check immediately
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

SCRAPE_HOUR    = 5   # scrape + leads run at 05:00 UTC
PIPELINE_HOUR  = 6   # cards + notify run at 06:00 UTC
SCRAPE_ENABLED = os.environ.get("SCRAPE_ENABLED", "false").lower() == "true"
SCRAPE_INTERVAL_HOURS = 72   # scrape + leads refresh every 72 hours

# Trackr runs 3x daily at these UTC (hour, minute) slots
TRACKR_SCHEDULE = [(5, 45), (10, 45), (16, 45)]
TRACKR_LEADS_THRESHOLD = 25   # run lead builder if company has fewer leads than this


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
    n = build_leads(top_n=50)
    logger.info(f"LEADS JOB done — {n} leads upserted")


def run_trackr_scrape_job() -> tuple[int, set]:
    """Run all Trackr scrapers. Returns (new_job_count, new_companies)."""
    logger.info("─" * 60)
    logger.info("TRACKR SCRAPE JOB starting")
    from scrapers.trackr import (
        TrackrSummerInternshipsScraper, TrackrSpringWeeksScraper,
        TrackrOffCycleScraper, TrackrIndustrialPlacementsScraper,
        TrackrGradProgrammesScraper, TrackrEventsScraper, TrackrScraper,
    )
    from pipeline.ingest import run_single_scraper
    scrapers = [
        TrackrSummerInternshipsScraper(),
        TrackrSpringWeeksScraper(),
        TrackrOffCycleScraper(),
        TrackrIndustrialPlacementsScraper(),
        TrackrGradProgrammesScraper(),
        TrackrEventsScraper(),
        TrackrScraper(),
    ]
    new_total = 0
    all_new_companies: set = set()
    for scraper in scrapers:
        try:
            summary = run_single_scraper(scraper)
            new_total += summary.get("jobs_new", 0)
            all_new_companies.update(summary.get("new_companies", set()))
        except Exception as e:
            logger.error(f"Trackr scraper {scraper.source_id} crashed: {e}", exc_info=True)
    logger.info(f"TRACKR SCRAPE JOB done — {new_total} new jobs from {len(all_new_companies)} new companies")
    return new_total, all_new_companies


def run_trackr_leads_check_job(companies: set | None = None):
    """
    For each company in `companies` that has fewer than 25 leads, run the lead builder.
    When called without arguments (e.g. manual --trackr run), falls back to checking
    all active Trackr companies in the DB that are short on leads.
    """
    logger.info("─" * 60)
    logger.info("TRACKR LEADS CHECK starting")
    from db.database import fetchall, USE_POSTGRES
    from pipeline.lead_builder import build_leads

    if companies is not None and len(companies) == 0:
        logger.info("TRACKR LEADS CHECK — no new companies this run, skipping")
        return

    if companies:
        # Check only the specific companies added in this scrape
        company_list = [c for c in companies if c]
        if USE_POSTGRES:
            placeholders = ", ".join(["%s"] * len(company_list))
            sql = f"""
                SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
                FROM jobs j
                LEFT JOIN leads l ON lower(l.company) = lower(j.company)
                WHERE lower(j.company) IN ({placeholders})
                  AND j.company IS NOT NULL AND j.company != ''
                GROUP BY j.company
                HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
                ORDER BY COUNT(DISTINCT l.id) ASC
            """
        else:
            placeholders = ", ".join(["?"] * len(company_list))
            sql = f"""
                SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
                FROM jobs j
                LEFT JOIN leads l ON lower(l.company) = lower(j.company)
                WHERE lower(j.company) IN ({placeholders})
                  AND j.company IS NOT NULL AND j.company != ''
                GROUP BY lower(j.company)
                HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
                ORDER BY COUNT(DISTINCT l.id) ASC
            """
        rows = fetchall(sql, [c.lower() for c in company_list])
    else:
        # Manual / fallback: check all active Trackr companies
        if USE_POSTGRES:
            sql = f"""
                SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
                FROM jobs j
                LEFT JOIN leads l ON lower(l.company) = lower(j.company)
                WHERE j.source LIKE 'trackr%%'
                  AND j.company IS NOT NULL AND j.company != ''
                GROUP BY j.company
                HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
                ORDER BY COUNT(DISTINCT l.id) ASC
            """
        else:
            sql = f"""
                SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
                FROM jobs j
                LEFT JOIN leads l ON lower(l.company) = lower(j.company)
                WHERE j.source LIKE 'trackr%'
                  AND j.company IS NOT NULL AND j.company != ''
                GROUP BY lower(j.company)
                HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
                ORDER BY COUNT(DISTINCT l.id) ASC
            """
        rows = fetchall(sql)

    if not rows:
        logger.info("TRACKR LEADS CHECK — all companies have >= 25 leads")
        return

    logger.info(f"TRACKR LEADS CHECK — {len(rows)} companies need more leads")
    for row in rows:
        company = (row.get("company") or "").strip()
        if not company:
            continue
        lead_count = row.get("lead_count", 0)
        logger.info(f"  Lead builder: {company} ({lead_count} leads)")
        try:
            build_leads(company_filter=company, top_n=0)
        except Exception as e:
            logger.error(f"Lead builder failed for {company}: {e}", exc_info=True)
    logger.info("TRACKR LEADS CHECK done")


def run_trackr_pipeline():
    """Scrape Trackr, then ensure every newly added company has >= 25 leads."""
    logger.info("=" * 60)
    logger.info("TRACKR PIPELINE starting")
    start = time.time()
    new_companies: set = set()
    try:
        _, new_companies = run_trackr_scrape_job()
    except Exception as e:
        logger.error(f"Trackr scrape crashed: {e}", exc_info=True)
    try:
        run_trackr_leads_check_job(companies=new_companies)
    except Exception as e:
        logger.error(f"Trackr leads check crashed: {e}", exc_info=True)
    elapsed = round((time.time() - start) / 60, 1)
    logger.info(f"TRACKR PIPELINE done in {elapsed} min")


def run_cards_job():
    logger.info("─" * 60)
    logger.info("CARDS JOB starting")
    from db.database import fetchall, get_card_count_today
    students = fetchall(
        "SELECT id, email FROM students WHERE deactivated_at IS NULL"
    )
    generate_all_students_cards(DB_PATH)
    zero = []
    for s in students:
        n = get_card_count_today(s["id"])
        if n == 0:
            zero.append(s["email"])
        else:
            logger.info(f"  {s['email']}: {n} cards generated today")
    if zero:
        logger.warning(f"CARDS JOB — 0 cards for {len(zero)} student(s): {zero}")
    logger.info("CARDS JOB done")


def run_notify_job():
    logger.info("─" * 60)
    logger.info("NOTIFY JOB starting")
    from config.settings import SEND_DAILY_EMAILS
    if not SEND_DAILY_EMAILS:
        logger.info("NOTIFY JOB skipped — SEND_DAILY_EMAILS flag is off")
        return
    from db.database import fetchall, get_card_count_today, execute
    from utils.notifications import send_daily_matches_ready
    today_str     = datetime.utcnow().strftime("%Y-%m-%d")
    today_is_monday = datetime.utcnow().weekday() == 0
    students = fetchall(
        "SELECT id, email, name, notify_frequency, notify_sent_date, industries FROM students "
        "WHERE notify_matches = TRUE AND deactivated_at IS NULL"
    )
    sent = 0
    for s in students:
        if s["notify_frequency"] == "weekly" and not today_is_monday:
            continue
        # Dedup: skip if we already sent a notification email today
        sent_date = s.get("notify_sent_date")
        if sent_date and str(sent_date)[:10] == today_str:
            logger.info(f"Skipping {s['email']} — notification already sent today")
            continue
        n_cards = get_card_count_today(s["id"])
        if n_cards == 0:
            industries = s.get("industries") or "[]"
            logger.warning(f"Skipping email for {s['email']} — 0 cards today (industries: {industries})")
            continue
        try:
            send_daily_matches_ready(dict(s), n_cards=n_cards)
            execute(
                "UPDATE students SET notify_sent_date = ? WHERE id = ?",
                (today_str, s["id"])
            )
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


def _next_trackr_datetime() -> datetime:
    """Return the next UTC datetime matching one of the TRACKR_SCHEDULE slots."""
    now = datetime.utcnow()
    candidates = []
    for h, m in TRACKR_SCHEDULE:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        candidates.append(t)
    return min(candidates)


def run_daemon():
    init_db()
    trackr_slots = ", ".join(f"{h:02d}:{m:02d}" for h, m in TRACKR_SCHEDULE)
    logger.info(
        f"inroad scheduler started — "
        f"Trackr pipeline at {trackr_slots} UTC, "
        f"scrape+leads every {SCRAPE_INTERVAL_HOURS}h at {SCRAPE_HOUR:02d}:00 UTC, "
        f"cards+notify daily at {PIPELINE_HOUR:02d}:00 UTC"
    )

    stop = {"flag": False}

    def _handler(sig, frame):
        logger.info("Stopping scheduler...")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT,  _handler)

    # Schedule first scrape for tomorrow at 05:00 UTC
    next_scrape  = datetime.utcnow().replace(
        hour=SCRAPE_HOUR, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    next_trackr  = _next_trackr_datetime()
    logger.info(f"First scrape+leads run scheduled for {next_scrape.strftime('%Y-%m-%d %H:%M')} UTC")
    logger.info(f"First Trackr pipeline run scheduled for {next_trackr.strftime('%Y-%m-%d %H:%M')} UTC")

    while not stop["flag"]:
        wait_scrape   = seconds_until(SCRAPE_HOUR)
        wait_pipeline = seconds_until(PIPELINE_HOUR)
        now           = datetime.utcnow()
        wait_trackr   = max((next_trackr - now).total_seconds(), 0)
        wait = min(wait_scrape, wait_pipeline, wait_trackr)
        logger.info(
            f"Next wake in {wait/3600:.1f}h — "
            f"Trackr at {next_trackr.strftime('%H:%M')} UTC, "
            f"scrape at {SCRAPE_HOUR:02d}:00 UTC, cards at {PIPELINE_HOUR:02d}:00 UTC"
        )

        elapsed = 0.0
        while elapsed < wait and not stop["flag"]:
            time.sleep(min(30, wait - elapsed))
            elapsed += 30

        if stop["flag"]:
            break

        now = datetime.utcnow()

        # Trackr pipeline — 3x daily at TRACKR_SCHEDULE slots
        if now >= next_trackr:
            try:
                run_trackr_pipeline()
            except Exception as e:
                logger.error(f"Trackr pipeline crashed: {e}", exc_info=True)
            next_trackr = _next_trackr_datetime()
            logger.info(f"Next Trackr pipeline scheduled for {next_trackr.strftime('%Y-%m-%d %H:%M')} UTC")

        # 05:00 UTC — full scrape + leads (every 72 hours)
        elif now.hour == SCRAPE_HOUR and now >= next_scrape:
            try:
                run_scrape_job()
            except Exception as e:
                logger.error(f"Scrape job crashed: {e}", exc_info=True)
            try:
                run_leads_job()
            except Exception as e:
                logger.error(f"Leads job crashed: {e}", exc_info=True)
            next_scrape = now + timedelta(hours=SCRAPE_INTERVAL_HOURS)
            logger.info(f"Next scrape+leads scheduled for {next_scrape.strftime('%Y-%m-%d %H:%M')} UTC")

        # 06:00 UTC — cards + notify (every day)
        elif now.hour == PIPELINE_HOUR:
            try:
                run_cards_job()
            except Exception as e:
                logger.error(f"Cards job crashed: {e}", exc_info=True)
            try:
                run_notify_job()
            except Exception as e:
                logger.error(f"Notify job crashed: {e}", exc_info=True)

    logger.info("Scheduler stopped")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    init_db()

    if "--once" in args:
        run_full_pipeline()
    elif "--trackr" in args:
        run_trackr_pipeline()
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
