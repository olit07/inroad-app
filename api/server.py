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
    MAGIC_LINK_RATE_WINDOW, SESSION_DAYS, ALLOWED_ORIGINS, DEV_MODE,
    JWT_REFRESH_TTL_DAYS
)
from db.database import (
    init_db, get_student_by_email, get_student_by_id,
    create_student, upsert_student_profile, update_student_fields,
    deactivate_student, revoke_all_tokens_for_student,
    create_magic_token, get_and_consume_token,
    log_email, count_recent_tokens, fetchall, fetchone,
    create_refresh_token, get_refresh_token, revoke_refresh_token,
    get_queued_cards, mark_card_consumed
)
from api.auth import make_access_token, make_refresh_token_str, require_jwt
from utils.university_lookup import detect_university

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

def send_magic_link(email: str, token: str, next_url: str = None):
    """Send a magic-link email via Resend REST API."""
    import requests as req
    from urllib.parse import urlencode

    qs = {"token": token}
    if next_url:
        qs["next"] = next_url
    verify_url = f"{APP_BASE_URL}/verify?{urlencode(qs)}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>Your inroad sign-in link</title>
</head>
<body style="margin:0;padding:0;background:#F5F5F2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
<div style="background:#F5F5F2;padding:40px 20px;">
<div style="background:#FFFFFF;border-radius:16px;max-width:520px;margin:0 auto;overflow:hidden;border:1px solid #E2DED8;">

  <!-- Header -->
  <div style="background:#1F4530;padding:36px 40px;">
    <div style="display:inline-flex;align-items:center;gap:12px;">
      <svg width="32" height="32" viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="88" height="88" rx="22" fill="rgba(255,255,255,0.18)"/><path d="M26 24 L54 44 L26 64" stroke="white" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><line x1="54" y1="44" x2="70" y2="44" stroke="white" stroke-width="8" stroke-linecap="round"/></svg>
      <span style="font-family:Georgia,serif;font-weight:700;font-size:1.4rem;color:#FFFFFF;letter-spacing:-0.02em;">inroad</span>
    </div>
    <div style="margin-top:16px;font-size:13px;color:rgba(255,255,255,0.5);font-weight:400;letter-spacing:0.02em;">Get into the workforce the smart way</div>
  </div>

  <!-- Body -->
  <div style="padding:44px 40px 36px;">

    <div style="display:inline-flex;align-items:center;gap:7px;background:#EBF4EE;border:1px solid #A8C9B0;border-radius:100px;padding:6px 14px;font-size:11px;font-weight:700;color:#1F4530;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:28px;">
      <span style="width:5px;height:5px;background:#1F4530;border-radius:50%;display:inline-block;"></span>
      Magic link
    </div>

    <h1 style="font-size:26px;font-weight:900;color:#111110;letter-spacing:-0.02em;line-height:1.15;margin:0 0 14px;">Your sign-in link<br>is <em style="font-style:italic;font-weight:300;color:#1F4530;">ready.</em></h1>

    <p style="font-size:15px;color:#6E6860;line-height:1.7;margin:0 0 36px;font-weight:400;">
      Click below to verify your email and start getting matched to real people
      at companies you want to work at &mdash; alumni first.
    </p>

    <div style="margin-bottom:40px;">
      <a href="{verify_url}" style="display:inline-block;background:#1F4530;color:#FFFFFF;font-size:15px;font-weight:700;padding:15px 32px;border-radius:10px;text-decoration:none;letter-spacing:0.01em;">Sign in to inroad &rarr;</a>
    </div>

    <!-- What happens next -->
    <div style="background:#F5F5F2;border-radius:14px;padding:28px 28px;margin-bottom:32px;">
      <div style="font-size:11px;font-weight:700;color:#6E6860;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:22px;">What happens next</div>
      <div style="display:flex;gap:14px;margin-bottom:20px;align-items:flex-start;">
        <div style="width:24px;height:24px;min-width:24px;background:#1F4530;border-radius:50%;color:#FFFFFF;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;margin-top:1px;">1</div>
        <div style="font-size:14px;color:#3A3733;line-height:1.6;"><strong style="font-weight:700;color:#111110;">Set up your profile</strong> &mdash; tell us your target role, industry, and company size.</div>
      </div>
      <div style="display:flex;gap:14px;margin-bottom:20px;align-items:flex-start;">
        <div style="width:24px;height:24px;min-width:24px;background:#1F4530;border-radius:50%;color:#FFFFFF;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;margin-top:1px;">2</div>
        <div style="font-size:14px;color:#3A3733;line-height:1.6;"><strong style="font-weight:700;color:#111110;">Get 3 matches every day</strong> &mdash; real people at companies with open roles, alumni prioritised.</div>
      </div>
      <div style="display:flex;gap:14px;align-items:flex-start;">
        <div style="width:24px;height:24px;min-width:24px;background:#1F4530;border-radius:50%;color:#FFFFFF;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;margin-top:1px;">3</div>
        <div style="font-size:14px;color:#3A3733;line-height:1.6;"><strong style="font-weight:700;color:#111110;">Send, they book</strong> &mdash; AI drafts the email, you approve, a scheduling link handles the rest.</div>
      </div>
    </div>

    <!-- Stats -->
    <table style="width:100%;border-collapse:separate;border-spacing:8px;margin-bottom:32px;">
      <tr>
        <td style="background:#F5F5F2;border-radius:10px;padding:16px;text-align:center;width:33%;">
          <div style="font-size:24px;font-weight:900;color:#1F4530;letter-spacing:-0.02em;line-height:1;">20%</div>
          <div style="font-size:11px;color:#6E6860;font-weight:400;margin-top:5px;line-height:1.4;">Average reply rate from alumni</div>
        </td>
        <td style="background:#F5F5F2;border-radius:10px;padding:16px;text-align:center;width:33%;">
          <div style="font-size:24px;font-weight:900;color:#1F4530;letter-spacing:-0.02em;line-height:1;">3</div>
          <div style="font-size:11px;color:#6E6860;font-weight:400;margin-top:5px;line-height:1.4;">Targeted matches per day</div>
        </td>
        <td style="background:#F5F5F2;border-radius:10px;padding:16px;text-align:center;width:33%;">
          <div style="font-size:24px;font-weight:900;color:#1F4530;letter-spacing:-0.02em;line-height:1;">72h</div>
          <div style="font-size:11px;color:#6E6860;font-weight:400;margin-top:5px;line-height:1.4;">Avg. time to first coffee chat</div>
        </td>
      </tr>
    </table>

    <hr style="border:none;border-top:1px solid #E2DED8;margin:32px 0;">

    <!-- Fallback link -->
    <div style="background:#F5F5F2;border:1px solid #E2DED8;border-radius:8px;padding:14px 16px;margin-bottom:8px;">
      <div style="font-size:10px;font-weight:700;color:#A8A09A;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:7px;">Button not working? Copy this link</div>
      <div style="font-size:11px;color:#6E6860;word-break:break-all;font-family:'Courier New',monospace;line-height:1.5;">{verify_url}</div>
    </div>

  </div>

  <!-- Footer -->
  <div style="padding:24px 40px;border-top:1px solid #E2DED8;text-align:center;">
    <div style="display:inline-flex;align-items:center;gap:8px;margin-bottom:10px;">
      <svg width="18" height="18" viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="88" height="88" rx="22" fill="#1F4530"/><path d="M26 24 L54 44 L26 64" stroke="white" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><line x1="54" y1="44" x2="70" y2="44" stroke="white" stroke-width="8" stroke-linecap="round"/></svg>
      <span style="font-family:Georgia,serif;font-size:13px;font-weight:700;color:#3A3733;letter-spacing:-0.01em;">inroad</span>
    </div>
    <div style="font-size:12px;color:#A8A09A;line-height:1.7;">
      This link expires in {MAGIC_LINK_EXPIRY_MINUTES} minutes and can only be used once.<br>
      If you didn&rsquo;t request this, you can safely ignore it.<br><br>
      <a href="#" style="color:#6E6860;text-decoration:underline;text-underline-offset:2px;">Privacy</a> &middot;
      <a href="#" style="color:#6E6860;text-decoration:underline;text-underline-offset:2px;">Terms</a>
    </div>
  </div>

