"""
inroad Backend — Daily 3-card algorithm (Phase 5)

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
                               execute as db_execute, get_leads_for_company, get_seen_linkedin_urls, \
                               get_recently_matched_linkedin_urls, update_student_fields
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
    elif any(ind and ind.lower() in (job.get("industry") or "").lower() for ind in student_industries):
        score += 25.0

    # Company size match
    if student.get("company_size") and job.get("company_size"):
        if student["company_size"] == job["company_size"]:
            score += 30.0

    # Role type alignment: +20 bonus when job type matches student's stated preference.
    # No penalty here — mismatched jobs are already separated into a secondary pool
    # in generate_daily_cards() and only used as a fallback when primary is exhausted.
    student_status = student.get("status", "")
    job_role_type  = job.get("role_type") or ""
    if student_status == "grad-program" and job_role_type == "internship_grad":
        score += 20.0   # strong match: grad/intern student → grad/intern role
    elif student_status == "full-time" and job_role_type == "entry_level":
        score += 20.0   # strong match: graduated student → permanent junior role

    return min(max(score, 0.0), 100.0)


def _recency_score(job: dict) -> float:
    """
    Recency component (0–100).
    Jobs posted in the current calendar month are always prioritised (100).
    Older postings decay by age.
      current month:  100
      1 month old:     70
      2 months old:    50
      3 months old:    30
      >3 months:       10
    """
    # get_active_jobs returns opening_date; fall back to posted_date (created_at alias)
    raw_date = job.get("opening_date") or job.get("posted_at") or job.get("posted_date")
    if not raw_date:
        return 30.0  # unknown — deprioritise relative to dated postings
    try:
        posted_date = date.fromisoformat(str(raw_date)[:10])
    except Exception:
        return 30.0

    today = date.today()
    # Same calendar month → always top priority
    if posted_date.year == today.year and posted_date.month == today.month:
        return 100.0

    # Months elapsed (approximate)
    months_old = (today.year - posted_date.year) * 12 + (today.month - posted_date.month)
    if months_old <= 1:
        return 70.0
    elif months_old <= 2:
        return 50.0
    elif months_old <= 3:
        return 30.0
    else:
        return 10.0


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
      Score = (Preference Match × 0.30) + (Recency × 0.50) + (Diversity × 0.20)

    Returns a float 0–100.
    """
    already_selected = already_selected or []
    pref = _preference_score(job, student)
    rec  = _recency_score(job)
    div  = _diversity_score(job, already_selected)
    return round(pref * 0.30 + rec * 0.50 + div * 0.20, 1)


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


def _role_type_matches(job: dict, student: dict) -> bool:
    """
    Return True if the job's role_type aligns with the student's status preference.

    grad-program  → wants internship_grad roles (intern, placement, grad scheme)
    full-time     → wants entry_level roles (analyst, associate, junior, etc.)
    any other / unset status → no preference; all jobs are treated as matching.

    Jobs with no/unknown role_type are always treated as primary (matching)
    so they are never incorrectly relegated to the secondary fallback pool.
    """
    status    = student.get("status") or ""
    role_type = job.get("role_type") or ""

    if not role_type:
        return True  # unknown type — keep in primary pool rather than discarding

    if status == "grad-program":
        return role_type == "internship_grad"
    if status == "full-time":
        return role_type == "entry_level"
    return True  # unknown student status — treat everything as matching


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
    "Data & Analytics":   "Be analytical. Reference the role's data domain specifically.",
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

