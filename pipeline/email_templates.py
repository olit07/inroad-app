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

def template_direct(ctx: dict) -> tuple[str, str]:
    """Short, confident, direct."""
    subject = f"Quick question about the {ctx['job_title']} role at {ctx['recipient_company']}"
    body = f"""Hi {ctx['recipient_first_name']},

I came across the {ctx['job_title']} opening at {ctx['recipient_company']} and your background stood out.

I'm a student at {ctx['student_university']} with a strong interest in this area. Would you be open to 20 minutes to share how you got into your role?

Thanks,
{ctx['student_name']}"""
    return subject, body.strip()


def template_narrative(ctx: dict) -> tuple[str, str]:
    """One story hook, asks for perspective."""
    tenure = f"{int(ctx.get('tenure_years', 1))} year{'s' if ctx.get('tenure_years', 1) != 1 else ''}"
    subject = f"{ctx['student_university']} student — your path to {ctx['recipient_company']}"
    body = f"""Hi {ctx['recipient_first_name']},

After {tenure} at {ctx['recipient_company']}, you've clearly built something real in {ctx.get('industry_hint', 'this space')}. I'm finishing my degree at {ctx['student_university']} and trying to understand what that path actually looks like from the inside.

I spotted the {ctx['job_title']} role and it caught my attention. Would you spare 20 minutes?

{ctx['student_name']}"""
    return subject, body.strip()


def template_alumni(ctx: dict) -> tuple[str, str]:
    """Alumni-first. Warmest tone."""
    subject = f"Fellow {ctx['student_university']} student — {ctx['job_title']} at {ctx['recipient_company']}"
    body = f"""Hi {ctx['recipient_first_name']},

I noticed you studied at {ctx['student_university']} — I'm currently in my final year there.

I came across the {ctx['job_title']} opening at {ctx['recipient_company']} and I'd love to hear how you made the transition. Even 20 minutes would be incredibly helpful.

Thanks so much,
{ctx['student_name']}"""
    return subject, body.strip()


def template_curiosity(ctx: dict) -> tuple[str, str]:
    """Asks a specific question. Less about student, more about them."""
    subject = f"Question about your work at {ctx['recipient_company']}"
    body = f"""Hi {ctx['recipient_first_name']},

I've been researching {ctx['recipient_company']} and reading about the {ctx['job_title']} team's work. One thing I'm genuinely curious about: what does the day-to-day actually look like compared to what's in the job description?

I'm a student at {ctx['student_university']} exploring this area seriously. Would you be open to 20 minutes?

{ctx['student_name']}"""
    return subject, body.strip()


def template_referral_prep(ctx: dict) -> tuple[str, str]:
    """Frames the chat as research before applying. Less pushy."""
    subject = f"Doing research before applying — {ctx['job_title']} at {ctx['recipient_company']}"
    body = f"""Hi {ctx['recipient_first_name']},

I'm seriously considering applying for the {ctx['job_title']} role at {ctx['recipient_company']} and wanted to speak to someone who actually works there before I do.

I'm finishing up at {ctx['student_university']} and would really value your perspective. Would 20 minutes work?

Thanks,
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
