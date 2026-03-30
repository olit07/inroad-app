"""
CCC Backend — Daily 3-card algorithm (Phase 5)

For each student, every morning at 07:00:
1. Query active jobs matching student's preferences
2. Run LinkedIn matching for top candidates
3. Score and deduplicate against history
4. Apply diversity rules (max 1/company, max 2/industry per day)
5. Generate AI email draft via Claude API for final 3 cards
6. Write to daily_matches table

Usage:
    python pipeline/daily_cards.py --student-id 1
    python pipeline/daily_cards.py --all           # run for all students
"""
import os
import re
import json
import logging
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings    import DAILY_MATCH_QUOTA, CLOSING_SOON_DAYS, FRESHNESS_DECAY_DAYS, DB_PATH
from db.database        import db_conn, get_active_jobs
from pipeline.matcher   import LinkedInMatcher, score_lead
from pipeline.email_infer import EmailInferrer

logger = logging.getLogger(__name__)


# ── Score a job for a student ─────────────────────────────────────────────────

def score_job(job: dict, student: dict) -> float:
    """
    Score a job 0–100 for a given student.
    Recency bonus / decay baked in here.
    """
    score = 50.0  # base

    # Freshness decay
    posted = job.get("posted_date", "")
    if posted:
        try:
            days_old = (date.today() - date.fromisoformat(posted)).days
            if days_old > FRESHNESS_DECAY_DAYS:
                score *= 0.6
        except Exception:
            pass

    # Closing soon boost
    closing = job.get("closing_date", "")
    if closing:
        try:
            days_left = (date.fromisoformat(closing) - date.today()).days
            if 0 < days_left <= CLOSING_SOON_DAYS:
                score *= 1.4
        except Exception:
            pass

    # Company size preference match
    student_size = student.get("company_size", "")
    # (Would use company size from companies table — skipping enrichment for now)

    # Industry overlap
    job_industries     = set(job.get("industries", []))
    student_industries = set(json.loads(student.get("industries") or "[]"))
    overlap = len(job_industries & student_industries)
    score += overlap * 10

    # Seniority fit — students want intern/junior/mid roles
    seniority = job.get("seniority", "")
    if seniority in ("intern", "junior"):
        score += 15
    elif seniority == "mid":
        score += 8

    return round(min(score, 100), 1)


def get_seen_history(conn, student_id: int) -> set:
    """Return set of job_ids already shown to this student."""
    rows = conn.execute(
        "SELECT job_id FROM matches WHERE student_id=?", (student_id,)
    ).fetchall()
    return {r[0] for r in rows}


def get_suppressed_linkedin_urls(conn) -> set:
    """Return suppressed LinkedIn URLs from suppression_list."""
    rows = conn.execute(
        "SELECT identifier FROM suppression_list WHERE identifier_type='linkedin'"
    ).fetchall()
    return {r[0] for r in rows}


# ── Email draft generation ────────────────────────────────────────────────────

INDUSTRY_TONE_HINTS: dict[str, str] = {
    "Investment Banking": "Be direct and numbers-aware. Finance professionals respect brevity. Don't be vague.",
    "Finance":            "Be concise. Reference markets or quant angle if relevant to the role.",
    "Technology":         "Be curious and product-minded. You can reference the company's product briefly.",
    "Software Engineering": "Be technical but human. Skip buzzwords. Show you understand the craft.",
    "Product Management": "Show you think in user problems and outcomes. Reference the company's product.",
    "Consulting":         "Be structured. Show you understand the problem-solving culture.",
    "Strategy":           "Be sharp and commercial. Reference market positioning if you can.",
    "Law":                "Be formal but warm. You can mention the firm's practice area.",
    "Venture Capital":    "Show intellectual curiosity about their portfolio. Be specific not generic.",
    "Data & Analytics":   "Be analytical. Reference the role's data domain specifically.",
    "Design & UX":        "Show taste. Mention you've used their product or a specific design decision.",
    "Marketing":          "Be creative but focused. Reference a campaign or channel you respect.",
    "Healthcare":         "Be earnest. Show genuine interest in the impact, not just the career.",
    "Non-profit & Policy":"Be values-led. Show you understand the mission.",
    "Media & Journalism": "Be direct and opinionated. Journalists respond to people with a point of view.",
}