# Maps extracted department strings (lowercase) to natural phrases for the
# subject line "Student with a query on X"
_DEPARTMENT_PHRASES = {
    "risk management": "Risk Management",
    "quantitative risk": "Quantitative Risk",
    "quantitative research": "Quantitative Research",
    "quantitative finance": "Quantitative Finance",
    "quantitative trading": "Quantitative Trading",
    "investment banking": "Investment Banking",
    "investment management": "Investment Management",
    "private equity": "Private Equity",
    "venture capital": "Venture Capital",
    "asset management": "Asset Management",
    "portfolio management": "Portfolio Management",
    "capital markets": "Capital Markets",
    "equity research": "Equity Research",
    "fixed income": "Fixed Income",
    "corporate finance": "Corporate Finance",
    "corporate development": "Corporate Development",
    "mergers and acquisitions": "M&A",
    "m&a": "M&A",
    "trading": "Trading",
    "sales and trading": "Sales & Trading",
    "software engineering": "Software Engineering",
    "data science": "Data Science",
    "data analytics": "Data Analytics",
    "data engineering": "Data Engineering",
    "machine learning": "Machine Learning",
    "artificial intelligence": "AI",
    "product management": "Product Management",
    "product": "Product",
    "technology": "Technology",
    "consulting": "Consulting",
    "strategy": "Strategy",
    "strategy and operations": "Strategy & Operations",
    "business development": "Business Development",
    "operations": "Operations",
    "marketing": "Marketing",
    "research": "Research",
    "policy": "Policy",
    "economics": "Economics",
    "finance": "Finance",
    "accounting": "Accounting",
    "compliance": "Compliance",
    "legal": "Legal",
    "human resources": "Human Resources",
    "actuarial": "Actuarial",
    "insurance": "Insurance",
    "real estate": "Real Estate",
    "infrastructure": "Infrastructure",
    "energy": "Energy",
    "sustainability": "Sustainability",
    "healthcare": "Healthcare",
    "public policy": "Public Policy",
    "impact investing": "Impact Investing",
}


def _job_department(job_title: str) -> str:
    """
    Extract a clean, natural-sounding department phrase from a job title.
    e.g. '2026 Risk Management (Quant) Summer Associate' → 'Risk Management'
         'Software Engineering Intern' → 'Software Engineering'
    Looks up the extracted phrase in _DEPARTMENT_PHRASES for a canonical form.
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
    extracted = result or title.strip()
    # Look up canonical phrase (case-insensitive)
    return _DEPARTMENT_PHRASES.get(extracted.lower(), extracted)


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


def _generate_with_claude(student: dict, lead: dict, job: dict) -> tuple[str, str] | None:
    """
    Generate a personalised cold-email draft via Claude API.
    Returns (subject, body) or None if generation fails.
    """
    import os
    import anthropic as _anthropic

    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("CLAUDE2_API_KEY")
    if not api_key:
        return None

    name_parts     = (lead.get("name") or "").split()
    first_name     = name_parts[0] if name_parts else "there"
    student_name   = (student.get("name") or "").strip()
    university     = (student.get("university") or "").strip()
    bio            = (student.get("bio") or "").strip()
    company        = (lead.get("company") or job.get("company_name") or "the company").strip()
    department     = _job_department(job.get("title", ""))
    industry_hint  = _get_industry_tone(job)
    is_alumni      = bool(lead.get("is_alumni"))

    identity_line = f"I'm a{' ' + university if university else ''} student"
    if bio:
        identity_line = bio
    alumni_note = f" (I noticed you also studied at {university})" if is_alumni and university else ""

    prompt = f"""Write a short cold-email from a student to a professional at {company}{alumni_note}.

Context:
- Recipient: {first_name}, works in {department} at {company}
- Sender: {student_name or 'a student'}. {identity_line}
- Industry tone: {industry_hint}