</div>
</div>
</body>
</html>"""

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


def send_login_link(email: str, token: str, next_url: str = None):
    """Send a simple sign-in email for returning users."""
    import requests as req
    from urllib.parse import urlencode

    qs = {"token": token}
    if next_url:
        qs["next"] = next_url
    verify_url = f"{APP_BASE_URL}/verify?{urlencode(qs)}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to inroad</title>
</head>
<body style="margin:0;padding:0;background:#F5F5F2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
<div style="background:#F5F5F2;padding:40px 20px;">
<div style="background:#FFFFFF;border-radius:16px;max-width:520px;margin:0 auto;overflow:hidden;border:1px solid #E2DED8;">

  <!-- Header -->
  <div style="background:#1F4530;padding:36px 40px;">
    <div style="display:inline-flex;align-items:center;gap:12px;">
      <svg width="32" height="32" viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="88" height="88" rx="22" fill="rgba(255,255,255,0.18)"/><path d="M26 24 L54 44 L26 64" stroke="white" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><line x1="54" y1="44" x2="70" y2="44" stroke="white" stroke-width="8" stroke-linecap="round"/></svg>
      <span style="font-family:Georgia,serif;font-weight:700;font-size:1.4rem;color:#FFFFFF;letter-spacing:-0.02em;">inroad</span>
    </div>
  </div>

  <!-- Body -->
  <div style="padding:44px 40px 36px;">

    <h1 style="font-size:26px;font-weight:900;color:#111110;letter-spacing:-0.02em;line-height:1.2;margin:0 0 14px;">Welcome back.</h1>

    <p style="font-size:15px;color:#6E6860;line-height:1.7;margin:0 0 36px;">
      Here's your sign-in link. It'll take you straight to your dashboard.
    </p>

    <div style="margin-bottom:40px;">
      <a href="{verify_url}" style="display:inline-block;background:#1F4530;color:#FFFFFF;font-size:15px;font-weight:700;padding:15px 32px;border-radius:10px;text-decoration:none;letter-spacing:0.01em;">Sign in to inroad &rarr;</a>
    </div>

    <div style="background:#F5F5F2;border:1px solid #E2DED8;border-radius:8px;padding:14px 16px;">
      <div style="font-size:10px;font-weight:700;color:#A8A09A;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:7px;">Button not working? Copy this link</div>
      <div style="font-size:11px;color:#6E6860;word-break:break-all;font-family:'Courier New',monospace;line-height:1.5;">{verify_url}</div>
    </div>

  </div>

  <!-- Footer -->
  <div style="padding:24px 40px;border-top:1px solid #E2DED8;text-align:center;">
    <div style="font-size:12px;color:#A8A09A;line-height:1.7;">
      This link expires in {MAGIC_LINK_EXPIRY_MINUTES} minutes and can only be used once.<br>
      If you didn&rsquo;t request this, you can safely ignore it.
    </div>
  </div>

</div>
</div>
</body>
</html>"""

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
                "subject": "Your inroad sign-in link",
                "html": html,
            },
            timeout=10,
        )
        data = resp.json()
        log_email(email, "Login link", "login_link", data.get("id"))
        return True
    except Exception as e:
        print(f"[email] Failed to send login link: {e}")
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

    next_url = (data.get("next") or "").strip() or None
    create_magic_token(email, token, expires_at_str)

    # Determine whether this is a new sign-up or a returning user login
    existing_student = get_student_by_email(email)
    is_new_user = existing_student is None

    if DEV_MODE:
        from urllib.parse import urlencode
        qs_dev = {"token": token}
        if next_url:
            qs_dev["next"] = next_url
        print(f"\n[DEV] Magic link for {email} (new={is_new_user}):")
        print(f"  {APP_BASE_URL}/verify?{urlencode(qs_dev)}\n")
        return jsonify({"status": "sent", "dev_token": token})

    if is_new_user:
        ok = send_magic_link(email, token, next_url=next_url)
    else:
        ok = send_login_link(email, token, next_url=next_url)
    if not ok:
        return jsonify({"error": "Failed to send email. Please try again."}), 500

    return jsonify({"status": "sent"})


