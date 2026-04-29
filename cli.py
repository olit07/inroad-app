"""
inroad Backend — Admin CLI

Usage:
    python cli.py stats                         # database health summary
    python cli.py scrape                        # run all scrapers now
    python cli.py scrape --source greenhouse    # run one scraper
    python cli.py search "goldman analyst"      # full-text job search
    python cli.py jobs --industry Finance       # list jobs by filter
    python cli.py purge --days 45               # delete inactive jobs older than N days
    python cli.py init                          # initialise database schema
"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from config.settings import DB_PATH
from db.database     import init_db, db_conn, db_stats, get_active_jobs, search_jobs


# ── Colour helpers (no rich dependency) ─────────────────────────────────────
def green(s):  return click.style(str(s), fg="green")
def yellow(s): return click.style(str(s), fg="yellow")
def red(s):    return click.style(str(s), fg="red")
def bold(s):   return click.style(str(s), bold=True)
def dim(s):    return click.style(str(s), dim=True)


@click.group()
def cli():
    """Coffee Chat Connect — backend admin CLI."""
    pass


# ── init ─────────────────────────────────────────────────────────────────────
@cli.command()
def init():
    """Initialise the database schema."""
    init_db(DB_PATH)
    click.echo(green(f"✓ Database initialised at {DB_PATH}"))


# ── stats ─────────────────────────────────────────────────────────────────────
@cli.command()
def stats():
    """Show database health summary."""
    init_db(DB_PATH)
    with db_conn(DB_PATH) as conn:
        s = db_stats(conn)

    click.echo()
    click.echo(bold("  inroad Database Stats"))
    click.echo("  " + "─" * 40)
    click.echo(f"  Total jobs       {bold(s['total_jobs'])}")
    click.echo(f"  Active jobs      {green(s['active_jobs'])}")
    click.echo(f"  Companies        {s['companies']}")
    click.echo(f"  Students         {s['students']}")
    click.echo(f"  Matches today    {s['matches_today']}")
    click.echo(f"  Emails sent (7d) {s['emails_sent_week']}")

    click.echo()
    click.echo(bold("  Jobs by source"))
    click.echo("  " + "─" * 40)
    for source, count in s["by_source"].items():
        bar = "█" * min(int(count / 5), 40)
        click.echo(f"  {source:<30} {green(count):>6}  {dim(bar)}")

    click.echo()
    click.echo(bold("  Jobs by industry (top 10)"))
    click.echo("  " + "─" * 40)
    for industry, count in list(s["by_industry"].items())[:10]:
        click.echo(f"  {industry:<30} {count:>6}")

    click.echo()
    click.echo(bold("  Recent scrape runs"))
    click.echo("  " + "─" * 40)
    for run in s["recent_runs"]:
        status_str = green("✓ ok") if run["status"] == "ok" else \
                     yellow("⚠ empty") if run["status"] == "empty" else \
                     red("✗ error")
        click.echo(
            f"  {run['source_id']:<25}  {status_str}  "
            f"+{run['jobs_new']} new  {dim(run['finished_at'] or '')}"
        )
    click.echo()


# ── scrape ────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--source", default=None, help="Run only this source_id")
def scrape(source):
    """Run scrapers and ingest jobs into the database."""
    from pipeline.ingest import run_all_scrapers, run_single_scraper

    init_db(DB_PATH)

    if source:
        from scrapers import get_scraper_by_id
        try:
            scraper = get_scraper_by_id(source)
        except ValueError as e:
            click.echo(red(f"✗ {e}"))
            sys.exit(1)
        summary = run_single_scraper(scraper, DB_PATH)
        _print_summary([summary])
    else:
        summaries = run_all_scrapers(DB_PATH)
        _print_summary(summaries)


def _print_summary(summaries: list):
    click.echo()
    click.echo(bold("  Scrape results"))
    click.echo("  " + "─" * 60)
    for s in summaries:
        status_str = green("✓") if s["status"] == "ok" else \
                     yellow("⚠") if s["status"] == "empty" else \
                     red("✗")
        click.echo(
            f"  {status_str}  {s['source_name']:<28}"
            f"  found:{s['jobs_found']:>4}"
            f"  new:{s['jobs_new']:>4}"
            f"  {s['duration_secs']:.1f}s"
            + (f"  {red(s['error'][:40])}" if s.get("error") else "")
        )
    click.echo()
    click.echo(
        f"  Total: {green(sum(s['jobs_new'] for s in summaries))} new jobs "
        f"from {len(summaries)} scrapers"
    )
    click.echo()


# ── search ────────────────────────────────────────────────────────────────────
@cli.command()
@click.argument("query")
@click.option("--limit", default=20, help="Max results")
def search(query, limit):
    """Full-text search jobs by title or company."""
    init_db(DB_PATH)
    with db_conn(DB_PATH) as conn:
        results = search_jobs(conn, query, limit=limit)

    if not results:
        click.echo(yellow(f"No results for '{query}'"))
        return

    click.echo()
    click.echo(bold(f"  {len(results)} results for '{query}'"))
    click.echo("  " + "─" * 70)
    for j in results:
        inds = ", ".join(j["industries"][:2])
        click.echo(
            f"  {j['company_name']:<25}  {j['title'][:35]:<35}"
            f"  {dim(j['region']):<5}  {dim(j['seniority']):<10}  {dim(inds)}"
        )
    click.echo()


# ── jobs ──────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--industry", default=None, help="Filter by industry name")
@click.option("--region",   default=None, help="UK / US / Global")
@click.option("--seniority",default=None, help="intern / junior / mid / senior / leadership")
@click.option("--days",     default=30,   help="Only jobs posted within N days")
@click.option("--limit",    default=30,   help="Max results")
def jobs(industry, region, seniority, days, limit):
    """List active jobs with optional filters."""
    init_db(DB_PATH)
    with db_conn(DB_PATH) as conn:
        industries_filter = [industry] if industry else None
        results = get_active_jobs(
            conn,
            industries  = industries_filter,
            region      = region,
            seniority   = seniority,
            days_fresh  = days,
            limit       = limit,
        )

    if not results:
        click.echo(yellow("No jobs found matching those filters"))
        return

    click.echo()
    click.echo(bold(f"  {len(results)} jobs"))
    click.echo("  " + "─" * 80)
    for j in results:
        inds  = ", ".join(j["industries"][:2])
        close = f" → {j['closing_date']}" if j.get("closing_date") else ""
        click.echo(
            f"  {j['posted_date']:<12}"
            f"  {j['company_name']:<22}"
            f"  {j['title'][:38]:<38}"
            f"  {dim(j['region']):<5}"
            f"  {dim(j['seniority']):<10}"
            f"  {dim(inds)}"
            f"  {dim(close)}"
        )
    click.echo()


# ── purge ─────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--days", default=45, help="Delete inactive jobs older than N days")
@click.confirmation_option(prompt="This permanently deletes jobs from the DB. Proceed?")
def purge(days):
    """Delete old inactive jobs from the database."""
    init_db(DB_PATH)
    cutoff = datetime.utcnow().strftime("%Y-%m-%d")
    with db_conn(DB_PATH) as conn:
        cur = conn.execute(
            """DELETE FROM jobs
               WHERE is_active=0
               AND last_seen_at < date('now', ?)""",
            (f"-{days} days",),
        )
        deleted = cur.rowcount
    click.echo(green(f"✓ Deleted {deleted} inactive jobs older than {days} days"))


# ── students ──────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--limit", default=20)
def students(limit):
    """List all registered students with match stats."""
    init_db(DB_PATH)
    with db_conn(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT s.id, s.email, s.first_name, s.university, s.created_at,
               (SELECT COUNT(*) FROM matches WHERE student_id=s.id AND email_sent_at IS NOT NULL) as sent,
               (SELECT COUNT(*) FROM matches WHERE student_id=s.id AND reply_received=1) as replies
               FROM students s ORDER BY s.created_at DESC LIMIT ?""", (limit,)
        ).fetchall()
    if not rows:
        click.echo(yellow("No students registered yet"))
        return
    click.echo()
    click.echo(bold(f"  {len(rows)} students"))
    click.echo("  " + "─" * 80)
    for r in rows:
        rate = f"{round(r['replies']/r['sent']*100)}%" if r['sent'] else "—"
        click.echo(
            f"  [{r['id']:>3}] {r['email']:<35} {(r['first_name'] or '?'):<12}"
            f" sent:{r['sent']:>3} replies:{r['replies']:>2} rate:{rate:>5}"
            f"  {dim(r['created_at'][:10] if r['created_at'] else '')}"
        )
    click.echo()