Rules:
- Max 80 words in the body
- 2–3 short paragraphs
- No flattery, no "I hope this email finds you well"
- End with a single specific question (not "can we chat?" — ask something about their work, team or how they got into the role)
- Sign off with just the student's first name
- Output JSON only: {{"subject": "...", "body": "..."}}
- Body must use \\n for line breaks (no HTML)"""

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (msg.content[0].text or "").strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        subject = (data.get("subject") or "").strip()
        body    = (data.get("body") or "").strip()
        if subject and body and len(body.split()) <= 200:
            return subject, body
    except Exception as e:
        logger.debug(f"Claude draft generation failed: {e}")
    return None


def generate_email_draft(
    student: dict,
    lead:    dict,
    job:     dict,
) -> tuple[str, str]:
    """
    Generate subject + body for a cold-email draft using the standard template.
    Returns (subject, body).
    """
    from pipeline.email_templates import template_standard

    name_parts = (lead.get("name") or "").split()
    ctx = {
        "student_name":         student.get("name") or student.get("first_name") or "",
        "student_university":   student.get("university") or "",
        "student_bio":          student.get("bio") or "",
        "recipient_first_name": name_parts[0] if name_parts else "there",
        "recipient_company":    lead.get("company") or job.get("company_name") or "your company",
        "job_department":       _job_department(job.get("title", "")),
        "industry_hint":        _get_industry_tone(job),
    }
    return template_standard(ctx)


# ── Industry → dept_tag mapping for lead filtering ────────────────────────────

_INDUSTRY_DEPT_TAGS: dict[str, set[str]] = {
    "Finance":            {"risk", "asset_management", "quant", "equity_research",
                           "sales_trading", "investment_banking"},
    "Investment Banking": {"investment_banking"},
    "Software Engineering": {"software_engineering", "infrastructure", "data_ml"},
    "Technology":         {"product", "data_ml", "infrastructure"},
    "Data & Analytics":   {"data_ml"},
    "Product Management": {"product"},
    "Consulting":         {"consulting"},
    "Law":                {"law_corporate"},
    "Marketing":          {"marketing"},
    "Healthcare":         {"healthcare"},
}


def _relevant_dept_tags(student_industries: list[str]) -> set[str]:
    """Return the set of dept_tags that correspond to a student's chosen industries."""
    tags: set[str] = set()
    for ind in student_industries:
        tags.update(_INDUSTRY_DEPT_TAGS.get(ind or "", set()))
    return tags


# ── Main daily card generation ────────────────────────────────────────────────