@app.route("/auth/verify")
def verify():
    token = request.args.get("token", "").strip()
    if not token:
        return jsonify({"error": "Invalid link. Please request a new one."}), 400

    row = get_and_consume_token(token)
    if not row:
        return jsonify({"error": "This link has expired or has already been used. Please request a new one."}), 400

    email = row["email"]

    # Ensure student exists — track whether this is a brand-new account
    student = get_student_by_email(email)
    is_new_user = student is None
    if not student:
        student = create_student(email)

    student_id = student["id"]

    # Auto-detect university from email domain and store it (once, for new users)
    if is_new_user or not student.get("university"):
        uni_info = detect_university(email)
        if uni_info:
            update_student_fields(student_id, {"university": uni_info["name"]})

    # New users with no profile → onboarding. Everyone else → dashboard.
    # (Existing users in the DB always go to dashboard even if name isn't set yet.)
    has_profile = bool(student.get("name"))
    next_param = request.args.get("next", "").strip()
    if next_param and not is_new_user:
        destination = next_param
    elif is_new_user and not has_profile:
        destination = "/onboarding"
    else:
        destination = "/dashboard"

    # Issue JWT access token
    access_token = make_access_token(student_id)

    # Issue opaque refresh token and persist it
    refresh_token_str = make_refresh_token_str()
    refresh_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_TTL_DAYS)
    ).isoformat()
    create_refresh_token(student_id, refresh_token_str, refresh_expires_at)

    # Kick off card generation in the background so cards are ready when the
    # dashboard loads. Runs in a daemon thread — won't block the response.
    import threading
    def _generate_cards(sid):
        try:
            from pipeline.daily_cards import generate_daily_cards
            generate_daily_cards(sid)
        except Exception as exc:
            print(f"[cards] background generation failed for student {sid}: {exc}")
    threading.Thread(target=_generate_cards, args=(student_id,), daemon=True).start()

    resp = make_response(jsonify({"access_token": access_token, "redirect": destination}))
    resp.set_cookie(
        "ccc_refresh",
        refresh_token_str,
        max_age=JWT_REFRESH_TTL_DAYS * 86400,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="None" if IS_PRODUCTION else "Lax",
        path="/",
    )
    return resp