# ── cards ─────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--student-id", type=int, required=True, help="Generate cards for this student ID")
@click.option("--all", "all_students", is_flag=True, help="Generate for all students")
def cards(student_id, all_students):
    """Generate daily match cards for a student (or all students)."""
    init_db(DB_PATH)
    from pipeline.daily_cards import generate_daily_cards, generate_all_students_cards
    from pipeline.profile_cache import init_cache
    init_cache(DB_PATH)
    if all_students:
        click.echo("Generating cards for all students...")
        generate_all_students_cards(DB_PATH)
        click.echo(green("✓ Done"))
    else:
        click.echo(f"Generating cards for student {student_id}...")
        result = generate_daily_cards(student_id, DB_PATH)
        if result:
            click.echo(green(f"✓ {len(result)} cards generated"))
            for c in result:
                click.echo(f"  → {c.get('person_name','—')} @ {c.get('person_company','—')} (score {c.get('relevance_score','—')})")
        else:
            click.echo(yellow("No cards generated (no search API key, or already generated today)"))


# ── enrich ────────────────────────────────────────────────────────────────────
@cli.command()
def enrich():
    """Enrich companies table with domain, size, sector data."""
    init_db(DB_PATH)
    from utils.company_enrichment import enrich_companies
    result = enrich_companies(DB_PATH)
    click.echo(green(f"✓ Enriched: {result['enriched']} new, {result['updated']} updated, {result['total']} total"))


# ── notify ────────────────────────────────────────────────────────────────────
@cli.command()
@click.argument("student_id", type=int)
@click.option("--type", "notif_type", default="matches", help="matches | digest | magic")
def notify(student_id, notif_type):
    """Send a notification email to a student."""
    init_db(DB_PATH)
    with db_conn(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not row:
        click.echo(red(f"Student {student_id} not found"))
        return
    student = dict(row)
    from utils.notifications import send_daily_matches_ready, send_weekly_digest_email
    from pipeline.reply_tracker import weekly_digest
    if notif_type == "matches":
        sent = send_daily_matches_ready(student, 3, DB_PATH)
    elif notif_type == "digest":
        d = weekly_digest(student_id, DB_PATH)
        sent = send_weekly_digest_email(student, d)
    else:
        click.echo(red("Unknown notification type"))
        return
    if sent:
        click.echo(green(f"✓ Email sent to {student['email']}"))
    else:
        click.echo(yellow(f"○ SMTP not configured (would send to {student['email']})"))

if __name__ == '__main__':
    cli()
