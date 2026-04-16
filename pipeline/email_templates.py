"""
inroad — Email Template

Single cold outreach template used as fallback when Claude API is unavailable.
"""


def template_standard(ctx: dict) -> tuple[str, str]:
    subject = f"Student with a query on {ctx.get('job_department') or 'your team'}"

    bio = (ctx.get("student_bio") or "").strip()
    bio_line = f"{bio}\n\n" if bio else ""

    student_name = (ctx.get("student_name") or "").strip()
    university   = (ctx.get("student_university") or "").strip()
    sign_off_parts = [p for p in [student_name, university] if p]
    sign_off = "\n".join(sign_off_parts)

    body = f"""Hi {ctx['recipient_first_name']},

I know you are incredibly busy and get a lot of emails, so this will only take 30 seconds to read.

{bio_line}What do you think are the most critical skills or qualities that an aspiring analyst on your team should possess?

I totally understand if you are too busy to reply.
Even a 1 or 2 line response will completely make my day.

All the best,
{sign_off}"""
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
        "student_name":         student.get("name", student.get("first_name", "")),
        "student_university":   student.get("university", ""),
        "student_bio":          student.get("bio", ""),
        "recipient_name":       lead.get("name", ""),
        "recipient_first_name": first,
        "recipient_title":      lead.get("title", ""),
        "recipient_company":    lead.get("company", job.get("company_name", "")),
        "is_alumni":            bool(lead.get("is_alumni")),
        "job_title":            job.get("title", ""),
        "industry_hint":        industry_hint,
    }