@app.route("/auth/refresh", methods=["POST"])
def refresh():
    token_str = request.cookies.get("ccc_refresh", "")
    if not token_str:
        return jsonify({"error": "missing refresh token"}), 401

    token_row = get_refresh_token(token_str)
    if not token_row:
        return jsonify({"error": "invalid refresh token"}), 401
    if token_row.get("revoked_at"):
        return jsonify({"error": "refresh token revoked"}), 401

    # Check expiry — expires_at is stored as an ISO string
    expires_at_str = token_row["expires_at"]
    try:
        # Handle both offset-aware and offset-naive ISO strings from the DB
        if expires_at_str.endswith("+00:00") or expires_at_str.endswith("Z"):
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        else:
            expires_at = datetime.fromisoformat(expires_at_str).replace(tzinfo=timezone.utc)
    except Exception:
        return jsonify({"error": "invalid refresh token"}), 401

    if datetime.now(timezone.utc) > expires_at:
        return jsonify({"error": "refresh token expired"}), 401

    student_id = token_row["student_id"]

    # Token rotation: revoke old, issue new
    revoke_refresh_token(token_str)
    new_refresh_str = make_refresh_token_str()
    new_refresh_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_TTL_DAYS)
    ).isoformat()
    create_refresh_token(student_id, new_refresh_str, new_refresh_expires_at)

    access_token = make_access_token(student_id)

    resp = make_response(jsonify({"access_token": access_token}))
    resp.set_cookie(
        "ccc_refresh",
        new_refresh_str,
        max_age=JWT_REFRESH_TTL_DAYS * 86400,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="None" if IS_PRODUCTION else "Lax",
        path="/",
    )
    return resp


