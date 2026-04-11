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
from db.database import db_conn

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
    with db_conn(db_path) as conn:
        conn.executescript(TOKENS_SCHEMA)


def create_magic_token(email: str, purpose: str = "login", db_path=DB_PATH) -> str:
    """Generate and store a magic link token. Returns the token string."""
    init_tokens_table(db_path)
    token     = secrets.token_urlsafe(32)
    now       = datetime.utcnow()
    expires   = now + timedelta(hours=MAGIC_LINK_TTL_HOURS)
    with db_conn(db_path) as conn:
        conn.execute(
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
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM magic_tokens WHERE token=? AND used_at IS NULL AND expires_at > ?",
            (token, now)
        ).fetchone()
        if not row:
            return None
        conn.execute(
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
    student_id = student.get("id")

    if not email:
        return False

    dash_link = f"{APP_BASE_URL}/dashboard"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#F5F5F2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="background:#F5F5F2;padding:40px 20px;">
<div style="background:#FFFFFF;border-radius:16px;max-width:520px;margin:0 auto;overflow:hidden;border:1px solid #E2DED8;">

  <div style="background:#1F4530;padding:36px 40px;">
    <div style="display:inline-flex;align-items:center;gap:12px;">
      <svg width="32" height="32" viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="88" height="88" rx="22" fill="rgba(255,255,255,0.18)"/><path d="M26 24 L54 44 L26 64" stroke="white" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><line x1="54" y1="44" x2="70" y2="44" stroke="white" stroke-width="8" stroke-linecap="round"/></svg>
      <span style="font-family:Georgia,serif;font-weight:700;font-size:1.4rem;color:#FFFFFF;letter-spacing:-0.02em;">inroad</span>
    </div>
  </div>

  <div style="padding:36px 40px;">
    <h2 style="margin:0 0 12px;font-size:1.2rem;font-weight:700;color:#1A1714;">
      Your matches for today are ready, {first_name}.
    </h2>
    <p style="margin:0 0 28px;color:#7A7068;line-height:1.65;font-size:0.95rem;">
      We've found {n_cards} people who could get you through the door.
      Each one is connected to a live opening that fits your profile —
      draft emails are already written.
    </p>
    <a href="{dash_link}" style="display:inline-block;background:#1F4530;color:#FFFFFF;padding:13px 28px;border-radius:10px;text-decoration:none;font-weight:700;font-size:0.9rem;">
      See today's matches →
    </a>
  </div>

  <div style="padding:20px 40px;border-top:1px solid #E2DED8;">
    <p style="margin:0;font-size:0.78rem;color:#ADA79F;">
      inroad · <a href="{APP_BASE_URL}/settings" style="color:#ADA79F;">Manage notifications</a>
    </p>
  </div>

</div>
</div>
</body></html>"""

    text = (
        f"Your matches for today are ready, {first_name}.\n\n"
        f"See them here: {dash_link}\n\n"
        f"— inroad"
    )
    return _send(email, "Your matches for today are ready", html, text)


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
