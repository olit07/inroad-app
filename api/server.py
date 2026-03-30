"""
api/server.py
Flask application entry point.
Run via: gunicorn --bind 0.0.0.0:$PORT api.server:app  (Railway)
      or: python api/server.py                           (local dev)
"""

import os
import sys
import hmac
import hashlib
import json
import secrets
import base64
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, request, jsonify, redirect, make_response,
    send_from_directory, g
)
from flask_cors import CORS

# Make sure project root is on the path when running as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    APP_BASE_URL, SESSION_SECRET, FROM_EMAIL, FROM_NAME,
    MAGIC_LINK_EXPIRY_MINUTES, MAGIC_LINK_RATE_LIMIT,
    MAGIC_LINK_RATE_WINDOW, SESSION_DAYS, ALLOWED_ORIGINS, DEV_MODE
)
from db.database import (
    init_db, get_student_by_email, get_student_by_id,
    create_student, upsert_student_profile,
    create_magic_token, get_and_consume_token,
    log_email, count_recent_tokens, fetchall, fetchone
)

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), ".."))

CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

# Initialise DB tables on startup — must be at module level so Gunicorn picks it up
init_db()

# Determine if we're on a secure host (Railway / any https origin)
IS_PRODUCTION = any("https://" in o for o in ALLOWED_ORIGINS) or not DEV_MODE

# ── Session helpers ──────────────────────────────────────────────────────────

COOKIE_NAME = "ccc_session"


def _sign(payload: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return sig


def make_session_cookie(student_id: int) -> str:
    payload = json.dumps({"id": student_id, "ts": datetime.utcnow().isoformat()})
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = _sign(b64)
    return f"{b64}.{sig}"


def read_session_cookie(cookie_value: str):
    try:
        b64, sig = cookie_value.rsplit(".", 1)
        if not hmac.compare_digest(_sign(b64), sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64.encode()).decode())
        return payload
    except Exception:
        return None


def set_session(response, student_id: int):
    value = make_session_cookie(student_id)
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="None" if IS_PRODUCTION else "Lax",
        path="/",
    )
    return response