@app.route("/auth/logout", methods=["POST"])
def logout():
    token_str = request.cookies.get("ccc_refresh", "")
    if token_str:
        revoke_refresh_token(token_str)
    resp = make_response(jsonify({"status": "ok"}))
    resp.delete_cookie("ccc_refresh", path="/")
    return resp


# ── Current user ─────────────────────────────────────────────────────────────

@app.route("/api/me")
@require_jwt
def me():
    student = get_student_by_id(g.student_id)
    if not student:
        return jsonify({"error": "Student not found"}), 404
    return jsonify(student)


@app.route("/api/me", methods=["PATCH"])
@require_jwt
def update_me():
    data = request.get_json(silent=True) or {}

    # Map request keys to column names; only include keys present in body
    field_map = {
        "name":        "name",
        "age":         "age",
        "status":      "status",
        "industries":  "industries",
        "companySize": "company_size",
        "bio":         "bio",
        "university":  "university",
    }
    fields = {}
    for req_key, col in field_map.items():
        if req_key in data:
            fields[col] = data[req_key]

    if fields:
        update_student_fields(g.student_id, fields)

    student = get_student_by_id(g.student_id)
    if not student:
        return jsonify({"error": "Student not found"}), 401
    return jsonify(student)


@app.route("/api/me", methods=["DELETE"])
@require_jwt
def delete_me():
    revoke_all_tokens_for_student(g.student_id)
    deactivate_student(g.student_id)
    resp = make_response(jsonify({"status": "deactivated"}))
    resp.delete_cookie("ccc_refresh", path="/")
    return resp


# ── Student profile ───────────────────────────────────────────────────────────

@app.route("/api/students/register", methods=["POST"])
@require_jwt
def register_student():
    data = request.get_json(silent=True) or {}
    student = get_student_by_id(g.student_id)
    if not student:
        return jsonify({"error": "Student not found"}), 404
    email = student["email"]

    student = upsert_student_profile(
        email=email,
        name=data.get("name", ""),
        age=data.get("age"),
        status=data.get("status", ""),
        industries=json.dumps(data.get("industries", [])),
        company_size=data.get("companySize", ""),
        bio=data.get("bio", ""),
        university=data.get("university", student.get("university", "")),
    )
    return jsonify(student)


# ── Email draft regeneration ──────────────────────────────────────────────────

@app.route("/api/draft/regenerate", methods=["POST"])
@require_jwt
def regenerate_draft():
    data = request.get_json(silent=True) or {}
    student = get_student_by_id(g.student_id)
    if not student:
        return jsonify({"error": "Student not found"}), 404

    lead = {
        "name":           data.get("person_name", ""),
        "title":          data.get("person_title", ""),
        "company":        data.get("company", ""),
        "is_alumni":      bool(data.get("is_alumni", False)),
        "university":     "",
        "tenure_months":  0,
    }
    job = {
        "title":        data.get("job_title", ""),
        "company_name": data.get("company", ""),
        "url":          data.get("job_url", ""),
        "industries":   student.get("industries", []),
    }

    from pipeline.daily_cards import generate_email_draft
    subject, body = generate_email_draft(student, lead, job)
    return jsonify({"subject": subject, "body": body})


# ── Matches ───────────────────────────────────────────────────────────────────

