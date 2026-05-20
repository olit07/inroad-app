"""
inroad — Email Templates

Fallback pool used when Groq is unavailable or over quota.
Templates are inspired by the approved draft styles (1, 3, 6-10).
One is chosen at random per generation.
"""
import random


def _bio_intro(ctx: dict, fallback_dept: str = "finance") -> str:
    """Return bio prefixed with 'I'm a' unless it already starts that way."""
    bio = (ctx.get("student_bio") or "").strip().rstrip(".")
    # Normalise curly apostrophes so startswith checks work regardless of Groq output
    bio = bio.replace('’', "'").replace('‘', "'")
    if not bio:
        university = (ctx.get("student_university") or "").strip()
        dept = (ctx.get("job_department") or fallback_dept).lower()
        if university:
            return f"I'm a student at {university} with a keen interest in {dept}"
        return f"I'm a student with a keen interest in {dept}"
    lower = bio.lower()
    if lower.startswith("i'm ") or lower.startswith("i am "):
        return bio
    return f"I'm a {bio[0].lower()}{bio[1:]}"


# ── Template 1 — Dear / 30-second hook / skills question ──────────────────────

def template_hook_skills(ctx: dict) -> tuple[str, str]:
    first    = ctx.get("recipient_first_name") or "there"
    company  = ctx.get("recipient_company") or "your firm"
    dept     = ctx.get("job_department") or "your team"
    job_title = (ctx.get("job_title") or "").strip()
    name     = (ctx.get("student_name") or "").strip()
    intro    = _bio_intro(ctx, dept)
    alumni   = " I also noticed we share the same alma mater, small world!" if ctx.get("is_alumni") else ""

    prog_ref = f"the {job_title} programme at" if job_title else "a role at"
    subject  = f"Quick question about {dept} at {company}"
    body = f"""Dear {first},

I know you are incredibly busy and get a lot of emails so this will only take 30 seconds to read.

{intro}. I came across {prog_ref} {company} and wanted to reach out to you directly given your role on the team.{alumni}

What do you think are the most critical skills or qualities someone needs to break into {dept.lower()} today, and what attributes do you personally look for when building out your team?

I totally understand if you are too busy to reply. Even a 1 or 2 line response will completely make my day.

All the best,
{name or 'Best'}"""
    return subject, body.strip()


# ── Template 2 — Hi / impressed opener / one-sentence ask ────────────────────

def template_impressed(ctx: dict) -> tuple[str, str]:
    first     = ctx.get("recipient_first_name") or "there"
    company   = ctx.get("recipient_company") or "your firm"
    dept      = ctx.get("job_department") or "your team"
    job_title = (ctx.get("job_title") or "").strip()
    lead_title = (ctx.get("recipient_title") or dept).strip()
    name      = (ctx.get("student_name") or "").strip()
    intro     = _bio_intro(ctx, dept)

    prog = job_title or dept
    subject = f"Quick question about {dept} at {company}"
    body = f"""Hi {first},

I hope you don't mind the message. I came across your profile while researching opportunities at {company} and was impressed by your background in {lead_title} at {company}.

{intro}, currently applying for the {prog} role.
Would you be open to sharing any insight on the team or what you look for in candidates?

Thanks so much,
{name or 'Best'}"""
    return subject, body.strip()


# ── Template 3 — Hi / direct / firm kept coming up / what do you look for ────

def template_direct(ctx: dict) -> tuple[str, str]:
    first    = ctx.get("recipient_first_name") or "there"
    company  = ctx.get("recipient_company") or "your firm"
    dept     = ctx.get("job_department") or "your team"
    name     = (ctx.get("student_name") or "").strip()
    intro    = _bio_intro(ctx, dept)

    subject = f"Quick question about {dept} at {company}"
    body = f"""Hi {first},

{intro}. I spent time researching {dept.lower()} teams across the industry and {company} kept coming up as one of the firms doing the most interesting work.

I know reaching out cold is a long shot but I would genuinely value your perspective. What do you look for in candidates trying to break into {dept.lower()} at this level?

Thanks for reading,
{name or 'Best'}"""
    return subject, body.strip()


# ── Template 4 — Dear / came across profile / what has XP taught you ─────────