def clear_session(response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


def require_session(f):
    """Decorator: reject request with 401 if no valid session."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        cookie = request.cookies.get(COOKIE_NAME)
        if not cookie:
            return jsonify({"error": "Not authenticated"}), 401
        payload = read_session_cookie(cookie)
        if not payload:
            return jsonify({"error": "Invalid session"}), 401
        student = get_student_by_id(payload["id"])
        if not student:
            return jsonify({"error": "Student not found"}), 401
        g.student = student
        return f(*args, **kwargs)
    return wrapper


# ── Magic link helpers ───────────────────────────────────────────────────────

def send_magic_link(email: str, token: str):
    """Send a magic-link email via Resend REST API."""
    import requests as req

    verify_url = f"{APP_BASE_URL}/auth/verify?token={token}"

    html = f"""
    <div style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;background:#ffffff;">
      <div style="background:#1F4530;border-radius:12px;padding:28px 24px;margin-bottom:24px;text-align:center;">
        <svg width="36" height="36" viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg" style="display:inline-block;"><rect width="88" height="88" rx="22" fill="rgba(255,255,255,0.15)"/><path d="M26 24 L54 44 L26 64" stroke="white" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><line x1="54" y1="44" x2="70" y2="44" stroke="white" stroke-width="8" stroke-linecap="round"/></svg>
        <p style="color:#ffffff;font-size:1.1rem;font-weight:700;margin:8px 0 0;letter-spacing:-0.02em;font-family:Georgia,serif;">inroad</p>
      </div>
      <h2 style="font-size:1.4rem;font-weight:700;color:#111110;margin:0 0 12px;letter-spacing:-0.01em;">Your magic link is ready.</h2>
      <p style="color:#6E6860;font-size:0.9rem;line-height:1.7;margin:0 0 24px;">Click the button below to sign in. The link expires in {MAGIC_LINK_EXPIRY_MINUTES} minutes.</p>
      <a href="{verify_url}" style="display:inline-block;background:#1F4530;color:#ffffff;font-size:0.9rem;font-weight:700;padding:14px 28px;border-radius:10px;text-decoration:none;">Sign in to inroad →</a>
      <p style="color:#A8A09A;font-size:0.75rem;margin:24px 0 0;line-height:1.6;">If you didn't request this, you can safely ignore this email.<br>Link: <a href="{verify_url}" style="color:#1F4530;">{verify_url}</a></p>
    </div>
    """

    try:
        resp = req.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {os.environ.get('RESEND_API_KEY', '')}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"{FROM_NAME} <{FROM_EMAIL}>",
                "to": [email],
                "subject": "Your inroad magic link",
                "html": html,
            },
            timeout=10,
        )
        data = resp.json()
        log_email(email, "Magic link", "magic_link", data.get("id"))
        return True
    except Exception as e:
        print(f"[email] Failed to send magic link: {e}")
        return False


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()})


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/auth/magic-link", methods=["POST"])
def magic_link():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400

    # Rate limit: max 3 requests per 10 min per email
    if count_recent_tokens(email, MAGIC_LINK_RATE_WINDOW) >= MAGIC_LINK_RATE_LIMIT:
        return jsonify({"error": "Too many requests. Please wait a few minutes."}), 429

    token = secrets.token_urlsafe(32)
    expires_at_dt = datetime.now(timezone.utc) + timedelta(minutes=MAGIC_LINK_EXPIRY_MINUTES)

    # Store as ISO string — works for both SQLite and Postgres
    expires_at_str = expires_at_dt.isoformat()

    create_magic_token(email, token, expires_at_str)

    if DEV_MODE:
        # In dev mode, print the link to console instead of emailing
        print(f"\n[DEV] Magic link for {email}:")
        print(f"  {APP_BASE_URL}/auth/verify?token={token}\n")
        return jsonify({"status": "sent", "dev_token": token})

    ok = send_magic_link(email, token)
    if not ok:
        return jsonify({"error": "Failed to send email. Please try again."}), 500

    return jsonify({"status": "sent"})


@app.route("/auth/verify")
def verify():
    token = request.args.get("token", "").strip()
    if not token:
        return redirect("/signup?error=invalid")

    row = get_and_consume_token(token)
    if not row:
        return redirect("/signup?error=expired")

    email = row["email"]

    # Ensure student exists
    student = get_student_by_email(email)
    if not student:
        student = create_student(email)

    # Determine where to redirect
    has_profile = bool(student and student.get("name"))
    destination = "/dashboard" if has_profile else "/onboarding"

    resp = make_response(redirect(destination))
    set_session(resp, student["id"])
    return resp


@app.route("/auth/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"status": "ok"}))
    clear_session(resp)
    return resp


# ── Current user ─────────────────────────────────────────────────────────────

@app.route("/api/me")
@require_session
def me():
    return jsonify(g.student)


# ── Student profile ───────────────────────────────────────────────────────────

@app.route("/api/students/register", methods=["POST"])
@require_session
def register_student():
    data = request.get_json(silent=True) or {}
    email = g.student["email"]

    student = upsert_student_profile(
        email=email,
        name=data.get("name", ""),
        age=data.get("age"),
        status=data.get("status", ""),
        industries=json.dumps(data.get("industries", [])),
        company_size=data.get("companySize", ""),
        bio=data.get("bio", ""),
        university=data.get("university", g.student.get("university", "")),
    )
    return jsonify(student)


# ── Matches ───────────────────────────────────────────────────────────────────

@app.route("/api/matches/today/<int:student_id>")
@require_session
def matches_today(student_id):
    if g.student["id"] != student_id:
        return jsonify({"error": "Forbidden"}), 403

    rows = fetchall("""
        SELECT m.*, j.title as job_title, j.company, j.url as job_url,
               j.location, j.industry, j.posted_at
        FROM matches m
        JOIN jobs j ON j.id = m.job_id
        WHERE m.student_id = ?
          AND DATE(m.created_at) = DATE('now')
        ORDER BY m.is_alumni DESC, m.created_at ASC
        LIMIT 3
    """, (student_id,))

    return jsonify(rows)


# ── Signals ───────────────────────────────────────────────────────────────────

@app.route("/api/signals", methods=["POST"])
@require_session
def record_signal():
    data = request.get_json(silent=True) or {}
    from db.database import execute as db_execute
    db_execute(
        "INSERT INTO signals (match_id, student_id, signal) VALUES (?, ?, ?)",
        (data.get("match_id"), g.student["id"], data.get("signal", ""))
    )
    return jsonify({"status": "ok"})


@app.route("/api/matches/<int:match_id>/reply", methods=["POST"])
def match_reply(match_id):
    from db.database import execute as db_execute
    db_execute(
        "UPDATE matches SET replied_at = NOW(), status = 'replied' WHERE id = ?",
        (match_id,)
    )
    return jsonify({"status": "ok"})


# ── Jobs ──────────────────────────────────────────────────────────────────────

@app.route("/api/jobs")
@require_session
def list_jobs():
    rows = fetchall("SELECT * FROM jobs ORDER BY posted_at DESC LIMIT 50")
    return jsonify(rows)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/api/admin/stats")
def admin_stats():
    stats = {
        "students":    (fetchone("SELECT COUNT(*) as n FROM students") or {}).get("n", 0),
        "matches":     (fetchone("SELECT COUNT(*) as n FROM matches") or {}).get("n", 0),
        "sent":        (fetchone("SELECT COUNT(*) as n FROM matches WHERE status='sent'") or {}).get("n", 0),
        "replied":     (fetchone("SELECT COUNT(*) as n FROM matches WHERE replied_at IS NOT NULL") or {}).get("n", 0),
        "tokens_today": (fetchone(
            "SELECT COUNT(*) as n FROM magic_tokens WHERE created_at > datetime('now', '-1 day')"
        ) or {}).get("n", 0),
    }
    total_sent = stats["sent"] or 1
    stats["reply_rate"] = round(stats["replied"] / total_sent * 100, 1)
    return jsonify(stats)


# ── HTML page routes ──────────────────────────────────────────────────────────

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _send_html(filename):
    return send_from_directory(ROOT, filename)


@app.route("/")
@app.route("/landing")
def landing():
    return _send_html("ccc-landing-final.html")


@app.route("/signup")
def signup():
    return _send_html("ccc-signup.html")


@app.route("/onboarding")
def onboarding():
    return _send_html("ccc-onboarding.html")


@app.route("/dashboard")
def dashboard():
    return _send_html("ccc-dashboard-live.html")


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5001))
    debug = DEV_MODE
    print(f"[CCC] Starting on port {port} | production={IS_PRODUCTION} | debug={debug}")
    app.run(host="0.0.0.0", port=port, debug=debug)
