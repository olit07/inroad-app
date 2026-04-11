"""
inroad — Email Template

Single cold outreach template used as fallback when Claude API is unavailable.
"""


def _bio_line(ctx: dict) -> str:
    """Return the student's bio if set, otherwise a generic fallback."""
    bio = (ctx.get("student_bio") or "").strip()
    if bio:
        return bio
    # Example of a well-written intro:
    # I'm a 25-year-old MSc Economics student at LSE, where my master's thesis
    # focused on an econometric analysis of geopolitical risk. I hold two bachelor's
    # degrees and have prior experience launching my own business, but I'm now hoping
    # to build a career in geopolitical risk analysis.
    return (
        f"I'm a student at {ctx['student_university']} with a strong interest in "
        f"{ctx.get('industry_hint', 'this field')}."
    )


def template_standard(ctx: dict) -> tuple[str, str]:
    subject = f"Quick question about {ctx['recipient_company']}"
    body = f"""Hi {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails, so this will only take 30 seconds to read.

{_bio_line(ctx)}

What do you think are the most critical skills or qualities that an aspiring analyst on your team should possess?

I totally understand if you are too busy to reply.
Even a 1 or 2 line response will completely make my day.

All the best,
{ctx['student_name']}

{ctx['student_university']}"""
    return subject, body.strip()


TEMPLATES = [template_standard]


def check_quality(subject: str, body: str, ctx: dict) -> dict:
    """Basic quality check — used to decide whether to retry Claude generation."""
    words = len(body.split())
    has_name = bool(ctx.get("recipient_first_name", "")) and ctx["recipient_first_name"].lower() in body.lower()
    score = 10
    issues = []
    if words > 150:
        score -= 3
        issues.append(f"Too long ({words} words)")
    if words < 30:
        score -= 3
        issues.append(f"Too short ({words} words)")
    if not has_name:
        score -= 1
        issues.append("Missing recipient name")
    if not subject:
        score -= 1
        issues.append("Empty subject")
    return {"score": max(0, score), "issues": issues, "word_count": words, "has_name": has_name}


def build_ctx(student: dict, lead: dict, job: dict) -> dict:
    name_parts = (lead.get("name") or "").split()
    first = name_parts[0] if name_parts else "there"
    job_inds = job.get("industries", [])
    if isinstance(job_inds, str):
        import json as _j
        try:
            job_inds = _j.loads(job_inds)
        except Exception:
            job_inds = []
    industry_hint = job_inds[0] if job_inds else "this field"
    return {
        "student_name":         student.get("name", student.get("first_name", "there")),
        "student_university":   student.get("university", "my university"),
        "student_bio":          student.get("bio", ""),
        "recipient_name":       lead.get("name", ""),
        "recipient_first_name": first,
        "recipient_title":      lead.get("title", ""),
        "recipient_company":    lead.get("company", job.get("company_name", "")),
        "is_alumni":            bool(lead.get("is_alumni")),
        "job_title":            job.get("title", ""),
        "industry_hint":        industry_hint,
    }