@app.route("/api/matches/today/<int:student_id>")
@require_jwt
def matches_today(student_id):
    if g.student_id != student_id:
        return jsonify({"error": "forbidden"}), 403

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    queued = get_queued_cards(student_id, today_str)

    _matches_select = """
            SELECT m.id, m.student_id, m.job_id, m.match_date,
                   m.person_name, m.person_title, m.person_company,
                   m.person_linkedin_url, m.person_university,
                   m.person_tenure_months, m.is_alumni, m.relevance_score,
                   m.score_breakdown,
                   m.expected_email, m.email_confidence,
                   m.email_subject, m.email_body,
                   m.status, m.sent_at, m.replied_at, m.created_at,
                   j.title as job_title, j.company, j.url as job_url,
                   j.location, j.industry, j.posted_at
            FROM matches m
            JOIN jobs j ON j.id = m.job_id
    """

    if queued:
        # Build match rows from queued card job_ids (preserving queue order)
        job_ids = [c["job_id"] for c in queued]
        placeholders = ", ".join(["?"] * len(job_ids))
        rows = fetchall(
            _matches_select + f"""
            WHERE m.student_id = ?
              AND m.job_id IN ({placeholders})
            ORDER BY m.is_alumni DESC, m.created_at ASC
            LIMIT 3
        """, [student_id] + job_ids)
        # Mark each returned queued card as consumed
        returned_job_ids = {r["job_id"] for r in rows}
        for card in queued:
            if card["job_id"] in returned_job_ids:
                mark_card_consumed(card["id"])
    else:
        # Fallback: fetch today's matches by creation date
        rows = fetchall(
            _matches_select + """
            WHERE m.student_id = ?
              AND DATE(m.created_at) = DATE('now')
            ORDER BY m.is_alumni DESC, m.created_at ASC
            LIMIT 3
        """, (student_id,))

        # No cards yet — generate on-demand now (covers first dashboard load)
        if not rows:
            try:
                from pipeline.daily_cards import generate_daily_cards
                generate_daily_cards(student_id)
                rows = fetchall(
                    _matches_select + """
                    WHERE m.student_id = ?
                      AND DATE(m.created_at) = DATE('now')
                    ORDER BY m.is_alumni DESC, m.created_at ASC
                    LIMIT 3
                """, (student_id,))
            except Exception as exc:
                print(f"[cards] on-demand generation failed for student {student_id}: {exc}")

    for m in rows:
        raw = m.get("score_breakdown")
        if isinstance(raw, str):
            try:
                m["score_breakdown"] = json.loads(raw)
            except (ValueError, TypeError):
                m["score_breakdown"] = {}
        elif raw is None:
            m["score_breakdown"] = {}

    return jsonify(rows)


# ── Signals ───────────────────────────────────────────────────────────────────