def _get_industry_tone(job: dict) -> str:
    industries = job.get("industries", [])
    if isinstance(industries, str):
        import json as _j
        try:
            industries = _j.loads(industries)
        except Exception:
            industries = []
    for ind in industries:
        if ind in INDUSTRY_TONE_HINTS:
            return INDUSTRY_TONE_HINTS[ind]
    return "Be genuine and concise."


def generate_email_draft(
    student: dict,
    lead:    dict,
    job:     dict,
) -> tuple[str, str]:
    """
    Generate subject + body using Claude API (claude-sonnet-4-6).
    Falls back to email_templates.py if API key not set or quality check fails.
    Returns (subject, body).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    context = {
        "student_name":       student.get("first_name", "there"),
        "student_university": student.get("university", "my university"),
        "student_bio":        student.get("bio", ""),
        "recipient_name":     lead.get("name", "").split()[0] if lead.get("name") else "there",
        "recipient_title":    lead.get("title", ""),
        "recipient_company":  lead.get("company", job.get("company_name", "")),
        "is_alumni":          lead.get("is_alumni", False),
        "job_title":          job.get("title", ""),
        "job_url":            job.get("url", ""),
        "cal_link":           student.get("cal_link", "cal.com/me/coffee-chat"),
        "industry_tone":      _get_industry_tone(job),
    }

    if api_key:
        subject, body = _claude_draft(context, api_key)
        # Quality check — retry once if poor
        from pipeline.email_templates import check_quality
        q = check_quality(subject, body, {
            "recipient_first_name": context["recipient_name"],
            "student_cal_link": context["cal_link"],
            "job_title": context["job_title"],
        })
        if q["score"] < 6:
            logger.info(f"Email quality {q['score']}/10 — retrying with tighten prompt")
            subject, body = _claude_draft(context, api_key, tighten=True)
            q2 = check_quality(subject, body, {
                "recipient_first_name": context["recipient_name"],
                "student_cal_link": context["cal_link"],
                "job_title": context["job_title"],
            })
            if q2["score"] < 6:
                logger.info("Second attempt still low quality — falling back to template")
                return _template_draft(context)
        return subject, body
    else:
        logger.warning("ANTHROPIC_API_KEY not set — using template email")
        return _template_draft(context)


def _claude_draft(ctx: dict, api_key: str, tighten: bool = False) -> tuple[str, str]:
    """Call Claude claude-sonnet-4-6 to generate the email."""
    import urllib.request
    import json

    alumni_note = (
        f"They are an alumni of {ctx['student_university']} — mention this connection naturally in one sentence."
        if ctx["is_alumni"]
        else "They are NOT an alumni — do NOT mention any shared university connection."
    )

    tighten_note = "\n\nIMPORTANT: Previous draft was too long or used filler phrases. Make this version sharper, under 80 words, more direct." if tighten else ""

    prompt = f"""You are a career coach writing a cold outreach email for a university student.

STUDENT:
- Name: {ctx['student_name']}
- University: {ctx['student_university']}
- Bio: {ctx['student_bio']}
- Cal.com booking link: {ctx['cal_link']}

RECIPIENT:
- Name: {ctx['recipient_name']}
- Title: {ctx['recipient_title']} at {ctx['recipient_company']}
- {alumni_note}

ROLE: {ctx['job_title']} at {ctx['recipient_company']}

TONE: {ctx.get('industry_tone', 'Be genuine and concise.')}{tighten_note}

STRICT RULES:
- Under 100 words total in body
- Plain text only — no markdown, bullets, or HTML
- Mention the specific role by name
- Ask for exactly 20 minutes
- End with the cal.com link on its own line
- NEVER use: "I hope this finds you well", "reach out", "touch base", "passionate about", "leverage", "I am writing to"

