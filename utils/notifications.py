"""
CCC — Email Notification Layer (SMTP)

Sends two types of emails:
1. Magic link / verification email (on signup)
2. Daily "your matches are ready" nudge (on card generation)

Configure via environment variables:
    SMTP_HOST     default: smtp.gmail.com
    SMTP_PORT     default: 587
    SMTP_USER     your sender address
    SMTP_PASS     app password (not your account password)
    FROM_NAME     default: Coffee Chat Connect
    APP_BASE_URL  default: http://localhost:5001

For Gmail: create an App Password at myaccount.google.com/apppasswords
For SendGrid: use smtp.sendgrid.net port 587, user=apikey, pass=SG.xxx
For Resend: use smtp.resend.com port 465, user=resend, pass=re_xxx
"""
import os
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH
from db.database import db_conn, execute as db_execute, fetchone as db_fetchone

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL   = os.environ.get("FROM_EMAIL", "noreply@signin.the-inroad.com")
FROM_NAME    = os.environ.get("FROM_NAME", "inroad")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5001")
MAGIC_LINK_TTL_HOURS = 24


# ── Token table (appended to main DB) ────────────────────────────────────────
TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS magic_tokens (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    purpose     TEXT NOT NULL DEFAULT 'login',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_token_email ON magic_tokens(email);
"""


def init_tokens_table(db_path=DB_PATH):
    from db.database import USE_POSTGRES
    if USE_POSTGRES:
        return  # table already exists via Postgres migrations
    with db_conn(db_path) as conn:
        conn.executescript(TOKENS_SCHEMA)


def create_unsubscribe_token(email: str, db_path=DB_PATH) -> str:
    """Generate a long-lived (30-day) unsubscribe token."""
    init_tokens_table(db_path)
    token   = secrets.token_urlsafe(32)
    now     = datetime.utcnow()
    expires = now + timedelta(days=30)
    db_execute(
        "INSERT INTO magic_tokens (token,email,purpose,created_at,expires_at) VALUES (?,?,?,?,?)",
        (token, email.lower().strip(), "unsubscribe", now.isoformat(), expires.isoformat())
    )
    return token


def create_magic_token(email: str, purpose: str = "login", db_path=DB_PATH) -> str:
    """Generate and store a magic link token. Returns the token string."""
    init_tokens_table(db_path)
    token     = secrets.token_urlsafe(32)
    now       = datetime.utcnow()
    expires   = now + timedelta(hours=MAGIC_LINK_TTL_HOURS)
    db_execute(
        "INSERT INTO magic_tokens (token,email,purpose,created_at,expires_at) VALUES (?,?,?,?,?)",
        (token, email.lower().strip(), purpose, now.isoformat(), expires.isoformat())
    )
    return token


def verify_magic_token(token: str, db_path=DB_PATH) -> dict | None:
    """
    Verify a magic token. Returns {email, purpose} if valid, None if expired/used/invalid.
    Marks token as used on success.
    """
    init_tokens_table(db_path)
    now = datetime.utcnow().isoformat()
    row = db_fetchone(
        "SELECT * FROM magic_tokens WHERE token=? AND used_at IS NULL AND expires_at > ?",
        (token, now)
    )
    if not row:
        return None
    db_execute(
        "UPDATE magic_tokens SET used_at=? WHERE token=?",
        (now, token)
    )
    return {"email": row["email"], "purpose": row["purpose"]}


def _send(to: str, subject: str, html_body: str, text_body: str = "") -> bool:
    """Send an email via Resend REST API. Returns True on success."""
    api_key = RESEND_API_KEY
    if not api_key:
        logger.warning(f"RESEND_API_KEY not set — skipping email to {to}: {subject}")
        return False

    import requests as _req
    payload: dict = {
        "from":    f"{FROM_NAME} <{FROM_EMAIL}>",
        "to":      [to],
        "subject": subject,
        "html":    html_body,
    }
    if text_body:
        payload["text"] = text_body

    try:
        resp = _req.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Email sent to {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Resend send failed to {to}: {e}")
        return False


# ── Email templates ───────────────────────────────────────────────────────────

def send_magic_link(email: str, db_path=DB_PATH) -> bool:
    """Send a magic login link to the student's email."""
    token = create_magic_token(email, purpose="login", db_path=db_path)
    link  = f"{APP_BASE_URL}/auth/verify?token={token}"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;color:#1A1714">
<div style="margin-bottom:24px">
  <span style="font-size:1.4rem;font-weight:700">Coffee<span style="color:#1F4530">Chat</span>Connect</span>
</div>
<h2 style="font-weight:700;font-size:1.3rem;margin-bottom:8px">Here's your magic link</h2>
<p style="color:#7A7068;margin-bottom:24px">Click below to sign in. This link expires in 24 hours.</p>
<a href="{link}" style="display:inline-block;background:#1F4530;color:white;padding:12px 24px;
   border-radius:9px;text-decoration:none;font-weight:700;font-size:0.9rem">
  Sign in to Coffee Chat Connect →
</a>
<p style="margin-top:24px;font-size:0.8rem;color:#ADA79F">
  If you didn't request this, you can safely ignore this email.
</p>
</body></html>"""

    text = f"Your Coffee Chat Connect magic link:\n\n{link}\n\nExpires in 24 hours."
    return _send(email, "Your Coffee Chat Connect sign-in link", html, text)


def send_daily_matches_ready(student: dict, n_cards: int = 3, db_path=DB_PATH) -> bool:
    """Notify a student their daily matches are ready."""
    email      = student.get("email", "")
    full_name  = student.get("name", "") or ""
    first_name = full_name.split()[0] if full_name.strip() else "there"

    if not email:
        return False

    # Generate a magic token that lands straight on the dashboard
    token            = create_magic_token(email, purpose="login", db_path=db_path)
    dash_link        = f"{APP_BASE_URL}/verify?token={token}&next=/dashboard"
    unsub_token      = create_unsubscribe_token(email, db_path=db_path)
    unsub_link       = f"{APP_BASE_URL}/unsubscribe?token={unsub_token}"
    settings_link    = f"{APP_BASE_URL}/settings"

    # ── Avatar pool — identical to dashboard pickAvatar ──────────────────────
    _UB = 'https://images.unsplash.com/photo-'
    _UQ = '?w=150&h=150&fit=crop&crop=faces&q=80'
    AVATAR_POOL = [
        _UB + '1560250097-0b93528c311a' + _UQ,
        _UB + '1573496359142-b8d87734a5a2' + _UQ,
        _UB + '1507003211169-0a1dd7228f2d' + _UQ,
        _UB + '1500648767791-00dcc994a43e' + _UQ,
        _UB + '1566492031773-4f4e44671857' + _UQ,
        _UB + '1580489944761-15a19d654956' + _UQ,
        _UB + '1519085360753-af0119f7cbe7' + _UQ,
        _UB + '1531427186611-ecfd6d936c79' + _UQ,
        _UB + '1472099645785-5658abf4ff4e' + _UQ,
        _UB + '1438761681033-6461ffad8d80' + _UQ,
        _UB + '1552058544-f2b08422138a' + _UQ,
        _UB + '1570295999919-56ceb5ecca61' + _UQ,
        _UB + '1463453091185-61582044d556' + _UQ,
        _UB + '1594744803329-e58b31de8bf5' + _UQ,
        _UB + '1547425260-76bcadfb4f2c' + _UQ,
        _UB + '1534528741775-53994a69daeb' + _UQ,
        _UB + '1551836022-d5d88e9218df' + _UQ,
        _UB + '1567532939604-b6b5b0db2604' + _UQ,
        _UB + '1539571696357-5a69c17a67c6' + _UQ,
        _UB + '1506794778202-cad84cf45f1d' + _UQ,
    ]

    import ctypes as _ct
    def _pick_avatar(name):
        """Replicates JS pickAvatar: Math.imul(31,h) + charCode, then abs(h) % len."""
        h = 0
        for c in (name or ""):
            h = _ct.c_int32(31 * h + ord(c)).value
        return AVATAR_POOL[abs(h) % len(AVATAR_POOL)]

    # ── Fetch today's real cards from DB ──────────────────────────────────────
    from db.database import fetchall as _fetchall
    student_id = student.get("id")
    _rows = _fetchall(
        """SELECT m.person_name, m.person_title, m.person_company, m.expected_email,
                  j.title AS job_title, j.opening_date
           FROM matches m
           LEFT JOIN jobs j ON j.id = m.job_id
           WHERE m.student_id = %s AND m.match_date = CURRENT_DATE
           ORDER BY m.relevance_score DESC""",
        (student_id,),
    ) if student_id else []

    def _fmt_date(d):
        if not d:
            return ""
        try:
            from datetime import date as _date
            if isinstance(d, _date):
                return d.strftime("%-d %b %Y")
            return str(d)[:10]
        except Exception:
            return str(d)[:10]

    _opacities = [0.2, 0.15, 0.11, 0.08, 0.05]

    def _card_html(idx):
        row = _rows[idx] if idx < len(_rows) else {}
        name    = row.get("person_name") or "—"
        title   = (row.get("person_title") or "").replace("&", "&amp;")
        company = (row.get("person_company") or "").replace("&", "&amp;")
        role    = f"{title} · {company}" if title and company else (title or company)
        job     = (row.get("job_title") or "").replace("&", "&amp;")
        opened  = _fmt_date(row.get("opening_date"))
        mail    = row.get("expected_email") or ""
        av      = _pick_avatar(name)
        opc     = _opacities[idx] if idx < len(_opacities) else 0.2
        return f"""
      <!-- Card {idx+1} -->
      <div style="padding:8px 0;">
        <div style="filter:blur(8px);user-select:none;pointer-events:none;opacity:{opc};">
          <div style="background:#FFFFFF;border:1px solid #E2DED8;border-radius:10px;overflow:hidden;">
            <div style="padding:8px 10px;display:flex;align-items:flex-start;gap:14px;">
              <div style="flex:1;min-width:0;">
                <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:6px;margin-bottom:3px;">
                  <div style="min-width:0;overflow:hidden;">
                    <span style="font-size:0.63rem;font-weight:700;color:#1A1714;">{name}</span>
                    <span style="font-size:0.63rem;color:#ADA79F;"> – </span>
                    <span style="font-size:0.6rem;font-weight:500;color:#7A7068;">{role}</span>
                  </div>
                  <div style="background:#1A1714;color:#FFFFFF;border-radius:5px;padding:3px 7px;font-size:0.55rem;font-weight:600;white-space:nowrap;flex-shrink:0;">Draft →</div>
                </div>
                <div style="display:inline-flex;align-items:center;gap:3px;background:#F3F1EE;border:1px solid #E2DED8;border-radius:4px;padding:1px 6px;font-size:0.52rem;font-weight:500;color:#5A5450;max-width:100%;overflow:hidden;margin-bottom:2px;">
                  <div style="width:3px;height:3px;border-radius:50%;background:#1F4530;flex-shrink:0;"></div>
                  <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{job} ↗</span>
                </div>
                <div style="font-size:0.49rem;color:#ADA79F;">Opened: {opened} &nbsp;·&nbsp; {mail}</div>
              </div>
            </div>
          </div>
        </div>
      </div>"""

    _cards_html = "".join(_card_html(i) for i in range(n_cards))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#F5F5F2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="background:#F5F5F2;padding:40px 20px;">
<div style="background:#FFFFFF;border-radius:16px;max-width:580px;margin:0 auto;overflow:hidden;border:1px solid #E2DED8;">

  <!-- Header -->
  <div style="background:#1F4530;padding:32px 40px;">
    <div style="display:inline-flex;align-items:center;gap:12px;">
      <svg width="28" height="28" viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="88" height="88" rx="22" fill="rgba(255,255,255,0.18)"/><path d="M26 24 L54 44 L26 64" stroke="white" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><line x1="54" y1="44" x2="70" y2="44" stroke="white" stroke-width="8" stroke-linecap="round"/></svg>
      <span style="font-family:Georgia,serif;font-weight:700;font-size:1.35rem;color:#FFFFFF;letter-spacing:-0.02em;">inroad</span>
    </div>
  </div>

  <!-- Body -->
  <div style="padding:36px 40px 28px;">
    <h2 style="margin:0 0 14px;font-size:1.15rem;font-weight:700;color:#1A1714;line-height:1.3;">
      Your daily matches are ready, {first_name}.
    </h2>
    <p style="margin:0 0 24px;color:#7A7068;line-height:1.65;font-size:0.92rem;">
      We've found {n_cards} people connected to live openings that fit your profile, draft emails are written and waiting to be sent.
    </p>

    <!-- CTA button -->
    <a href="{dash_link}" style="display:inline-block;background:#1F4530;color:#FFFFFF;padding:13px 28px;border-radius:10px;text-decoration:none;font-weight:700;font-size:0.88rem;margin-bottom:28px;">
      See today's matches →
    </a>

    <!-- Card previews (blurred placeholders) -->
    <div style="display:flex;flex-direction:column;gap:8px;">
      {_cards_html}
    </div>
  </div>

  <!-- Footer -->
  <div style="padding:18px 40px;border-top:1px solid #E2DED8;">
    <p style="margin:0;font-size:0.76rem;color:#ADA79F;">
      inroad ·
      <a href="{settings_link}" style="color:#ADA79F;text-decoration:none;">Manage notifications</a>
      ·
      <a href="{unsub_link}" style="color:#ADA79F;text-decoration:none;">Unsubscribe</a>
    </p>
  </div>

</div>
</div>
</body></html>"""

    text = (
        f"Your daily matches are ready, {first_name}.\n\n"
        f"We've found {n_cards} people connected to live openings that fit your profile, "
        f"draft emails are written and waiting to be sent.\n\n"
        f"See them here: {dash_link}\n\n"
        f"inroad"
    )
    return _send(email, "Your daily matches are ready", html, text)


def send_weekly_digest_email(student: dict, digest: dict) -> bool:
    """Send a weekly performance digest to a student."""
    email      = student.get("email", "")
    first_name = student.get("first_name", "there")
    sent       = digest.get("emails_sent", 0)
    replies    = digest.get("replies_received", 0)
    rate       = int(digest.get("response_rate", 0) * 100)
    streak     = digest.get("streak_days", 0)

    if not email:
        return False

    streak_line = f"🔥 {streak}-day streak" if streak >= 3 else ""

    html = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;color:#1A1714">
<div style="margin-bottom:24px">
  <span style="font-size:1.4rem;font-weight:700">Coffee<span style="color:#1F4530">Chat</span>Connect</span>
</div>
<h2 style="font-weight:700;font-size:1.2rem;margin-bottom:16px">Your week in numbers, {first_name}</h2>
<div style="display:flex;gap:16px;margin-bottom:24px">
  <div style="flex:1;background:#F3F1EE;border-radius:10px;padding:16px;text-align:center">
    <div style="font-size:2rem;font-weight:900;color:#1F4530">{sent}</div>
    <div style="font-size:0.8rem;color:#7A7068">emails sent</div>
  </div>
  <div style="flex:1;background:#F3F1EE;border-radius:10px;padding:16px;text-align:center">
    <div style="font-size:2rem;font-weight:900;color:#1F4530">{replies}</div>
    <div style="font-size:0.8rem;color:#7A7068">replies</div>
  </div>
  <div style="flex:1;background:#F3F1EE;border-radius:10px;padding:16px;text-align:center">
    <div style="font-size:2rem;font-weight:900;color:#1F4530">{rate}%</div>
    <div style="font-size:0.8rem;color:#7A7068">response rate</div>
  </div>
</div>
{f'<p style="color:#1F4530;font-weight:700;margin-bottom:16px">{streak_line}</p>' if streak_line else ''}
<p style="color:#7A7068;font-size:0.875rem">Keep going. Consistency is the whole game.</p>
</body></html>"""

    text = f"Your week: {sent} sent · {replies} replies · {rate}% rate. {streak_line}"
    return _send(email, f"Your week: {sent} emails, {replies} replies", html, text)