@app.route("/api/signals", methods=["POST"])
@require_jwt
def record_signal():
    data = request.get_json(silent=True) or {}
    from db.database import execute as db_execute
    db_execute(
        "INSERT INTO signals (match_id, student_id, signal) VALUES (?, ?, ?)",
        (data.get("match_id"), g.student_id, data.get("signal", ""))
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
def list_jobs():
    limit = min(int(request.args.get("limit", 50)), 1000)
    rows = fetchall("SELECT * FROM jobs ORDER BY posted_at DESC LIMIT ?", (limit,))
    jobs = [{
        "company_name": r.get("company", ""),
        "title":        r.get("title", ""),
        "industries":   [r["industry"]] if r.get("industry") else [],
        "region":       r.get("location", ""),
        "seniority":    r.get("company_size", ""),
        "posted_date":  (r.get("posted_at") or "")[:10],
        "source_name":  r.get("source", ""),
        "url":          r.get("url", ""),
    } for r in rows]
    return jsonify({"data": {"jobs": jobs}})


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/api/admin/stats")
def admin_stats():
    active_jobs  = (fetchone("SELECT COUNT(*) as n FROM jobs") or {}).get("n", 0)
    companies    = (fetchone("SELECT COUNT(DISTINCT company) as n FROM jobs") or {}).get("n", 0)
    students     = (fetchone("SELECT COUNT(*) as n FROM students") or {}).get("n", 0)
    matches_today = (fetchone(
        "SELECT COUNT(*) as n FROM matches WHERE match_date = date('now')"
    ) or {}).get("n", 0)
    emails_sent_week = (fetchone(
        "SELECT COUNT(*) as n FROM matches WHERE status='sent' AND sent_at > datetime('now', '-7 days')"
    ) or {}).get("n", 0)

    by_source_rows = fetchall("SELECT source, COUNT(*) as n FROM jobs WHERE source IS NOT NULL GROUP BY source ORDER BY n DESC")
    by_source = {r["source"]: r["n"] for r in by_source_rows}

    by_industry_rows = fetchall("SELECT industry, COUNT(*) as n FROM jobs WHERE industry IS NOT NULL GROUP BY industry ORDER BY n DESC LIMIT 10")
    by_industry = {r["industry"]: r["n"] for r in by_industry_rows}

    return jsonify({"data": {
        "active_jobs":       active_jobs,
        "total_jobs":        active_jobs,
        "companies":         companies,
        "students":          students,
        "matches_today":     matches_today,
        "emails_sent_week":  emails_sent_week,
        "by_source":         by_source,
        "by_industry":       by_industry,
    }})


@app.route("/api/admin/runs")
def admin_runs():
    # No scrape_runs table yet — return empty list
    return jsonify({"data": {"runs": []}})


@app.route("/api/admin/students")
def admin_students():
    rows = fetchall("""
        SELECT s.email, s.university, s.industries, s.created_at,
               COUNT(CASE WHEN m.status='sent' THEN 1 END) as sent,
               COUNT(CASE WHEN m.replied_at IS NOT NULL THEN 1 END) as replies
        FROM students s
        LEFT JOIN matches m ON m.student_id = s.id
        WHERE s.deactivated_at IS NULL
        GROUP BY s.id
        ORDER BY s.created_at DESC
    """)
    return jsonify({"data": {"students": rows}})


@app.route("/api/admin/suppressions")
def admin_suppressions():
    rows = fetchall("SELECT identifier, identifier_type, created_at as added_at FROM suppression_list ORDER BY created_at DESC")
    return jsonify({"data": {"suppressions": rows}})


@app.route("/api/suppress", methods=["POST"])
def add_suppression():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip()
    id_type    = (data.get("type") or "linkedin").strip()
    if not identifier:
        return jsonify({"error": "identifier required"}), 400
    execute(
        "INSERT OR IGNORE INTO suppression_list (identifier, identifier_type) VALUES (?, ?)",
        (identifier, id_type),
    )
    return jsonify({"status": "ok"})


# ── HTML page routes ──────────────────────────────────────────────────────────

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_DIR = os.path.join(ROOT, "html")


def _send_html(filename):
    return send_from_directory(HTML_DIR, filename)


@app.route("/")
@app.route("/landing")
def landing():
    return _send_html("ccc-landing-final.html")


@app.route("/login")
def login():
    return _send_html("login.html")


@app.route("/signup")
def signup():
    return _send_html("ccc-signup.html")


@app.route("/onboarding")
def onboarding():
    return _send_html("ccc-onboarding.html")


@app.route("/dashboard")
def dashboard():
    return _send_html("ccc-dashboard-live.html")


@app.route("/settings")
def settings_page():
    return _send_html("ccc-settings.html")


@app.route("/admin")
def admin_page():
    return _send_html("ccc-admin.html")


@app.route("/verify")
def verify_page():
    """Client-side page that exchanges a magic-link token for a JWT."""
    return send_from_directory(os.path.join(ROOT, "static"), "verify.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve files from the project-level static/ directory."""
    return send_from_directory(os.path.join(ROOT, "static"), filename)


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5001))
    debug = DEV_MODE
    print(f"[CCC] Starting on port {port} | production={IS_PRODUCTION} | debug={debug}")
    app.run(host="0.0.0.0", port=port, debug=debug)