def generate_daily_cards(student_id: int, db_path=DB_PATH,
                         claimed_this_run: set | None = None) -> list[dict]:
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
    raw_inds = student.get("industries") or "[]"
    if isinstance(raw_inds, list):
        student_industries = raw_inds          # Postgres JSONB already parsed
    else:
        try:
            student_industries = json.loads(raw_inds)
        except Exception:
            student_industries = []

    # Strip nulls introduced by frontend (JSON null → Python None)
    student_industries = [i for i in student_industries if i is not None]

    with db_conn(db_path) as conn:
        jobs = get_active_jobs(
            conn,
            industries  = student_industries,
            region      = student.get("region") or "UK",
            days_fresh  = 14,
            limit       = 300,
        )

    # Post-query safety filter: enforce industry match even if DB filter was loose
    if student_industries:
        jobs = [j for j in jobs if j.get("industry") in student_industries]

    # Exclude jobs with no URL — cards must have a linked role
    jobs = [j for j in jobs if (j.get("url") or "").strip()]

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
    # Cross-student dedup — don't surface a lead already shown to another student in the last 3 days
    recently_matched = get_recently_matched_linkedin_urls(days=3)

    cards_written: list[dict] = []
    industries_used: dict = {}

    # Split jobs into primary (matching role_type) and secondary (fallback).
    # Primary is exhausted first; secondary is only used when primary runs out
    # and quota hasn't been filled.  This ensures a grad-seeking student always
    # sees internship/grad jobs first, and a full-time student always sees
    # entry-level jobs first — while still allowing leads from companies that
    # only have the "wrong" type of job to surface as a last resort.
    primary_jobs   = [j for j in jobs if _role_type_matches(j, student)]
    secondary_jobs = [j for j in jobs if not _role_type_matches(j, student)]

    # Start with the primary pool.  The main loop extends to secondary after
    # primary is exhausted (see the extend block inside the while loop below).
    remaining_jobs = list(primary_jobs)
    _secondary_added = False   # guard: extend to secondary at most once

    while len(cards_written) < quota and (remaining_jobs or (not _secondary_added and secondary_jobs)):
        # When primary pool is exhausted but quota not met, extend with secondary
        # (wrong role_type) jobs — this preserves access to leads at companies
        # that only have mismatched role types in the DB.
        if not remaining_jobs and not _secondary_added:
            remaining_jobs = list(secondary_jobs)
            _secondary_added = True
            logger.debug(
                f"Student {student_id}: primary pool exhausted after "
                f"{len(cards_written)}/{quota} cards — extending to "
                f"{len(secondary_jobs)} secondary (mismatched role_type) jobs"
            )
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
        leads = [l for l in leads if l.get("linkedin_url", "") not in recently_matched]
        # Also exclude leads claimed by other students earlier in this same scheduler run
        if claimed_this_run:
            leads = [l for l in leads if l.get("linkedin_url", "") not in claimed_this_run]

        # Only surface leads whose dept_tag aligns with the student's industries.
        # This prevents e.g. a law trainee at a finance firm appearing for a Finance student.
        relevant_tags = _relevant_dept_tags(student_industries)
        if relevant_tags:
            leads = [l for l in leads if not l.get("dept_tag") or l["dept_tag"] in relevant_tags]

        if not leads:
            continue

        # Score leads
        scored_leads = [
            (score_lead_v2(lead, job, student), lead) for lead in leads
        ]
        scored_leads.sort(key=lambda x: -x[0][0])
        (best_score, best_breakdown), best_lead = scored_leads[0]

        # (score threshold removed — all leads accepted regardless of score)

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
            "job_url":             job.get("url", ""),
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

    # Write daily match snapshot back onto the student row (overwrite each run)
    if cards_written:
        def _fmt(card):
            name  = card.get("person_name", "").strip()
            title = card.get("person_title", "").strip()
            return f"{name}, {title}" if title else name

        snap = {}
        for i, card in enumerate(cards_written[:3], start=1):
            snap[f"match{i}_job_url"]    = card.get("job_url", "") or ""
            snap[f"match{i}_name_title"] = _fmt(card)
            snap[f"match{i}_linkedin"]   = card.get("person_linkedin_url", "") or ""
        # Clear slots not filled this run
        for i in range(len(cards_written) + 1, 4):
            snap[f"match{i}_job_url"]    = None
            snap[f"match{i}_name_title"] = None
            snap[f"match{i}_linkedin"]   = None
        snap["matches_updated_date"] = today_str
        update_student_fields(student_id, snap)

    logger.info(f"Student {student_id}: {len(cards_written)} cards written for {today_str}")
    return cards_written


def generate_all_students_cards(db_path=DB_PATH):
    """Run daily card generation for every student.

    Students are processed newest-first (ORDER BY id DESC) so recently-joined
    students get first pick of leads.  A shared ``claimed_this_run`` set ensures
    the same lead cannot be assigned to more than one student in a single
    scheduler run — independent of what is already committed to the DB.
    """
    students = db_fetchall("SELECT id, name FROM students ORDER BY id DESC")

    # Shared exclusion set: LinkedIn URLs claimed by earlier students this run.
    # Complements get_recently_matched_linkedin_urls() which only covers URLs
    # already committed to the DB from previous runs / earlier today.
    claimed_this_run: set = set()

    logger.info(f"Generating daily cards for {len(students)} students")
    for s in students:
        try:
            cards = generate_daily_cards(s["id"], db_path, claimed_this_run=claimed_this_run)
            # Register every lead selected this run so subsequent students skip them
            for card in cards:
                url = card.get("person_linkedin_url", "")
                if url:
                    claimed_this_run.add(url)
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
