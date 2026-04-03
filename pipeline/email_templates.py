"""
CCC — Email Templates

5 distinct email styles with quality scoring.
Each template is deterministically selected per (student_id, day_of_year).
"""
import re
from datetime import date

BANNED_PHRASES = [
    "i hope this finds you well", "i hope this email finds you",
    "reach out", "touch base", "synergy", "passionate about",
    "leverage", "circle back", "at your earliest convenience",
    "please don't hesitate", "i wanted to", "i am writing to",
    "as per", "going forward", "game changer",
]


def count_words(text: str) -> int:
    return len(text.split())


def check_quality(subject: str, body: str, ctx: dict) -> dict:
    score = 10
    issues = []
    banned_found = []
    combined = (subject + " " + body).lower()

    word_count = count_words(body)
    if word_count > 110:
        score -= 3
        issues.append(f"Too long ({word_count} words, max 110)")
    elif word_count < 40:
        score -= 2
        issues.append(f"Too short ({word_count} words)")

    for phrase in BANNED_PHRASES:
        if phrase in combined:
            score -= 1
            banned_found.append(phrase)

    has_name = bool(ctx.get("recipient_first_name", "")) and ctx["recipient_first_name"].lower() in body.lower()
    has_role = bool(ctx.get("job_title", "")) and any(w.lower() in body.lower() for w in ctx["job_title"].split()[:3])

    if not has_name:
        score -= 1; issues.append("Missing recipient name")
    if not has_role:
        score -= 1; issues.append("Missing role reference")
    if not subject:
        score -= 1; issues.append("Empty subject")

    return {
        "score":              max(0, score),
        "issues":             issues,
        "word_count":         word_count,
        "has_name":           has_name,
        "has_role":           has_role,
        "banned_phrases_found": banned_found,
    }


def select_template(student_id: int, match_date: str | None = None) -> int:
    """Returns 0–4 deterministically per student per day."""
    d = date.fromisoformat(match_date) if match_date else date.today()
    return (student_id + d.timetuple().tm_yday) % 5


# ── Templates ─────────────────────────────────────────────────────────────────

def _bio_line(ctx: dict) -> str:
    """Return the student's bio if set, otherwise a generic fallback."""
    bio = (ctx.get("student_bio") or "").strip()
    if bio:
        return bio
    return f"I'm a student at {ctx['student_university']} with a strong interest in {ctx.get('industry_hint', 'this field')}."


def template_direct(ctx: dict) -> tuple[str, str]:
    """Short, direct — specific question about getting into the role."""
    subject = f"Quick question — {ctx['job_title']} at {ctx['recipient_company']}"
    body = f"""Dear {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails so this will only take 30 seconds to read.

{_bio_line(ctx)}

What do you think are the most important things a candidate should demonstrate to stand out for a {ctx['job_title']} role at {ctx['recipient_company']}?

I totally understand if you are too busy to reply. Even a 1 or 2 line response will completely make my day.

All the best,
{ctx['student_name']}"""
    return subject, body.strip()


def template_narrative(ctx: dict) -> tuple[str, str]:
    """Asks about their career path — focuses on the person, not the role."""
    subject = f"Your path into {ctx['recipient_company']} — quick question"
    body = f"""Dear {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails so this will only take 30 seconds to read.

{_bio_line(ctx)}

I came across your profile while researching the {ctx['job_title']} opening at {ctx['recipient_company']}. What's the one thing you wish you had known before making the move into {ctx.get('industry_hint', 'your field')}?

I totally understand if you are too busy to reply. Even a 1 or 2 line response will completely make my day.

All the best,
{ctx['student_name']}"""
    return subject, body.strip()


def template_alumni(ctx: dict) -> tuple[str, str]:
    """Alumni connection — warmest tone."""
    subject = f"Fellow {ctx['student_university']} student — quick question about {ctx['recipient_company']}"
    body = f"""Dear {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails so this will only take 30 seconds to read.

{_bio_line(ctx)} I noticed you also studied at {ctx['student_university']} before joining {ctx['recipient_company']}.

What attributes do you look for when hiring for the {ctx['job_title']} team, and what would you prioritise developing as a student today?

I totally understand if you are too busy to reply. Even a 1 or 2 line response will completely make my day.

All the best,
{ctx['student_name']}"""
    return subject, body.strip()


def template_curiosity(ctx: dict) -> tuple[str, str]:
    """Genuine curiosity — asks about skills and market dynamics."""
    subject = f"Question about the {ctx['job_title']} role at {ctx['recipient_company']}"
    body = f"""Dear {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails so this will only take 30 seconds to read.

{_bio_line(ctx)}

What do you think are the most critical skills or qualities someone needs to succeed in a {ctx['job_title']} role at {ctx['recipient_company']}, given how much the {ctx.get('industry_hint', 'industry')} space is evolving?

I totally understand if you are too busy to reply. Even a 1 or 2 line response will completely make my day.

All the best,
{ctx['student_name']}"""
    return subject, body.strip()


def template_referral_prep(ctx: dict) -> tuple[str, str]:
    """Research before applying — humble and low-pressure."""
    subject = f"Considering the {ctx['job_title']} role at {ctx['recipient_company']} — a quick question"
    body = f"""Dear {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails so this will only take 30 seconds to read.

{_bio_line(ctx)} I am currently considering applying for the {ctx['job_title']} opening at {ctx['recipient_company']}.

Before I do, I would love to hear — what is one thing you would want an applicant to genuinely understand about the role or the team that the job description doesn't capture?

I totally understand if you are too busy to reply. Even a 1 or 2 line response will completely make my day.

All the best,
{ctx['student_name']}"""
    return subject, body.strip()


TEMPLATES = [
    template_direct,
    template_narrative,
    template_alumni,
    template_curiosity,
    template_referral_prep,
]

TEMPLATE_NAMES = ["direct", "narrative", "alumni", "curiosity", "referral_prep"]


def get_template(index: int):
    return TEMPLATES[index % len(TEMPLATES)]


def render_template(index: int, ctx: dict) -> tuple[str, str]:
    fn = get_template(index)
    subject, body = fn(ctx)
    return subject, body


def build_ctx(student: dict, lead: dict, job: dict) -> dict:
    name_parts = (lead.get("name") or "").split()
    first = name_parts[0] if name_parts else "there"
    tenure_months = lead.get("tenure_months", 0) or 0

    # Industry hint from job
    job_inds = job.get("industries", [])
    if isinstance(job_inds, str):
        import json as _j
        try:
            job_inds = _j.loads(job_inds)
        except Exception:
            job_inds = []
    industry_hint = job_inds[0] if job_inds else "this field"

    return {
        "student_name":          student.get("first_name", "there"),
        "student_university":    student.get("university", "my university"),
        "student_bio":           student.get("bio", ""),
        "student_cal_link":      student.get("cal_link", "cal.com/me/coffee-chat"),
        "recipient_name":        lead.get("name", ""),
        "recipient_first_name":  first,
        "recipient_title":       lead.get("title", ""),
        "recipient_company":     lead.get("company", job.get("company_name", "")),
        "is_alumni":             bool(lead.get("is_alumni")),
        "job_title":             job.get("title", ""),
        "job_url":               job.get("url", ""),
        "tenure_years":          round(tenure_months / 12, 1),
        "industry_hint":         industry_hint,
    }
