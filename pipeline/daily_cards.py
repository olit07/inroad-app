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
from config.settings    import DAILY_MATCH_QUOTA, DB_PATH
from db.database        import db_conn, get_active_jobs, get_card_count_today, USE_POSTGRES, \
                               fetchone as db_fetchone, fetchall as db_fetchall, \
                               execute as db_execute, get_leads_for_company, get_seen_linkedin_urls
from pipeline.matcher   import LinkedInMatcher, score_lead, score_lead_v2
from pipeline.email_infer import EmailInferrer

logger = logging.getLogger(__name__)


# ── Score a job for a student ─────────────────────────────────────────────────

def _preference_score(job: dict, student: dict) -> float:
    """
    Preference Match component (0–100).
    Measures how well the job matches the student's stated preferences.
    """
    score = 0.0

    # Industry match: student.industries is a JSON list of strings;
    # job.industry is a single string
    student_industries = json.loads(student.get("industries") or "[]")
    if job.get("industry") in student_industries:
        score += 50.0
    elif any(ind.lower() in (job.get("industry") or "").lower() for ind in student_industries):
        score += 25.0

    # Company size match
    if student.get("company_size") and job.get("company_size"):
        if student["company_size"] == job["company_size"]:
            score += 30.0

    # Seniority fit (use job title keywords)
    student_status = student.get("status", "")  # "intern", "junior", "mid", "senior"
    job_title = (job.get("title") or "").lower()
    if student_status in ("intern", "junior"):
        if any(kw in job_title for kw in ("junior", "graduate", "analyst", "associate", "entry")):
            score += 20.0
    elif student_status == "mid":
        if any(kw in job_title for kw in ("senior", "lead", "manager", "principal")):
            score += 20.0

    return min(score, 100.0)


def _recency_score(job: dict) -> float:
    """
    Recency component (0–100).
    Newer postings score higher.
      0–3 days:   100
      4–7 days:    80
      8–14 days:   60
      15–21 days:  40
      >21 days:    20
    """
    posted_at = job.get("posted_at")
    if not posted_at:
        return 40.0  # neutral if unknown
    try:
        posted_date = date.fromisoformat(str(posted_at)[:10])
        days_old = (date.today() - posted_date).days
    except Exception:
        return 40.0

    if days_old <= 3:
        return 100.0
    elif days_old <= 7:
        return 80.0
    elif days_old <= 14:
        return 60.0
    elif days_old <= 21:
        return 40.0
    else:
        return 20.0


def _diversity_score(job: dict, already_selected: list) -> float:
    """
    Diversity component (0–100).
    Rewards jobs that differ from those already selected today.
      100 if no jobs selected yet.
      -30 per already-selected job from the same company.
      -20 per already-selected job from the same industry.
      Minimum 0.
    """
    if not already_selected:
        return 100.0

    score = 100.0
    job_company  = job.get("company_name", "")
    job_industry = job.get("industry", "")

    for selected in already_selected:
        if job_company and selected.get("company_name", "") == job_company:
            score -= 30.0
        if job_industry and selected.get("industry", "") == job_industry:
            score -= 20.0

    return max(score, 0.0)


def score_job(job: dict, student: dict, already_selected: list | None = None) -> float:
    """
    Score a job for a student using the roadmap formula:
      Score = (Preference Match × 0.40) + (Recency × 0.30) + (Diversity × 0.30)

    Returns a float 0–100.
    """
    already_selected = already_selected or []
    pref = _preference_score(job, student)
    rec  = _recency_score(job)
    div  = _diversity_score(job, already_selected)
    return round(pref * 0.40 + rec * 0.30 + div * 0.30, 1)