OUTPUT FORMAT:
SUBJECT: <subject line>
BODY:
<email body>"""

    payload = json.dumps({
        "model":      "claude-sonnet-4-6",
        "max_tokens": 400,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data  = json.loads(resp.read().decode())
            text  = data["content"][0]["text"].strip()
            subject, body = "", text

            lines = text.split("\n")
            for i, line in enumerate(lines):
                if line.upper().startswith("SUBJECT:"):
                    subject = line.split(":", 1)[1].strip()
                elif line.strip().upper() == "BODY:":
                    body = "\n".join(lines[i+1:]).strip()
                    break

            return subject, body

    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return _template_draft(ctx)


def _template_draft(ctx: dict) -> tuple[str, str]:
    """Fallback template email when Claude API is unavailable."""
    uni = ctx["student_university"]
    alumni_line = (
        f"I noticed you also studied at {uni} before joining {ctx['recipient_company']}. "
        if ctx["is_alumni"]
        else ""
    )

    subject = (
        f"{uni} student reaching out about the "
        f"{ctx['job_title']} role at {ctx['recipient_company']}"
    )

    body = f"""Hi {ctx['recipient_name']},

{alumni_line}I'm a student at {uni} and came across the {ctx['job_title']} opening at {ctx['recipient_company']}.

I'd love to hear how you got into your current role and what the work actually looks like day to day. Would you be open to 20 minutes?

{ctx['cal_link']}

