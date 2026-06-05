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
    python scheduler/run.py --wttj       # run WTTJ pipeline immediately
    python scheduler/run.py --jorb       # run Jorb.ai scrape + leads immediately
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

# Trackr runs 8x daily at these UTC (hour, minute) slots
TRACKR_SCHEDULE = [(5,0),(7,10),(9,15),(11,20),(13,25),(15,30),(17,45),(20,0)]
# WTTJ runs 4x daily so restarts never cause a full-day gap
WTTJ_SCHEDULE   = [(5,5),(11,5),(17,5),(23,5)]
# Jorb runs 8x daily, offset ~30 min from Trackr slots
JORB_SCHEDULE   = [(1,30),(4,30),(7,30),(10,30),(13,30),(16,30),(19,30),(22,30)]
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
        TrackrEA27SummerInternshipsScraper, TrackrEA27SpringWeeksScraper,
        TrackrEA27OffCycleScraper, TrackrEA27IndustrialPlacementsScraper,
        TrackrEA27GradProgrammesScraper, TrackrEA27EventsScraper,
    )
    from pipeline.ingest import run_single_scraper
    scrapers = [
        TrackrSummerInternshipsScraper(),
        TrackrSpringWeeksScraper(),
        TrackrOffCycleScraper(),
        TrackrIndustrialPlacementsScraper(),
        TrackrGradProgrammesScraper(),
        TrackrEventsScraper(),
        TrackrEA27SummerInternshipsScraper(),
        TrackrEA27SpringWeeksScraper(),
        TrackrEA27OffCycleScraper(),
        TrackrEA27IndustrialPlacementsScraper(),
        TrackrEA27GradProgrammesScraper(),
        TrackrEA27EventsScraper(),
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


def run_trackr_leads_check_job(companies: set | None = None, max_per_run: int = 50):
    """
    Build leads for Trackr companies. Two modes:
    - companies provided: targeted run for those specific companies (new listings)
    - companies=None: general sweep of all underserved companies (< threshold leads)

    max_per_run: cap companies processed per call to limit API usage (default 50).
    """
    logger.info("─" * 60)
    logger.info("TRACKR LEADS CHECK starting")
    from db.database import fetchall, USE_POSTGRES
    from pipeline.lead_builder import build_leads

    # Targeted mode: run immediately for newly-added companies
    if companies:
        company_list = sorted(c.strip() for c in companies if c and c.strip())[:max_per_run]
        logger.info(f"TRACKR LEADS CHECK — targeted run for {len(company_list)} new companies")
        for company in company_list:
            logger.info(f"  Lead builder (new): {company}")
            try:
                build_leads(company_filter=company, top_n=0)
            except Exception as e:
                if "SERPER_CREDITS_EXHAUSTED" in str(e):
                    logger.critical("🚨 SERPER CREDITS EXHAUSTED — stopping leads check early")
                    return
                logger.error(f"Lead builder failed for {company}: {e}", exc_info=True)
        logger.info("TRACKR LEADS CHECK done (targeted)")
        return

    # General sweep mode
    limit_clause = f"LIMIT {max_per_run}" if max_per_run > 0 else ""

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
            {limit_clause}
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
            {limit_clause}
        """
    rows = fetchall(sql)

    if not rows:
        logger.info("TRACKR LEADS CHECK — all companies have >= 25 leads")
        return

    logger.info(f"TRACKR LEADS CHECK — building leads for {len(rows)} companies (capped at {max_per_run})")
    for row in rows:
        company = (row.get("company") or "").strip()
        if not company:
            continue
        lead_count = row.get("lead_count", 0)
        logger.info(f"  Lead builder: {company} ({lead_count} leads)")
        try:
            build_leads(company_filter=company, top_n=0)
        except Exception as e:
            if "SERPER_CREDITS_EXHAUSTED" in str(e):
                logger.critical("🚨 SERPER CREDITS EXHAUSTED — stopping leads check early")
                break
            logger.error(f"Lead builder failed for {company}: {e}", exc_info=True)
    logger.info("TRACKR LEADS CHECK done")


def run_trackr_pipeline():
    """Scrape Trackr, build leads for new companies immediately, then sweep underserved."""
    logger.info("=" * 60)
    logger.info("TRACKR PIPELINE starting")
    start = time.time()
    new_companies: set = set()
    try:
        _new_total, new_companies = run_trackr_scrape_job()
    except Exception as e:
        logger.error(f"Trackr scrape crashed: {e}", exc_info=True)
    # Targeted pass for newly-added companies first
    if new_companies:
        try:
            run_trackr_leads_check_job(companies=new_companies, max_per_run=50)
        except Exception as e:
            logger.error(f"Trackr targeted leads check crashed: {e}", exc_info=True)
    # General sweep for any remaining underserved companies
    try:
        run_trackr_leads_check_job(max_per_run=50)
    except Exception as e:
        logger.error(f"Trackr leads check crashed: {e}", exc_info=True)
    elapsed = round((time.time() - start) / 60, 1)
    logger.info(f"TRACKR PIPELINE done in {elapsed} min")


def run_wttj_leads_check_job(max_per_run: int = 50):
    """
    For each WTTJ company with fewer than TRACKR_LEADS_THRESHOLD leads, run the
    lead builder. Mirrors run_trackr_leads_check_job but for source = 'wttj'.
    """
    logger.info("─" * 60)
    logger.info("WTTJ LEADS CHECK starting")
    from db.database import fetchall, USE_POSTGRES
    from pipeline.lead_builder import build_leads

    limit_clause = f"LIMIT {max_per_run}" if max_per_run > 0 else ""

    if USE_POSTGRES:
        sql = f"""
            SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
            FROM jobs j
            LEFT JOIN leads l ON lower(l.company) = lower(j.company)
            WHERE j.source = 'wttj'
              AND j.company IS NOT NULL AND j.company != ''
            GROUP BY j.company
            HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
            ORDER BY COUNT(DISTINCT l.id) ASC
            {limit_clause}
        """
    else:
        sql = f"""
            SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
            FROM jobs j
            LEFT JOIN leads l ON lower(l.company) = lower(j.company)
            WHERE j.source = 'wttj'
              AND j.company IS NOT NULL AND j.company != ''
            GROUP BY lower(j.company)
            HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
            ORDER BY COUNT(DISTINCT l.id) ASC
            {limit_clause}
        """
    rows = fetchall(sql)

    if not rows:
        logger.info("WTTJ LEADS CHECK — all companies have >= 25 leads")
        return

    logger.info(f"WTTJ LEADS CHECK — building leads for {len(rows)} companies (capped at {max_per_run})")
    for row in rows:
        company = (row.get("company") or "").strip()
        if not company:
            continue
        lead_count = row.get("lead_count", 0)
        logger.info(f"  Lead builder: {company} ({lead_count} leads)")
        try:
            build_leads(company_filter=company, top_n=0)
        except Exception as e:
            if "SERPER_CREDITS_EXHAUSTED" in str(e):
                logger.critical("🚨 SERPER CREDITS EXHAUSTED — stopping WTTJ leads check early")
                break
            logger.error(f"Lead builder failed for {company}: {e}", exc_info=True)
    logger.info("WTTJ LEADS CHECK done")


def run_wttj_job():
    """Scrape WTTJ internship listings and upsert into the DB. Runs daily at 05:00 UTC."""
    logger.info("─" * 60)
    logger.info("WTTJ SCRAPE JOB starting")
    from scripts.wttj_internship_local import scrape_jobs
    from db.database import get_conn, upsert_job

    try:
        jobs = scrape_jobs()
    except Exception as e:
        logger.error(f"WTTJ scrape failed: {e}", exc_info=True)
        return

    if not jobs:
        logger.info("WTTJ SCRAPE JOB — no jobs returned")
        return

    new_count = updated_count = 0
    with get_conn() as conn:
        # Remove WTTJ jobs older than 72h so the DB stays fresh; preserve any that have matches
        from db.database import _exec
        _exec(conn,
            "DELETE FROM jobs WHERE source = 'wttj' "
            "AND opening_date < ? "
            "AND id NOT IN (SELECT DISTINCT job_id FROM matches WHERE job_id IS NOT NULL)",
            ((datetime.utcnow() - timedelta(hours=72)).strftime("%Y-%m-%d"),)
        )
        for j in jobs:
            job_dict = {
                "company_name":        j.get("company_name", ""),
                "title":               j.get("title", ""),
                "url":                 j.get("job_url", ""),
                "industries":          [j.get("sector", "Other")],
                "region":              j.get("region", ""),
                "source_id":           "wttj",
                "source_name":         "Welcome to the Jungle",
                "company_size":        j.get("company_size", ""),
                "posted_date":         j.get("date_posted", ""),
                "opening_date":        j.get("date_posted", ""),
                "role_type":           "internship_grad",
                "programme_type":      j.get("programme_type", ""),
                "logo_url":            j.get("logo_url", ""),
                "wttj_url":            j.get("wttj_url", ""),
                "company_url":         j.get("company_url", ""),
                "recruitment_process": j.get("recruitment_process", ""),
                "location":            j.get("location", ""),
                "country":             j.get("country", ""),
            }
            if not job_dict["company_name"] or not job_dict["title"]:
                continue
            try:
                _exec(conn, "SAVEPOINT _sp")
                _, is_new = upsert_job(conn, job_dict)
                _exec(conn, "RELEASE SAVEPOINT _sp")
                if is_new:
                    new_count += 1
                else:
                    updated_count += 1
            except Exception as e:
                try:
                    _exec(conn, "ROLLBACK TO SAVEPOINT _sp")
                except Exception:
                    pass
                logger.debug(f"WTTJ upsert error ({job_dict.get('company_name')}): {e}")

    logger.info(f"WTTJ SCRAPE JOB done — {new_count} new, {updated_count} updated, {len(jobs)} total")


def run_wttj_pipeline():
    """Scrape WTTJ, then build leads for any company with fewer than 25 leads."""
    logger.info("=" * 60)
    logger.info("WTTJ PIPELINE starting")
    start = time.time()
    try:
        run_wttj_job()
    except Exception as e:
        logger.error(f"WTTJ scrape crashed: {e}", exc_info=True)
    try:
        run_wttj_leads_check_job(max_per_run=50)
    except Exception as e:
        logger.error(f"WTTJ leads check crashed: {e}", exc_info=True)
    elapsed = round((time.time() - start) / 60, 1)
    logger.info(f"WTTJ PIPELINE done in {elapsed} min")


def run_jorb_job():
    """Scrape jorb.ai internship/grad listings and upsert into the DB.

    No cutoff — always ingest all current listings (~60 jobs). opening_date is
    derived from the ObjectID timestamp so the API freshness filter stays correct
    regardless of when we upsert. Dedup is by URL so re-runs are safe.
    """
    logger.info("─" * 60)
    logger.info("JORB SCRAPE JOB starting")
    from scrapers.jorb import JorbScraper
    from pipeline.ingest import run_single_scraper
    scraper = JorbScraper(cutoff_hours=None, sitemap_days_back=4)
    try:
        summary = run_single_scraper(scraper)
        logger.info(
            f"JORB SCRAPE JOB done — "
            f"{summary.get('jobs_found', 0)} found, "
            f"{summary.get('jobs_new', 0)} new, "
            f"{summary.get('jobs_updated', 0)} updated"
        )
        return summary.get("new_companies", set())
    except Exception as e:
        logger.error(f"Jorb scrape crashed: {e}", exc_info=True)
        return set()


def run_jorb_leads_check_job(companies: set | None = None, max_per_run: int = 50):
    """Build leads for jorb companies with fewer than TRACKR_LEADS_THRESHOLD leads."""
    logger.info("─" * 60)
    logger.info("JORB LEADS CHECK starting")
    from db.database import fetchall, USE_POSTGRES
    from pipeline.lead_builder import build_leads

    if companies:
        company_list = sorted(c.strip() for c in companies if c and c.strip())[:max_per_run]
        logger.info(f"JORB LEADS CHECK — targeted run for {len(company_list)} new companies")
        for company in company_list:
            logger.info(f"  Lead builder (new): {company}")
            try:
                build_leads(company_filter=company, top_n=0)
            except Exception as e:
                if "SERPER_CREDITS_EXHAUSTED" in str(e):
                    logger.critical("SERPER CREDITS EXHAUSTED — stopping jorb leads check early")
                    return
                logger.error(f"Lead builder failed for {company}: {e}", exc_info=True)
        logger.info("JORB LEADS CHECK done (targeted)")
        return

    limit_clause = f"LIMIT {max_per_run}" if max_per_run > 0 else ""
    if USE_POSTGRES:
        sql = f"""
            SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
            FROM jobs j
            LEFT JOIN leads l ON lower(l.company) = lower(j.company)
            WHERE j.source = 'jorb'
              AND j.company IS NOT NULL AND j.company != ''
            GROUP BY j.company
            HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
            ORDER BY COUNT(DISTINCT l.id) ASC
            {limit_clause}
        """
    else:
        sql = f"""
            SELECT j.company, COUNT(DISTINCT l.id) AS lead_count
            FROM jobs j
            LEFT JOIN leads l ON lower(l.company) = lower(j.company)
            WHERE j.source = 'jorb'
              AND j.company IS NOT NULL AND j.company != ''
            GROUP BY lower(j.company)
            HAVING COUNT(DISTINCT l.id) < {TRACKR_LEADS_THRESHOLD}
            ORDER BY COUNT(DISTINCT l.id) ASC
            {limit_clause}
        """
    rows = fetchall(sql)
    if not rows:
        logger.info("JORB LEADS CHECK — all companies have >= 25 leads")
        return

    logger.info(f"JORB LEADS CHECK — building leads for {len(rows)} companies (capped at {max_per_run})")
    for row in rows:
        company = (row.get("company") or "").strip()
        if not company:
            continue
        lead_count = row.get("lead_count", 0)
        logger.info(f"  Lead builder: {company} ({lead_count} leads)")
        try:
            build_leads(company_filter=company, top_n=0)
        except Exception as e:
            if "SERPER_CREDITS_EXHAUSTED" in str(e):
                logger.critical("SERPER CREDITS EXHAUSTED — stopping jorb leads check early")
                break
            logger.error(f"Lead builder failed for {company}: {e}", exc_info=True)
    logger.info("JORB LEADS CHECK done")


def run_jorb_pipeline():
    """Scrape jorb.ai, build leads for new companies immediately, then sweep underserved."""
    if os.environ.get("JORB_DISABLED", "").lower() == "true":
        logger.info("JORB PIPELINE skipped — JORB_DISABLED is set to true")
        return
    logger.info("=" * 60)
    logger.info("JORB PIPELINE starting")
    start = time.time()
    new_companies: set = set()
    try:
        new_companies = run_jorb_job()
    except Exception as e:
        logger.error(f"Jorb scrape crashed: {e}", exc_info=True)
    if new_companies:
        try:
            run_jorb_leads_check_job(companies=new_companies, max_per_run=50)
        except Exception as e:
            logger.error(f"Jorb targeted leads check crashed: {e}", exc_info=True)
    try:
        run_jorb_leads_check_job(max_per_run=50)
    except Exception as e:
        logger.error(f"Jorb leads check crashed: {e}", exc_info=True)
    elapsed = round((time.time() - start) / 60, 1)
    logger.info(f"JORB PIPELINE done in {elapsed} min")


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


def _next_wttj_datetime() -> datetime:
    """Return the next UTC datetime matching one of the WTTJ_SCHEDULE slots."""
    now = datetime.utcnow()
    candidates = []
    for h, m in WTTJ_SCHEDULE:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        candidates.append(t)
    return min(candidates)


def _next_jorb_datetime() -> datetime:
    """Return the next UTC datetime matching one of the JORB_SCHEDULE slots."""
    now = datetime.utcnow()
    candidates = []
    for h, m in JORB_SCHEDULE:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        candidates.append(t)
    return min(candidates)


def _backfill_jorb_urls_once():
    """One-time startup fix: replace jorb.ai apply URLs with direct company URLs."""
    import urllib.request
    import re as _re
    from db.database import fetchall, execute

    _RE = _re.compile(
        r'href="(https?://(?!(?:www\.)?jorb\.ai(?:/|$))[^"]{15,})"[^>]*target="_blank"',
        _re.IGNORECASE,
    )
    _HDRS = {"User-Agent": "Mozilla/5.0 (compatible)"}

    rows = fetchall(
        "SELECT id, url, company, careers_site FROM jobs "
        "WHERE source = 'jorb' AND url LIKE %s",
        ("%jorb.ai%",),
    )
    if not rows:
        return
    logger.info(f"JORB BACKFILL: fixing {len(rows)} jobs with jorb.ai URLs")
    updated = 0
    for row in rows:
        try:
            time.sleep(0.2)
            req  = urllib.request.Request(row["url"], headers=_HDRS)
            html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="replace")
            m = _RE.search(html)
            if m:
                direct = m.group(1)
                execute(
                    "UPDATE jobs SET url = ? WHERE id = ?",
                    (direct, row["id"]),
                )
                updated += 1
            else:
                execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
                logger.info(f"JORB BACKFILL: deleted {row['company']} (no direct URL, jorb 404)")
        except Exception as e:
            execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            logger.info(f"JORB BACKFILL: deleted {row['company']} (error: {e})")
    logger.info(f"JORB BACKFILL: done — {updated} updated")


def run_daemon():
    init_db()
    try:
        _backfill_jorb_urls_once()
    except Exception as e:
        logger.warning(f"JORB BACKFILL: failed — {e}")
    trackr_slots = ", ".join(f"{h:02d}:{m:02d}" for h, m in TRACKR_SCHEDULE)
    wttj_slots   = ", ".join(f"{h:02d}:{m:02d}" for h, m in WTTJ_SCHEDULE)
    jorb_slots   = ", ".join(f"{h:02d}:{m:02d}" for h, m in JORB_SCHEDULE)
    logger.info(
        f"inroad scheduler started — "
        f"Trackr at {trackr_slots} UTC, "
        f"WTTJ at {wttj_slots} UTC, "
        f"Jorb at {jorb_slots} UTC, "
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
    next_wttj    = _next_wttj_datetime()
    next_jorb    = _next_jorb_datetime()
    logger.info(f"First scrape+leads run scheduled for {next_scrape.strftime('%Y-%m-%d %H:%M')} UTC")
    logger.info(f"First Trackr pipeline run scheduled for {next_trackr.strftime('%Y-%m-%d %H:%M')} UTC")
    logger.info(f"First WTTJ pipeline run scheduled for {next_wttj.strftime('%Y-%m-%d %H:%M')} UTC")
    logger.info(f"First Jorb pipeline run scheduled for {next_jorb.strftime('%Y-%m-%d %H:%M')} UTC")

    while not stop["flag"]:
        wait_scrape   = seconds_until(SCRAPE_HOUR)
        wait_pipeline = seconds_until(PIPELINE_HOUR)
        now           = datetime.utcnow()
        wait_trackr   = max((next_trackr - now).total_seconds(), 0)
        wait_wttj     = max((next_wttj - now).total_seconds(), 0)
        wait_jorb     = max((next_jorb - now).total_seconds(), 0)
        wait = min(wait_scrape, wait_pipeline, wait_trackr, wait_wttj, wait_jorb)
        logger.info(
            f"Next wake in {wait/3600:.1f}h — "
            f"Trackr at {next_trackr.strftime('%H:%M')} UTC, "
            f"WTTJ at {next_wttj.strftime('%H:%M')} UTC, "
            f"Jorb at {next_jorb.strftime('%H:%M')} UTC, "
            f"scrape at {SCRAPE_HOUR:02d}:00 UTC, cards at {PIPELINE_HOUR:02d}:00 UTC"
        )

        elapsed = 0.0
        while elapsed < wait and not stop["flag"]:
            time.sleep(min(30, wait - elapsed))
            elapsed += 30

        if stop["flag"]:
            break

        now = datetime.utcnow()

        # WTTJ pipeline — 4x daily at WTTJ_SCHEDULE slots
        if now >= next_wttj:
            try:
                run_wttj_job()
            except Exception as e:
                logger.error(f"WTTJ job crashed: {e}", exc_info=True)
            try:
                run_wttj_leads_check_job(max_per_run=50)
            except Exception as e:
                logger.error(f"WTTJ leads check crashed: {e}", exc_info=True)
            next_wttj = _next_wttj_datetime()
            logger.info(f"Next WTTJ pipeline scheduled for {next_wttj.strftime('%Y-%m-%d %H:%M')} UTC")

        # Jorb pipeline — 8x daily at JORB_SCHEDULE slots
        if now >= next_jorb:
            try:
                run_jorb_pipeline()
            except Exception as e:
                logger.error(f"Jorb pipeline crashed: {e}", exc_info=True)
            next_jorb = _next_jorb_datetime()
            logger.info(f"Next Jorb pipeline scheduled for {next_jorb.strftime('%Y-%m-%d %H:%M')} UTC")

        # Trackr pipeline — 8x daily at TRACKR_SCHEDULE slots
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
    elif "--wttj" in args:
        run_wttj_pipeline()
    elif "--jorb" in args:
        run_jorb_pipeline()
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