def _lead_company_matches(lead_company: str, job_company: str) -> bool:
    """
    Return True if the lead's company matches the job company.
    Allows empty lead_company through (couldn't be parsed from snippet).
    """
    if not lead_company:
        return True  # can't verify — let through; company is set from job at write time
    lc = lead_company.lower().strip()
    jc = job_company.lower().strip()
    if jc in lc or lc in jc:
        return True
    # Word-level overlap: any significant word from job company found in lead company
    for word in jc.split():
        if len(word) > 3 and word in lc:
            return True
    return False


def get_seen_history(student_id: int) -> set:
    """Return set of job_ids already shown to this student."""
    rows = db_fetchall(
        "SELECT job_id FROM matches WHERE student_id = %s", (student_id,)
    )
    return {r["job_id"] for r in rows}


def get_suppressed_linkedin_urls() -> set:
    """Return suppressed LinkedIn URLs from suppression_list."""
    rows = db_fetchall(
        "SELECT identifier FROM suppression_list WHERE identifier_type='linkedin'"
    )
    return {r["identifier"] for r in rows}


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


_SENIORITY_WORDS = {
    "summer", "intern", "internship", "associate", "analyst", "graduate",
    "junior", "senior", "lead", "head", "director", "manager", "officer",
    "specialist", "coordinator", "advisor", "consultant", "programme",
    "program", "placement", "spring", "winter", "year", "graduate",
}