Thanks,
{ctx['student_name']}"""

    return subject, body.strip()


# ── Main daily card generation ────────────────────────────────────────────────

def generate_daily_cards(student_id: int, db_path=DB_PATH) -> list[dict]:
    """
    Generate the 3 daily cards for a student and write to DB.
    Returns list of match dicts written.
    """
    today_str = date.today().isoformat()

    with db_conn(db_path) as conn:
        # Load student
        student = conn.execute(
            "SELECT * FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if not student:
            logger.warning(f"Student {student_id} not found")
            return []
        student = dict(student)

        # Check if today's cards already generated
        existing = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND match_date=?",
            (student_id, today_str),
        ).fetchone()[0]
        if existing >= DAILY_MATCH_QUOTA:
            logger.info(f"Student {student_id} already has {existing} cards for {today_str}")
            return []

        seen_job_ids  = get_seen_history(conn, student_id)
        suppressed    = get_suppressed_linkedin_urls(conn)

        # Get matching jobs
        student_industries = json.loads(student.get("industries") or "[]")
        jobs = get_active_jobs(
            conn,
            industries  = student_industries,
            region      = student.get("region", "UK"),
            days_fresh  = 21,
            limit       = 100,
        )

    # Filter already seen
    jobs = [j for j in jobs if j["id"] not in seen_job_ids]

    # Score jobs
    scored_jobs = [(score_job(j, student), j) for j in jobs]
    scored_jobs.sort(key=lambda x: -x[0])

    matcher  = LinkedInMatcher()
    inferrer = EmailInferrer()

    cards_written: list[dict] = []
    companies_used: set  = set()
    industries_used: dict = {}

    for _, job in scored_jobs:
        if len(cards_written) >= DAILY_MATCH_QUOTA:
            break

        company   = job["company_name"]
        job_inds  = job.get("industries", [])

        # Diversity rules
        if company in companies_used:
            continue
        if any(industries_used.get(ind, 0) >= 2 for ind in job_inds):
            continue

        # Find leads for this job
        leads = matcher.find_leads(
            company            = company,
            job_title          = job["title"],
            student_university = student.get("university", ""),
            n                  = 6,
        )

        # Filter suppressed
        leads = [l for l in leads if l.get("linkedin_url", "") not in suppressed]

        # Filter already-sent people (by linkedin_url in match history)
        with db_conn(db_path) as conn:
            sent_urls = {
                r[0] for r in conn.execute(
                    "SELECT person_linkedin_url FROM matches WHERE student_id=?",
                    (student_id,)
                ).fetchall()
            }
        leads = [l for l in leads if l.get("linkedin_url", "") not in sent_urls]

        if not leads:
            continue

        # Score leads
        scored_leads = [
            (score_lead(lead, job, student), lead) for lead in leads
        ]
        scored_leads.sort(key=lambda x: -x[0])
        best_score, best_lead = scored_leads[0]

        # Infer email
        name_parts = best_lead.get("name", "").split()
        first = name_parts[0] if name_parts else ""
        last  = name_parts[-1] if len(name_parts) > 1 else ""
        email_result = inferrer.infer(
            first_name     = first,
            last_name      = last,
            company_name   = company,
            job_url        = job.get("url", ""),
        )

        # Generate email draft
        subject, body = generate_email_draft(student, best_lead, job)

        card = {
            "student_id":          student_id,
            "job_id":              job["id"],
            "match_date":          today_str,
            "person_name":         best_lead.get("name", ""),
            "person_title":        best_lead.get("title", ""),
            "person_company":      best_lead.get("company", company),
            "person_linkedin_url": best_lead.get("linkedin_url", ""),
            "person_university":   best_lead.get("university", ""),
            "person_tenure_months": best_lead.get("tenure_months", 0),
            "is_alumni":           int(best_lead.get("is_alumni", False)),
            "relevance_score":     round(best_score, 1),
            "expected_email":      email_result["email"],
            "email_confidence":    email_result["confidence"],
            "email_subject":       subject,
            "email_body":          body,
        }

        with db_conn(db_path) as conn:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO matches
                       (student_id, job_id, match_date, person_name, person_title,
                        person_company, person_linkedin_url, person_university,
                        person_tenure_months, is_alumni, relevance_score,
                        expected_email, email_confidence, email_subject, email_body)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        card["student_id"], card["job_id"], card["match_date"],
                        card["person_name"], card["person_title"], card["person_company"],
                        card["person_linkedin_url"], card["person_university"],
                        card["person_tenure_months"], card["is_alumni"],
                        card["relevance_score"], card["expected_email"],
                        card["email_confidence"], card["email_subject"], card["email_body"],
                    )
                )
            except Exception as e:
                logger.error(f"Failed to insert card: {e}")
                continue

        cards_written.append(card)
        companies_used.add(company)
        for ind in job_inds:
            industries_used[ind] = industries_used.get(ind, 0) + 1

        logger.info(
            f"Card {len(cards_written)}: {best_lead.get('name')} @ {company} "
            f"({job['title']}) — score {best_score}"
        )

    # Anonymise matches older than 90 days
    _anonymise_old_matches(student_id, db_path)

    logger.info(f"Student {student_id}: {len(cards_written)} cards written for {today_str}")
    return cards_written


def generate_all_students_cards(db_path=DB_PATH):
    """Run daily card generation for every student."""
    with db_conn(db_path) as conn:
        students = conn.execute("SELECT id, first_name FROM students").fetchall()

    logger.info(f"Generating daily cards for {len(students)} students")
    for s in students:
        try:
            generate_daily_cards(s["id"], db_path)
        except Exception as e:
            logger.error(f"Card gen failed for student {s['id']}: {e}", exc_info=True)


def _anonymise_old_matches(student_id: int, db_path=DB_PATH):
    """Anonymise person data for matches older than 90 days."""
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    with db_conn(db_path) as conn:
        conn.execute(
            """UPDATE matches SET
               person_name=NULL, person_linkedin_url=NULL, expected_email=NULL
               WHERE student_id=? AND match_date < ? AND person_name IS NOT NULL""",
            (student_id, cutoff),
        )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if "--all" in sys.argv:
        generate_all_students_cards()
    elif "--student-id" in sys.argv:
        idx = sys.argv.index("--student-id")
        sid = int(sys.argv[idx + 1])
        cards = generate_daily_cards(sid)
        print(f"\nGenerated {len(cards)} cards for student {sid}")
        for c in cards:
            print(f"  → {c['person_name']} | {c['person_company']} | score {c['relevance_score']}")
    else:
        print("Usage: python daily_cards.py --student-id N | --all")