def template_experience(ctx: dict) -> tuple[str, str]:
    first     = ctx.get("recipient_first_name") or "there"
    company   = ctx.get("recipient_company") or "your firm"
    dept      = ctx.get("job_department") or "your team"
    job_title = (ctx.get("job_title") or "").strip()
    name      = (ctx.get("student_name") or "").strip()
    intro     = _bio_intro(ctx, dept)
    alumni    = " I also noticed we share the same alma mater, small world!" if ctx.get("is_alumni") else ""

    prog_ref  = f"the {job_title} programme" if job_title else f"{dept}"
    subject   = f"Quick question about {dept} at {company}"
    body = f"""Dear {first},

I came across your profile while looking into {company}'s {dept.lower()} team and wanted to reach out directly rather than just submitting an application into the void.

{intro}. I have been focusing my search on {prog_ref} and {company}'s positioning genuinely stood out.{alumni}

What has your experience at {company} taught you that you could not have learned anywhere else?

All the best,
{name or 'Best'}"""
    return subject, body.strip()


# ── Template 5 — Hi / quick one / applying for programme / advice ─────────────

def template_quick(ctx: dict) -> tuple[str, str]:
    first     = ctx.get("recipient_first_name") or "there"
    company   = ctx.get("recipient_company") or "your firm"
    dept      = ctx.get("job_department") or "your team"
    job_title = (ctx.get("job_title") or dept).strip()
    name      = (ctx.get("student_name") or "").strip()
    intro     = _bio_intro(ctx, dept)

    subject = f"Quick question about {dept} at {company}"
    body = f"""Hi {first},

Quick one. {intro}, applying for the {job_title} role at {company}. I came across your profile and thought reaching out directly made more sense than hoping my application stands out on its own.

Is there anything you wish you had known going into your first {dept.lower()} role?

Really appreciate your time either way.
{name or 'Best'}"""
    return subject, body.strip()


# ── Template 6 — Dear / keep this short / firm caught attention / misconception

def template_short(ctx: dict) -> tuple[str, str]:
    first    = ctx.get("recipient_first_name") or "there"
    company  = ctx.get("recipient_company") or "your firm"
    dept     = ctx.get("job_department") or "your team"
    name     = (ctx.get("student_name") or "").strip()
    intro    = _bio_intro(ctx, dept)

    subject = f"Quick question about {dept} at {company}"
    body = f"""Dear {first},

I know you are incredibly busy so I will keep this short.

{intro}. Something about the way {company} approaches {dept.lower()} caught my attention and I wanted to reach out to someone actually doing the work rather than just reading about it.

What is the most common misconception students have about working in {dept.lower()}?

Even a line or two would mean a lot.

All the best,
{name or 'Best'}"""
    return subject, body.strip()


# ── Template 7 — Hi / researching teams / not asking for call / curious ───────

def template_curious(ctx: dict) -> tuple[str, str]:
    first    = ctx.get("recipient_first_name") or "there"
    company  = ctx.get("recipient_company") or "your firm"
    dept     = ctx.get("job_department") or "your team"
    name     = (ctx.get("student_name") or "").strip()
    intro    = _bio_intro(ctx, dept)

    questions = [
        f"What drew you to {dept.lower()} and does it reward people who come from a quantitative background versus those who come from markets?",
        f"How did you come to be in your current role at {company}, and what has surprised you most about the work since joining?",
        f"What is one thing about working in {dept.lower()} at {company} that you would not have been able to guess from the outside?",
    ]
    question = random.choice(questions)

    subject = f"Quick question about {dept} at {company}"
    body = f"""Hi {first},

I hope you don't mind the message. {intro}, and I have been researching {dept.lower()} teams across the industry as I decide where to focus my applications. {company} stood out and your profile came up.

I am not going to ask for a call or a referral. I am just genuinely curious. {question}

Thanks so much,
{name or 'Best'}"""
    return subject, body.strip()


TEMPLATES = [
    template_hook_skills,
    template_impressed,
    template_direct,
    template_experience,
    template_quick,
    template_short,
    template_curious,
]


def check_quality(subject: str, body: str, ctx: dict) -> dict:
    words = len(body.split())
    has_name = bool(ctx.get("recipient_first_name", "")) and ctx["recipient_first_name"].lower() in body.lower()
    score = 10
    issues = []
    if words > 200:
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