def _job_department(job_title: str) -> str:
    """
    Extract a clean department label from a job title.
    e.g. '2026 Risk Management (Quant) Summer Associate' → 'Risk Management'
         'Software Engineering Intern' → 'Software Engineering'
    """
    import re
    title = job_title or ""
    # Strip leading year (e.g. "2026 ...")
    title = re.sub(r'^\d{4}\s+', '', title)
    # Strip content in parentheses
    title = re.sub(r'\(.*?\)', '', title)
    # Remove seniority/programme words from the end
    words = title.split()
    cleaned = [w for w in words if w.lower() not in _SENIORITY_WORDS]
    result = " ".join(cleaned).strip(" –-,")
    return result or title.strip()


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

    tenure_months = lead.get("tenure_months") or 0
    tenure_hint = ""
    if tenure_months > 0:
        tenure_years = round(tenure_months / 12, 1)
        tenure_hint = f"They have been at {lead.get('company', job.get('company_name', ''))} for approximately {tenure_years} year(s)."

    context = {
        "student_name":       student.get("name") or student.get("first_name", "there"),
        "student_university": student.get("university", "my university"),
        "student_bio":        student.get("bio", ""),
        "recipient_name":     lead.get("name", "").split()[0] if lead.get("name") else "there",
        "recipient_title":    lead.get("title", ""),
        "recipient_company":  lead.get("company", job.get("company_name", "")),
        "is_alumni":          lead.get("is_alumni", False),
        "job_title":          job.get("title", ""),
        "job_department":     _job_department(job.get("title", "")),
        "job_url":            job.get("url", ""),
        "industry_tone":      _get_industry_tone(job),
        "tenure_hint":        tenure_hint,
    }

    if api_key:
        subject, body = _claude_draft(context, api_key)
        # Quality check — retry once if poor
        from pipeline.email_templates import check_quality
        q = check_quality(subject, body, {
            "recipient_first_name": context["recipient_name"],
            "job_title": context["job_title"],
        })
        if q["score"] < 6:
            logger.info(f"Email quality {q['score']}/10 — retrying with tighten prompt")
            subject, body = _claude_draft(context, api_key, tighten=True)
            q2 = check_quality(subject, body, {
                "recipient_first_name": context["recipient_name"],
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
    tenure_note = f"\n- {ctx['tenure_hint']}" if ctx.get("tenure_hint") else ""

    tighten_note = "\n\nIMPORTANT: The bio paragraph must be used EXACTLY as provided — do not paraphrase or shorten it." if tighten else ""

    bio = (ctx.get("student_bio") or "").strip() or f"I'm a student at {ctx['student_university']} interested in {ctx.get('industry_tone', 'this field')}."

    prompt = f"""You are writing a cold outreach email on behalf of a university student. Follow the structure EXACTLY — do not deviate.{tighten_note}

STUDENT:
- Name: {ctx['student_name']}
- University: {ctx['student_university']}
- Bio (use verbatim as the intro paragraph): {bio}

RECIPIENT:
- First name: {ctx['recipient_first_name']}
- Title: {ctx['recipient_title']} at {ctx['recipient_company']}{tenure_note}

REQUIRED STRUCTURE (copy this exactly, filling in the placeholders):

Hi [recipient first name],
I know you are incredibly busy and get a lot of emails, so this will only take 30 seconds to read.

[Student bio — paste it verbatim, no changes]

What do you think are the most critical skills or qualities that an aspiring analyst on your team should possess?

I totally understand if you are too busy to reply.
Even a 1 or 2 line response will completely make my day.

All the best,
[Student name]

[Student university]

RULES:
- Plain text only — no markdown, bullets, asterisks, or HTML
- Do NOT change or paraphrase the bio — use it word for word
- Do NOT add any extra sentences, questions, or paragraphs
- Do NOT mention the specific job title or company name in the body
- Do NOT ask for a meeting, coffee chat, or call
- Do NOT include any URLs or links

OUTPUT FORMAT:
SUBJECT: Student with a query on {ctx['job_department']}
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
    from pipeline.email_templates import template_standard

    name_parts = (ctx.get("recipient_name") or "").split()
    template_ctx = {
        "student_name":          ctx["student_name"],
        "student_university":    ctx["student_university"],
        "student_bio":           ctx.get("student_bio", ""),
        "recipient_first_name":  name_parts[0] if name_parts else "there",
        "recipient_company":     ctx["recipient_company"] or "your company",
        "job_department":        ctx.get("job_department", ""),
        "industry_hint":         "this field",
    }
    return template_standard(template_ctx)


# ── Main daily card generation ────────────────────────────────────────────────

def generate_daily_cards(student_id: int, db_path=DB_PATH) -> list[dict]:
    """
    Generate the 3 daily cards for a student and write to DB.
    Returns list of match dicts written.
    """
    today_str = date.today().isoformat()

    # Load student
    student = db_fetchone("SELECT * FROM students WHERE id = %s", (student_id,))
    if not student:
        logger.warning(f"Student {student_id} not found")
        return []
    student = dict(student)

    # Per-student quota (referral bonus overrides global default)
    quota = int(student.get("daily_cards_override") or DAILY_MATCH_QUOTA)

    # Check if today's cards already generated
    if get_card_count_today(student_id) >= quota:
        logger.info(f"Student {student_id} already has {quota} cards for {today_str}")
        return []

    seen_job_ids  = get_seen_history(student_id)
    suppressed    = get_suppressed_linkedin_urls()

    # Get matching jobs
    student_industries = json.loads(student.get("industries") or "[]")
    with db_conn(db_path) as conn:
        jobs = get_active_jobs(
            conn,
            industries  = student_industries,
            region      = student.get("region", "UK"),
            days_fresh  = 30,
            limit       = 100,
        )

    # Filter already seen
    jobs = [j for j in jobs if j["id"] not in seen_job_ids]

    # Filter by company_size preference if the student has one set
    if student.get("company_size"):
        student_size = student["company_size"].lower().strip()
        filtered = []
        for job in jobs:
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
        jobs = filtered

    matcher  = LinkedInMatcher()
    inferrer = EmailInferrer()

    # All-time seen people — never show the same person twice to the same student
    seen_people = get_seen_linkedin_urls(student_id)

    cards_written: list[dict] = []
    industries_used: dict = {}

    # Re-score and re-sort after each selection so the diversity component
    # reflects the jobs already chosen this round.
    remaining_jobs = list(jobs)

    while len(cards_written) < quota and remaining_jobs:
        scored_jobs = [
            (score_job(j, student, already_selected=cards_written), j)
            for j in remaining_jobs
        ]
        scored_jobs.sort(key=lambda x: -x[0])

        # Walk the sorted list to find the first job that passes diversity rules
        picked = False
        for _, job in scored_jobs:
            company   = job["company_name"]
            job_inds  = job.get("industries", [])

            # Remove from remaining regardless so we never revisit
            remaining_jobs = [j for j in remaining_jobs if j["id"] != job["id"]]

            # Diversity: max 2 jobs per industry per day (company duplicates OK)
            if any(industries_used.get(ind, 0) >= 2 for ind in job_inds):
                continue

            picked = True
            break

        if not picked:
            break  # no more eligible jobs

        # Use pre-fetched leads pool only (live Serper search disabled)
        leads = get_leads_for_company(company)
        leads = [l for l in leads if l.get("linkedin_url", "") not in seen_people]
        leads = [l for l in leads if l.get("linkedin_url", "") not in suppressed]

        if not leads:
            continue

        # Score leads
        scored_leads = [
            (score_lead_v2(lead, job, student), lead) for lead in leads
        ]
        scored_leads.sort(key=lambda x: -x[0][0])
        (best_score, best_breakdown), best_lead = scored_leads[0]

        # Skip card if best lead score is too low
        MIN_LEAD_SCORE = 50
        if best_score < MIN_LEAD_SCORE:
            logger.info(
                f"Skipping {company} ({job['title']}) — best lead score {best_score} < {MIN_LEAD_SCORE}"
            )
            continue

        # Infer email — use Apollo-provided email if available, otherwise fall back
        apollo_email = best_lead.get("email", "")
        if apollo_email:
            email_result = {
                "email":          apollo_email,
                "confidence":     "HIGH",
                "all_candidates": [apollo_email],
                "domain":         "",
            }
        else:
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
            "is_alumni":           bool(best_lead.get("is_alumni", False)),
            "relevance_score":     round(best_score, 1),
            "score_breakdown":     json.dumps(best_breakdown),
            "expected_email":      email_result["email"],
            "email_confidence":    {"HIGH": 0.9, "MEDIUM": 0.6, "LOW": 0.3}.get(
                                       str(email_result.get("confidence", "")).upper(), 0.5),
            "email_subject":       subject,
            "email_body":          body,
        }

        try:
            insert_sql = """INSERT INTO matches
                           (student_id, job_id, match_date, person_name, person_title,
                            person_company, person_linkedin_url, person_university,
                            person_tenure_months, is_alumni, relevance_score,
                            score_breakdown, expected_email, email_confidence,
                            email_subject, email_body)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (student_id, job_id) DO NOTHING"""
            db_execute(insert_sql, (
                card["student_id"], card["job_id"], card["match_date"],
                card["person_name"], card["person_title"], card["person_company"],
                card["person_linkedin_url"], card["person_university"],
                card["person_tenure_months"], card["is_alumni"],
                card["relevance_score"], card["score_breakdown"],
                card["expected_email"], card["email_confidence"],
                card["email_subject"], card["email_body"],
            ))
        except Exception as e:
            logger.error(f"Failed to insert card: {e}")
            continue

        cards_written.append(card)
        # Track person so they aren't picked again in this same run
        if best_lead.get("linkedin_url"):
            seen_people.add(best_lead["linkedin_url"])
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
    students = db_fetchall("SELECT id, first_name FROM students")

    logger.info(f"Generating daily cards for {len(students)} students")
    for s in students:
        try:
            generate_daily_cards(s["id"], db_path)
        except Exception as e:
            logger.error(f"Card gen failed for student {s['id']}: {e}", exc_info=True)


def _anonymise_old_matches(student_id: int, db_path=DB_PATH):
    """Anonymise person data for matches older than 90 days."""
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    db_execute(
        """UPDATE matches SET
           person_name=NULL, person_linkedin_url=NULL, expected_email=NULL
           WHERE student_id = %s AND match_date < %s AND person_name IS NOT NULL""",
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
