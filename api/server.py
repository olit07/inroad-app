"""
api/server.py
Flask application entry point.
Run via: gunicorn --bind 0.0.0.0:$PORT api.server:app  (Railway)
      or: python api/server.py                           (local dev)
"""

import os
import sys
import logging
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
    JWT_REFRESH_TTL_DAYS, ADMIN_SECRET, SEARCH_ROLES_ENABLED,
    AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, ADMIN_UI_ENABLED,
)
from db.database import (
    init_db, get_student_by_email, get_student_by_id,
    create_student, upsert_student_profile, update_student_fields,
    deactivate_student, revoke_all_tokens_for_student,
    create_magic_token, get_and_consume_token,
    log_email, count_recent_tokens, fetchall, fetchone,
    execute as db_execute,
    create_refresh_token, get_refresh_token, revoke_refresh_token,
    get_queued_cards, mark_card_consumed,
    get_leads_for_company
)
from api.auth import make_access_token, make_refresh_token_str, require_jwt
from utils.university_lookup import detect_university

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), ".."))

CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

# Configure logging so scraper output appears in Railway logs
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

# Initialise DB tables on startup — must be at module level so Gunicorn picks it up
init_db()

# Determine if we're on a secure host (Railway / any https origin)
IS_PRODUCTION = any("https://" in o for o in ALLOWED_ORIGINS) or not DEV_MODE


# ── Embedded background scheduler ────────────────────────────────────────────

def _start_background_scheduler():
    """
    Start all scheduled jobs in a background thread.
    Uses a non-blocking file lock so only one gunicorn worker runs the loop.

    Schedule:
      - Trackr scrape + leads check: 05:45, 10:45, 16:45 UTC daily
      - Cards + notify: 06:00 UTC daily
    """
    import threading, time, fcntl, logging as _log
    from datetime import datetime, timedelta

    try:
        _lock_fh = open("/tmp/inroad_scheduler.lock", "w")
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        return  # another worker already holds the lock

    TRACKR_SLOTS = [(5,0),(7,10),(9,15),(11,20),(13,25),(15,30),(17,45),(20,0)]
    PIPELINE_HOUR = 6
    WTTJ_HOUR = 5

    def _next_slot(slots):
        now = datetime.utcnow()
        candidates = []
        for h, m in slots:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            candidates.append(t)
        return min(candidates)

    def _next_hour(hour):
        now = datetime.utcnow()
        t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t

    def _loop():
        from scheduler.run import run_trackr_pipeline, run_cards_job, run_notify_job, run_wttj_job
        log = _log.getLogger("scheduler")
        log.info("Background scheduler started — Trackr slots, WTTJ at 05:05 UTC, pipeline at 06:00 UTC")

        next_trackr   = _next_slot(TRACKR_SLOTS)
        next_pipeline = _next_hour(PIPELINE_HOUR)
        next_wttj = datetime.utcnow().replace(hour=WTTJ_HOUR, minute=5, second=0, microsecond=0)
        if next_wttj <= datetime.utcnow():
            next_wttj += timedelta(days=1)
        log.info(f"First Trackr run: {next_trackr.strftime('%Y-%m-%d %H:%M')} UTC")
        log.info(f"First WTTJ run: {next_wttj.strftime('%Y-%m-%d %H:%M')} UTC")
        log.info(f"First pipeline run: {next_pipeline.strftime('%Y-%m-%d %H:%M')} UTC")

        while True:
            now = datetime.utcnow()
            wait = min(
                max((next_trackr - now).total_seconds(), 0),
                max((next_pipeline - now).total_seconds(), 0),
                max((next_wttj - now).total_seconds(), 0),
            )
            time.sleep(max(wait, 30))

            now = datetime.utcnow()

            if now >= next_wttj:
                try:
                    run_wttj_job()
                except Exception as exc:
                    log.error(f"WTTJ job crashed: {exc}", exc_info=True)
                next_wttj = now.replace(hour=WTTJ_HOUR, minute=5, second=0, microsecond=0) + timedelta(days=1)
                log.info(f"Next WTTJ run: {next_wttj.strftime('%Y-%m-%d %H:%M')} UTC")

            if now >= next_trackr:
                try:
                    run_trackr_pipeline()
                except Exception as exc:
                    log.error(f"Trackr pipeline crashed: {exc}", exc_info=True)
                next_trackr = _next_slot(TRACKR_SLOTS)
                log.info(f"Next Trackr run: {next_trackr.strftime('%Y-%m-%d %H:%M')} UTC")

            if now >= next_pipeline:
                try:
                    run_cards_job()
                except Exception as exc:
                    log.error(f"Cards job crashed: {exc}", exc_info=True)
                try:
                    run_notify_job()
                except Exception as exc:
                    log.error(f"Notify job crashed: {exc}", exc_info=True)
                next_pipeline = _next_hour(PIPELINE_HOUR)
                log.info(f"Next pipeline run: {next_pipeline.strftime('%Y-%m-%d %H:%M')} UTC")

    threading.Thread(target=_loop, daemon=True, name="scheduler").start()


_start_background_scheduler()

# ── Admin auth ───────────────────────────────────────────────────────────────

def require_admin(f):
    """Guard admin routes with a secret key passed as ?key= or X-Admin-Key header."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not ADMIN_SECRET:
            # No secret configured — block access entirely in production
            if IS_PRODUCTION:
                return jsonify({"error": "Admin access not configured"}), 403
            # In dev mode without a secret, allow through
            return f(*args, **kwargs)
        provided = (
            request.args.get("key")
            or request.headers.get("X-Admin-Key")
            or (request.get_json(silent=True) or {}).get("admin_key")
        )
        if not provided or not secrets.compare_digest(provided, ADMIN_SECRET):
            return jsonify({"error": "Unauthorised"}), 401
        return f(*args, **kwargs)
    return wrapped


# ── Session helpers ──────────────────────────────────────────────────────────

COOKIE_NAME = "inroad_session"


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

def send_magic_link(email: str, token: str, next_url: str = None, ref: str = None):
    """Send a magic-link email via Resend REST API."""
    import requests as req
    from urllib.parse import urlencode

    qs = {"token": token}
    if next_url:
        qs["next"] = next_url
    if ref:
        qs["ref"] = ref
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

    <h1 style="font-size:26px;font-weight:900;color:#111110;letter-spacing:-0.02em;line-height:1.15;margin:0 0 14px;">Your sign-up link<br>is <em style="font-style:italic;font-weight:300;color:#1F4530;">ready.</em></h1>

    <p style="font-size:15px;color:#6E6860;line-height:1.7;margin:0 0 36px;font-weight:400;">
      Click below to verify your email and start getting matched to real people
      at companies you want to work at, alumni first.
    </p>

    <div style="margin-bottom:40px;">
      <a href="{verify_url}" style="display:inline-block;background:#1F4530;color:#FFFFFF;font-size:15px;font-weight:700;padding:15px 32px;border-radius:10px;text-decoration:none;letter-spacing:0.01em;">Sign up to inroad &rarr;</a>
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


# ── Anonymous visitor tracking ────────────────────────────────────────────────

_VISITOR_COOKIE = "inroad_visitor"
_SKIP_PREFIXES  = ("/api/", "/static/", "/auth/")

_BOT_UA_FRAGMENTS = (
    "bot", "crawl", "spider", "slurp", "curl/", "python", "go-http",
    "java/", "okhttp", "axios", "wget", "scrapy", "headless", "phantom",
    "selenium", "puppeteer", "playwright", "node-fetch", "got/", "httpx",
)

@app.before_request
def _record_page_visit():
    if request.method in ("OPTIONS", "HEAD"):
        return
    if any(request.path.startswith(p) for p in _SKIP_PREFIXES):
        return
    ua = (request.headers.get("User-Agent") or "").lower()
    if not ua or any(f in ua for f in _BOT_UA_FRAGMENTS):
        return
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    app.logger.info("VISIT %s %s | ip=%s ua=%s", request.method, request.path, ip, ua[:120])
    visitor_id = request.cookies.get(_VISITOR_COOKIE)
    if not visitor_id:
        import uuid as _uuid
        visitor_id = str(_uuid.uuid4())
    g._visitor_id     = visitor_id
    g._visitor_is_new = _VISITOR_COOKIE not in request.cookies
    try:
        from db.database import execute as _ex, USE_POSTGRES
        if USE_POSTGRES:
            _ex(
                "INSERT INTO site_visits (visitor_id, visited_date) "
                "VALUES (?, CURRENT_DATE) ON CONFLICT (visitor_id, visited_date) DO NOTHING",
                (visitor_id,),
            )
        else:
            _ex(
                "INSERT OR IGNORE INTO site_visits (visitor_id, visited_date) "
                "VALUES (?, date('now'))",
                (visitor_id,),
            )
    except Exception:
        pass


@app.after_request
def _set_visitor_cookie(response):
    visitor_id = getattr(g, "_visitor_id", None)
    if visitor_id and getattr(g, "_visitor_is_new", False):
        response.set_cookie(
            _VISITOR_COOKIE,
            visitor_id,
            max_age=365 * 24 * 3600,
            httponly=True,
            secure=IS_PRODUCTION,
            samesite="Lax",
            path="/",
        )
    return response


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()})


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/api/check-email", methods=["POST"])
def check_email():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400
    exists = get_student_by_email(email) is not None
    return jsonify({"exists": exists})


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

    # Login page always forces opportunities destination
    if data.get("source") == "login":
        next_url = "/opportunities"
    else:
        next_url = (data.get("next") or "").strip() or None
    # Referral code passed from signup page (?ref=CODE or body.ref)
    ref_code = (request.args.get("ref") or data.get("ref") or "").strip().upper() or None
    create_magic_token(email, token, expires_at_str)

    # Determine whether this is a new sign-up or a returning user login
    existing_student = get_student_by_email(email)
    is_new_user = existing_student is None

    # Login page must not create accounts — reject unknown emails immediately
    if data.get("source") == "login" and is_new_user:
        return jsonify({"error": "no_account", "message": "No account found with this email."}), 404

    if DEV_MODE:
        from urllib.parse import urlencode
        qs_dev = {"token": token}
        if next_url:
            qs_dev["next"] = next_url
        if ref_code:
            qs_dev["ref"] = ref_code
        print(f"\n[DEV] Magic link for {email} (new={is_new_user}):")
        print(f"  {APP_BASE_URL}/verify?{urlencode(qs_dev)}\n")
        return jsonify({"status": "sent", "dev_token": token})

    if is_new_user and data.get("source") != "login":
        ok = send_magic_link(email, token, next_url=next_url, ref=ref_code)
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

    # Auto-detect university from email domain; set university name and region
    if is_new_user or not student.get("university"):
        uni_info = detect_university(email)
        if uni_info:
            region = "US" if uni_info.get("country", "").upper() == "US" else "UK"
            update_student_fields(student_id, {"university": uni_info["name"], "region": region})
        else:
            # No university email — default to UK so leads/jobs are UK-based
            update_student_fields(student_id, {"region": "UK"})

    # Credit referrer if this is a new signup with a valid referral code
    if is_new_user:
        ref_code = request.args.get("ref", "").strip().upper()
        if ref_code:
            from db.database import get_student_by_referral_code, execute as db_execute
            referrer = get_student_by_referral_code(ref_code)
            if referrer:
                db_execute(
                    "UPDATE students SET referred_by = ? WHERE id = ?",
                    (ref_code, student_id)
                )
                db_execute(
                    "UPDATE students SET daily_cards_override = 5 WHERE referral_code = ?",
                    (ref_code,)
                )

    # next_param always wins (login page sets next=/opportunities).
    # New users with no next_param → onboarding. Everyone else → opportunities.
    has_profile = bool(student.get("name"))
    next_param = request.args.get("next", "").strip()
    if next_param:
        destination = next_param
    elif is_new_user and not has_profile:
        destination = "/onboarding"
    else:
        destination = "/opportunities"

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
        "inroad_refresh",
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
    token_str = request.cookies.get("inroad_refresh", "")
    if not token_str:
        return jsonify({"error": "missing refresh token"}), 401

    token_row = get_refresh_token(token_str)
    if not token_row:
        return jsonify({"error": "invalid refresh token"}), 401
    if token_row.get("revoked_at"):
        return jsonify({"error": "refresh token revoked"}), 401

    # Check expiry — Postgres returns a datetime object, SQLite returns an ISO string
    raw_expiry = token_row["expires_at"]
    try:
        if isinstance(raw_expiry, datetime):
            expires_at = raw_expiry if raw_expiry.tzinfo else raw_expiry.replace(tzinfo=timezone.utc)
        else:
            expires_at_str = str(raw_expiry)
            if expires_at_str.endswith("+00:00") or expires_at_str.endswith("Z"):
                expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            else:
                expires_at = datetime.fromisoformat(expires_at_str).replace(tzinfo=timezone.utc)
    except Exception:
        return jsonify({"error": "invalid refresh token"}), 401

    if datetime.now(timezone.utc) > expires_at:
        return jsonify({"error": "refresh token expired"}), 401

    student_id = token_row["student_id"]

    # Stamp last_seen on every token refresh (catches users whose sessions stay alive)
    try:
        from datetime import datetime as _dt, timezone as _tz
        update_student_fields(student_id, {"last_seen": _dt.now(_tz.utc).isoformat()})
    except Exception:
        pass

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
        "inroad_refresh",
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
    token_str = request.cookies.get("inroad_refresh", "")
    if token_str:
        revoke_refresh_token(token_str)
    resp = make_response(jsonify({"status": "ok"}))
    resp.delete_cookie("inroad_refresh", path="/")
    return resp


# ── Current user ─────────────────────────────────────────────────────────────

def _sanitise_student(student: dict) -> dict:
    """Strip sensitive OAuth token fields; expose a safe boolean instead."""
    s = dict(student)
    connected = bool(s.get("outlook_access_token"))
    s.pop("outlook_access_token", None)
    s.pop("outlook_refresh_token", None)
    s.pop("outlook_token_expiry", None)
    s["outlook_connected"]    = connected
    s["search_roles_enabled"] = SEARCH_ROLES_ENABLED
    # Normalise company_size to a list (handles legacy plain-string values)
    cs = s.get("company_size") or "[]"
    try:
        parsed = json.loads(cs)
        s["company_size"] = parsed if isinstance(parsed, list) else ([parsed] if parsed else [])
    except (json.JSONDecodeError, TypeError):
        s["company_size"] = [cs] if cs else []
    return s


@app.route("/api/me")
@require_jwt
def me():
    student = get_student_by_id(g.student_id)
    if not student:
        return jsonify({"error": "Student not found"}), 404
    from db.database import USE_POSTGRES
    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        update_student_fields(g.student_id, {"last_seen": now_str})
    except Exception as exc:
        app.logger.warning("last_seen update failed for student %s: %s", g.student_id, exc)
    return jsonify(_sanitise_student(student))


def _regenerate_todays_drafts(student_id: int) -> int:
    """
    Regenerate email subject + body for today's unsent matches for a student.
    Called after the student updates bio, name, or university in settings.
    Returns the number of drafts updated.
    """
    from pipeline.daily_cards import generate_email_draft
    from datetime import date
    today = date.today().isoformat()

    rows = fetchall("""
        SELECT m.id,
               m.person_name, m.person_company,
               s.name         AS student_name,
               s.university   AS student_university,
               s.bio          AS student_bio,
               j.title        AS job_title,
               j.industry
        FROM matches m
        JOIN students s ON s.id = m.student_id
        JOIN jobs     j ON j.id = m.job_id
        WHERE m.student_id = %s
          AND m.match_date  = %s
          AND m.status     != 'sent'
    """, (student_id, today))

    updated = 0
    for r in rows:
        student_ctx = {
            "name":       r.get("student_name") or "",
            "university": r.get("student_university") or "",
            "bio":        r.get("student_bio") or "",
        }
        lead_ctx = {"name": r.get("person_name") or "", "company": r.get("person_company") or ""}
        job_ctx  = {"title": r.get("job_title") or "", "industry": r.get("industry") or ""}
        subject, body, _ = generate_email_draft(student_ctx, lead_ctx, job_ctx)
        db_execute(
            "UPDATE matches SET email_subject = %s, email_body = %s WHERE id = %s",
            (subject, body, r["id"])
        )
        updated += 1
    return updated


@app.route("/api/me", methods=["PATCH"])
@require_jwt
def update_me():
    data = request.get_json(silent=True) or {}

    # Map request keys to column names; only include keys present in body
    field_map = {
        "name":            "name",
        "age":             "age",
        "status":          "status",
        "industries":      "industries",
        "companySize":     "company_size",
        "bio":             "bio",
        "university":      "university",
        "notifyMatches":   "notify_matches",
        "notifyFrequency": "notify_frequency",
    }
    fields = {}
    for req_key, col in field_map.items():
        if req_key in data:
            fields[col] = data[req_key]

    if fields:
        update_student_fields(g.student_id, fields)

    # Regenerate today's draft emails if any email-content fields changed
    if any(k in data for k in ("bio", "name", "university")):
        try:
            n = _regenerate_todays_drafts(g.student_id)
            app.logger.info(f"Regenerated {n} draft(s) for student {g.student_id} after settings save")
        except Exception as e:
            app.logger.warning(f"Draft regen failed for student {g.student_id}: {e}")

    student = get_student_by_id(g.student_id)
    if not student:
        return jsonify({"error": "Student not found"}), 401
    return jsonify(_sanitise_student(student))


@app.route("/api/me", methods=["DELETE"])
@require_jwt
def delete_me():
    revoke_all_tokens_for_student(g.student_id)
    deactivate_student(g.student_id)
    resp = make_response(jsonify({"status": "deactivated"}))
    resp.delete_cookie("inroad_refresh", path="/")
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

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    student = upsert_student_profile(
        email=email,
        name=name,
        age=data.get("age"),
        status=data.get("status", ""),
        industries=json.dumps(data.get("industries", [])),
        company_size=json.dumps(data.get("companySize", [])) if isinstance(data.get("companySize"), list) else json.dumps([data["companySize"]] if data.get("companySize") else []),
        bio=data.get("bio", ""),
        university=data.get("university", student.get("university", "")),
    )
    # Persist notification preferences set during onboarding
    notify_fields = {}
    if "notifyMatches" in data:
        notify_fields["notify_matches"] = bool(data["notifyMatches"])
    if "notifyFrequency" in data:
        notify_fields["notify_frequency"] = data["notifyFrequency"]
    if notify_fields:
        update_student_fields(g.student_id, notify_fields)
        student = get_student_by_id(g.student_id)

    # Generate today's cards immediately so the dashboard isn't empty on first login
    _sid = g.student_id
    def _gen_cards():
        try:
            from pipeline.daily_cards import generate_daily_cards
            generate_daily_cards(_sid)
        except Exception as exc:
            logging.getLogger("cards").warning(f"Card gen on signup failed for student {_sid}: {exc}")
    import threading
    threading.Thread(target=_gen_cards, daemon=True).start()

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
    subject, body, is_personalised = generate_email_draft(student, lead, job)

    # Persist the regenerated draft so it survives page refresh
    match_id = data.get("match_id")
    if match_id:
        from db.database import execute as _exec, USE_POSTGRES
        ph = "%s" if USE_POSTGRES else "?"
        _exec(
            f"UPDATE matches SET email_subject = {ph}, email_body = {ph} WHERE id = {ph} AND student_id = {ph}",
            (subject, body, match_id, g.student_id),
        )

    return jsonify({"subject": subject, "body": body, "personalised": is_personalised})


# ── Matches ───────────────────────────────────────────────────────────────────

@app.route("/api/matches/today/<int:student_id>")
@require_jwt
def matches_today(student_id):
    if g.student_id != student_id:
        return jsonify({"error": "forbidden"}), 403

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    queued = get_queued_cards(student_id, today_str)

    # Respect per-student card quota (referral bonus may raise it above default 3)
    from config.settings import DAILY_MATCH_QUOTA
    student_row = fetchone("SELECT daily_cards_override FROM students WHERE id = ?", (student_id,))
    card_limit = int((student_row or {}).get("daily_cards_override") or DAILY_MATCH_QUOTA)

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
                   j.location, j.industry, j.opening_date
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
            LIMIT {card_limit}
        """, [student_id] + job_ids)
        # Mark each returned queued card as consumed
        returned_job_ids = {r["job_id"] for r in rows}
        for card in queued:
            if card["job_id"] in returned_job_ids:
                mark_card_consumed(card["id"])
    else:
        # Fallback: fetch today's matches by creation date
        rows = fetchall(
            _matches_select + f"""
            WHERE m.student_id = ?
              AND DATE(m.created_at) = CURRENT_DATE
            ORDER BY m.is_alumni DESC, m.created_at ASC
            LIMIT {card_limit}
        """, (student_id,))

        # No cards yet — generate on-demand now (covers first dashboard load)
        if not rows:
            try:
                from pipeline.daily_cards import generate_daily_cards
                generate_daily_cards(student_id)
                rows = fetchall(
                    _matches_select + f"""
                    WHERE m.student_id = ?
                      AND DATE(m.created_at) = CURRENT_DATE
                    ORDER BY m.is_alumni DESC, m.created_at ASC
                    LIMIT {card_limit}
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


# ── Outlook OAuth ─────────────────────────────────────────────────────────────

_MS_AUTH_URL   = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MS_TOKEN_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_MS_GRAPH_SEND = "https://graph.microsoft.com/v1.0/me/sendMail"
_MS_SCOPES     = "Mail.Send Mail.Read offline_access openid"


def _make_outlook_state(student_id: int) -> str:
    """Sign student_id so the OAuth callback can verify who initiated the flow."""
    payload = str(student_id)
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(SESSION_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_outlook_state(state: str):
    """Return student_id (int) if state is valid, else None."""
    try:
        b64, sig = state.rsplit(".", 1)
        if not hmac.compare_digest(
            hmac.new(SESSION_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest(),
            sig,
        ):
            return None
        return int(base64.urlsafe_b64decode(b64.encode()).decode())
    except Exception:
        return None


def _refresh_outlook_token(student_id: int, refresh_token: str):
    """Exchange refresh_token for a new access token. Updates DB. Returns new access_token or None."""
    import requests as req
    r = req.post(_MS_TOKEN_URL, data={
        "client_id":     AZURE_CLIENT_ID,
        "client_secret": AZURE_CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "scope":         _MS_SCOPES,
    }, timeout=10)
    if r.status_code != 200:
        return None
    data = r.json()
    expiry = int(datetime.now(timezone.utc).timestamp()) + int(data.get("expires_in", 3600))
    update_student_fields(student_id, {
        "outlook_access_token":  data["access_token"],
        "outlook_refresh_token": data.get("refresh_token", refresh_token),
        "outlook_token_expiry":  expiry,
    })
    return data["access_token"]


def _get_valid_outlook_token(student_id: int) -> str | None:
    """Return a valid Outlook access token for student, refreshing if needed."""
    student = get_student_by_id(student_id)
    if not student or not student.get("outlook_access_token"):
        return None
    now = int(datetime.now(timezone.utc).timestamp())
    expiry = student.get("outlook_token_expiry") or 0
    if now < expiry - 60:
        return student["outlook_access_token"]
    # Token expired — refresh
    return _refresh_outlook_token(student_id, student["outlook_refresh_token"])


@app.route("/api/auth/outlook")
@require_jwt
def outlook_auth_start():
    """Kick off Microsoft OAuth flow. Returns {redirect_url} for the frontend."""
    if not AZURE_CLIENT_ID:
        return jsonify({"error": "Outlook OAuth not configured"}), 503
    state = _make_outlook_state(g.student_id)
    redirect_uri = f"{APP_BASE_URL}/api/auth/outlook/callback"
    from urllib.parse import urlencode
    params = urlencode({
        "client_id":     AZURE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         _MS_SCOPES,
        "state":         state,
        "response_mode": "query",
    })
    return jsonify({"redirect_url": f"{_MS_AUTH_URL}?{params}"})


@app.route("/api/auth/outlook/callback")
def outlook_auth_callback():
    """Microsoft redirects here with ?code=...&state=... after user consent."""
    import requests as req
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    error = request.args.get("error", "")

    if error or not code:
        app.logger.error(f"[outlook/callback] Microsoft returned error: {error!r}, code present: {bool(code)}")
        return redirect(f"{APP_BASE_URL}/opportunities?outlook=error")

    student_id = _verify_outlook_state(state)
    if not student_id:
        app.logger.error(f"[outlook/callback] State verification failed for state: {state[:40]!r}")
        return redirect(f"{APP_BASE_URL}/opportunities?outlook=error")

    redirect_uri = f"{APP_BASE_URL}/api/auth/outlook/callback"
    app.logger.info(f"[outlook/callback] Exchanging code for tokens, redirect_uri={redirect_uri!r}, client_id={AZURE_CLIENT_ID!r}")
    r = req.post(_MS_TOKEN_URL, data={
        "client_id":     AZURE_CLIENT_ID,
        "client_secret": AZURE_CLIENT_SECRET,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
        "scope":         _MS_SCOPES,
    }, timeout=10)

    if r.status_code != 200:
        app.logger.error(f"[outlook/callback] Token exchange failed {r.status_code}: {r.text[:400]}")
        return redirect(f"{APP_BASE_URL}/opportunities?outlook=error")

    data = r.json()
    expiry = int(datetime.now(timezone.utc).timestamp()) + int(data.get("expires_in", 3600))
    update_student_fields(student_id, {
        "outlook_access_token":  data["access_token"],
        "outlook_refresh_token": data.get("refresh_token", ""),
        "outlook_token_expiry":  expiry,
    })
    return redirect(f"{APP_BASE_URL}/opportunities?outlook=connected")


@app.route("/api/auth/outlook/disconnect", methods=["POST"])
@require_jwt
def outlook_auth_disconnect():
    update_student_fields(g.student_id, {
        "outlook_access_token":  None,
        "outlook_refresh_token": None,
        "outlook_token_expiry":  None,
    })
    return jsonify({"status": "ok"})


@app.route("/api/outlook/send-direct", methods=["POST"])
@require_jwt
def outlook_send_direct():
    """Send an arbitrary email via the student's connected Outlook account."""
    import requests as req
    data    = request.get_json(silent=True) or {}
    to_addr = (data.get("to") or "").strip()
    subject = (data.get("subject") or "").strip()
    body    = (data.get("body") or "").strip()
    if not to_addr:
        return jsonify({"error": "recipient required"}), 400
    token = _get_valid_outlook_token(g.student_id)
    if not token:
        return jsonify({"error": "Outlook not connected"}), 403
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
        },
        "saveToSentItems": True,
    }
    r = req.post(
        _MS_GRAPH_SEND, json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code not in (200, 202):
        app.logger.error(f"[outlook/send-direct] {r.status_code}: {r.text[:300]}")
        return jsonify({"error": "Failed to send"}), 502
    return jsonify({"status": "sent"})


@app.route("/api/matches/<int:match_id>/send", methods=["POST"])
@require_jwt
def send_match_email(match_id):
    """Send the match email.

    If `manual: true` in the request body, just marks the match as sent in the
    DB without using Outlook (user sent it themselves via another client).
    Otherwise sends via the student's connected Outlook account.
    """
    import requests as req
    from db.database import execute as db_execute, USE_POSTGRES

    match = fetchone(
        "SELECT m.*, j.title as job_title FROM matches m JOIN jobs j ON j.id = m.job_id"
        " WHERE m.id = ? AND m.student_id = ?",
        (match_id, g.student_id),
    )
    if not match:
        return jsonify({"error": "Match not found"}), 404

    data   = request.get_json(silent=True) or {}
    manual = bool(data.get("manual"))

    def _mark_sent():
        if USE_POSTGRES:
            db_execute("UPDATE matches SET status = 'sent', sent_at = NOW() WHERE id = ?", (match_id,))
        else:
            db_execute("UPDATE matches SET status = 'sent', sent_at = datetime('now') WHERE id = ?", (match_id,))

    app.logger.info(f"[outlook/send] match_id={match_id} student_id={g.student_id} manual={manual}")

    if manual:
        _mark_sent()
        return jsonify({"status": "sent"})

    # ── Outlook send ──
    subject = data.get("subject") or match.get("email_subject") or ""
    body    = data.get("body")    or match.get("email_body")    or ""
    to_addr = data.get("to")      or match.get("expected_email") or ""

    app.logger.info(f"[outlook/send] to={to_addr!r} subject={subject[:60]!r}")

    if not to_addr:
        app.logger.error(f"[outlook/send] No recipient address. match keys: {list(match.keys())}")
        return jsonify({"error": "No recipient email address for this match"}), 400

    token = _get_valid_outlook_token(g.student_id)
    if not token:
        app.logger.error(f"[outlook/send] No valid Outlook token for student {g.student_id}")
        return jsonify({"error": "Outlook not connected"}), 403

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
        },
        "saveToSentItems": True,
    }
    app.logger.info(f"[outlook/send] Calling Graph API…")
    r = req.post(
        _MS_GRAPH_SEND,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )

    app.logger.info(f"[outlook/send] Graph API response {r.status_code}")
    if r.status_code not in (200, 202):
        app.logger.error(f"[outlook/send] Graph API error {r.status_code}: {r.text[:400]}")
        return jsonify({"error": "Failed to send email"}), 502

    _mark_sent()
    return jsonify({"status": "sent"})


# ── Jobs ──────────────────────────────────────────────────────────────────────

@app.route("/api/jobs")
def list_jobs():
    import json as _json
    limit = min(int(request.args.get("limit", 50)), 5000)
    from db.database import USE_POSTGRES
    if USE_POSTGRES:
        rows = fetchall(
            "SELECT *, "
            "  NULLIF(raw::jsonb->>'opening_date', '') AS od, "
            "  NULLIF(raw::jsonb->>'closing_date',  '') AS cd  "
            "FROM jobs WHERE company IS NOT NULL AND company != '' "
            "AND title IS NOT NULL AND title != '' "
            "ORDER BY NULLIF(raw::jsonb->>'opening_date', '') DESC NULLS LAST, "
            "         created_at DESC NULLS LAST LIMIT ?",
            (limit,)
        )
    else:
        rows = fetchall(
            "SELECT * FROM jobs WHERE company IS NOT NULL AND company != '' "
            "AND title IS NOT NULL AND title != '' "
            "ORDER BY created_at DESC NULLS LAST LIMIT ?",
            (limit,)
        )

    def _parse_row(r):
        raw = {}
        try:
            raw = _json.loads(r.get("raw") or "{}")
        except Exception:
            pass
        industries = raw.get("industries") or []
        if not industries and r.get("industry"):
            industries = [r["industry"]]
        seniority    = raw.get("seniority") or ""
        # od/cd = jsonb-extracted aliases; opening_date/closing_date = dedicated columns
        opening_date = r.get("od") or r.get("opening_date") or raw.get("opening_date") or ""
        closing_date = r.get("cd") or r.get("closing_date") or raw.get("closing_date") or ""
        return {
            "company_name": r.get("company") or "",
            "title":        r.get("title") or "",
            "industries":   industries,
            "region":       r.get("location") or "",
            "seniority":    seniority,
            "opening_date": opening_date,
            "closing_date": closing_date,
            "source_name":  r.get("source") or "",
            "url":          r.get("url") or "",
        }

    jobs = [_parse_row(r) for r in rows]
    return jsonify({"data": {"jobs": jobs}})


@app.route("/api/jobs/search")
@require_jwt
def search_jobs():
    q     = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 20)), 100)

    if not q:
        return jsonify({"results": []})

    student = fetchone("SELECT id FROM students WHERE id = ?", (g.student_id,))
    if not student:
        return jsonify({"error": "student not found"}), 404

    from db.database import USE_POSTGRES
    ilike = "ILIKE" if USE_POSTGRES else "LIKE"
    term  = f"%{q}%"

    jobs = fetchall(
        f"SELECT id, title, company, url, location, industry, role_type, opening_date "
        f"FROM jobs "
        f"WHERE (title {ilike} ? OR company {ilike} ?) "
        f"  AND (url IS NOT NULL AND url != '') "
        f"ORDER BY opening_date DESC NULLS LAST "
        f"LIMIT ?",
        (term, term, limit),
    )

    results = []
    seen_companies: set = set()
    for job in jobs:
        company = job["company"]
        if company in seen_companies:
            continue
        seen_companies.add(company)

        leads     = get_leads_for_company(company)
        best_lead = leads[0] if leads else None

        results.append({
            "job":  dict(job),
            "lead": dict(best_lead) if best_lead else None,
        })

    return jsonify({"results": results})


@app.route("/api/admin/jobs/cleanup", methods=["POST"])
@require_admin
def cleanup_blank_jobs():
    """Delete jobs with empty company or title (artefacts from old scraper bugs)."""
    from db.database import execute as db_execute, fetchone
    deleted = fetchone(
        "SELECT COUNT(*) as n FROM jobs WHERE company IS NULL OR company = '' OR title IS NULL OR title = ''"
    ) or {}
    count = deleted.get("n", 0)
    db_execute("DELETE FROM jobs WHERE company IS NULL OR company = '' OR title IS NULL OR title = ''")
    return jsonify({"status": "ok", "deleted": count})


@app.route("/api/admin/jobs/delete-source", methods=["POST"])
@require_admin
def delete_jobs_by_source():
    """Delete all jobs from a given source."""
    from db.database import execute as db_execute, fetchone
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    if not source:
        return jsonify({"error": "source required"}), 400
    row = fetchone("SELECT COUNT(*) as n FROM jobs WHERE source = ?", (source,)) or {}
    count = row.get("n", 0)
    db_execute("DELETE FROM jobs WHERE source = ?", (source,))
    return jsonify({"status": "ok", "deleted": count, "source": source})


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    from db.database import USE_POSTGRES
    if USE_POSTGRES:
        now_minus_7  = "NOW() - INTERVAL '7 days'"
        now_minus_14 = "NOW() - INTERVAL '14 days'"
        today_expr   = "CURRENT_DATE"
    else:
        now_minus_7  = "datetime('now', '-7 days')"
        now_minus_14 = "datetime('now', '-14 days')"
        today_expr   = "date('now')"

    active_jobs  = (fetchone(f"SELECT COUNT(*) as n FROM jobs WHERE created_at > {now_minus_14}") or {}).get("n", 0)
    companies    = (fetchone(f"SELECT COUNT(DISTINCT company) as n FROM jobs WHERE created_at > {now_minus_14}") or {}).get("n", 0)
    students     = (fetchone("SELECT COUNT(*) as n FROM students") or {}).get("n", 0)
    matches_today = (fetchone(
        f"SELECT COUNT(*) as n FROM matches WHERE match_date = {today_expr}"
    ) or {}).get("n", 0)
    emails_sent_week = (fetchone(
        f"SELECT COUNT(*) as n FROM matches WHERE status='sent' AND sent_at > {now_minus_7}"
    ) or {}).get("n", 0)

    by_source_rows = fetchall(f"SELECT source, COUNT(*) as n FROM jobs WHERE source IS NOT NULL AND created_at > {now_minus_14} GROUP BY source ORDER BY n DESC")
    by_source = {r["source"]: r["n"] for r in by_source_rows}

    by_industry_rows = fetchall(f"SELECT industry, COUNT(*) as n FROM jobs WHERE industry IS NOT NULL AND created_at > {now_minus_14} GROUP BY industry ORDER BY n DESC LIMIT 10")
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


@app.route("/api/admin/generate-cards", methods=["POST"])
@require_admin
def admin_generate_cards():
    """Force-regenerate today's cards for a student (by email or id)."""
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    student_id = data.get("student_id")

    if email:
        row = fetchone("SELECT id FROM students WHERE email = %s", (email,))
        if not row:
            return jsonify({"error": "student not found"}), 404
        student_id = row["id"]
    elif not student_id:
        return jsonify({"error": "email or student_id required"}), 400

    # Delete today's existing cards so they regenerate fresh
    from db.database import execute as db_execute
    db_execute(
        "DELETE FROM matches WHERE student_id = %s AND DATE(created_at) = CURRENT_DATE",
        (student_id,)
    )

    def _run():
        try:
            from pipeline.daily_cards import generate_daily_cards
            generate_daily_cards(student_id)
            print(f"[admin/generate-cards] done for student {student_id}")
        except Exception as exc:
            print(f"[admin/generate-cards] error for student {student_id}: {exc}", flush=True)

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered", "student_id": student_id})


@app.route("/api/admin/student-info")
@require_admin
def admin_student_info():
    """Return referral/quota info for a student by email."""
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    from db.database import fetchone as _fetchone, fetchall as _fetchall
    student = _fetchone(
        "SELECT id, email, name, referral_code, referred_by, daily_cards_override, notify_matches, notify_frequency "
        "FROM students WHERE lower(email) = ?", (email,)
    )
    if not student:
        return jsonify({"error": "not found"}), 404
    cards_today = _fetchall(
        "SELECT id, created_at FROM matches WHERE student_id = ? AND DATE(created_at) = CURRENT_DATE",
        (student["id"],)
    )
    return jsonify({"student": dict(student), "cards_today": len(cards_today)})


@app.route("/api/admin/credit-referral", methods=["POST"])
@require_admin
def admin_credit_referral():
    """Manually set daily_cards_override=5 for a student and top up today's cards."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    from db.database import fetchone as _fetchone, execute as _execute
    student = _fetchone("SELECT id FROM students WHERE lower(email) = ?", (email,))
    if not student:
        return jsonify({"error": "not found"}), 404
    student_id = student["id"]
    _execute("UPDATE students SET daily_cards_override = 5 WHERE id = ?", (student_id,))

    # Generate the extra cards in background (generate_daily_cards will fill up to quota)
    def _run():
        try:
            from pipeline.daily_cards import generate_daily_cards
            generate_daily_cards(student_id)
            print(f"[admin/credit-referral] cards topped up for student {student_id}")
        except Exception as exc:
            print(f"[admin/credit-referral] error: {exc}")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "ok", "student_id": student_id, "daily_cards_override": 5})


@app.route("/api/admin/fix-leads-apr2026", methods=["POST"])
@require_admin
def admin_fix_leads_apr2026():
    """One-time lead data cleanup: delete ghost lead, fix bad universities."""
    from db.database import execute as _execute, fetchall as _fetchall
    results = {}

    # 1. Delete Vishal Singh Chauhan (Facebook employee erroneously linked to D.E. Shaw)
    _execute("DELETE FROM leads WHERE name ILIKE 'Vishal Singh Chauhan'")
    results["deleted_vishal"] = True

    # 2. Clear Simon Zeng's false 'Unc' university
    _execute("UPDATE leads SET university = '' WHERE name = 'Simon Zeng' AND lower(university) = 'unc'")
    results["fixed_simon_zeng"] = True

    # 3. Fix Malay Patel's university — lived in New York, went to Harvard
    _execute("UPDATE leads SET university = 'Harvard' WHERE name = 'Malay Patel' AND lower(university) = 'york'")
    results["fixed_malay_patel"] = True

    # 4. Clear 'York' university for all US-based leads — false positive from 'New York' location
    # %% escapes the literal % so psycopg2 doesn't treat it as a placeholder
    _execute(
        "UPDATE leads SET university = '' WHERE lower(university) = 'york' "
        "AND (lower(location_country) LIKE '%%united states%%' OR lower(location_city) LIKE '%%new york%%')"
    )
    results["cleared_york_us"] = True

    return jsonify({"status": "ok", **results})


@app.route("/api/admin/delete-goldman-york", methods=["POST"])
@require_admin
def admin_delete_goldman_york():
    """Delete all Goldman Sachs leads tagged with York university (New York false positives)."""
    from db.database import execute as _execute, fetchall as _fetchall
    before = _fetchall("SELECT COUNT(*) AS n FROM leads WHERE lower(company)='goldman sachs' AND lower(university)='york'")
    n_before = (before[0].get("n") or 0) if before else 0
    _execute("DELETE FROM leads WHERE lower(company)='goldman sachs' AND lower(university)='york'")
    # Also delete any Goldman leads with name starting "Mark L"
    _execute("DELETE FROM leads WHERE lower(company)='goldman sachs' AND name ILIKE 'Mark L%'")
    after = _fetchall("SELECT COUNT(*) AS n FROM leads WHERE lower(company)='goldman sachs' AND lower(university)='york'")
    n_after = (after[0].get("n") or 0) if after else 0
    return jsonify({"status": "ok", "deleted": n_before - n_after, "remaining_york": n_after})


@app.route("/api/admin/fix-leads-may2026", methods=["POST"])
@require_admin
def admin_fix_leads_may2026():
    """May 2026 lead cleanup: remove NY-misidentified Goldman leads, fix Avis uni tag, fix Lazard lead type."""
    from db.database import execute as _execute
    results = {}

    # Delete specific Goldman Sachs leads that are NY-based engineers tagged with York uni
    goldman_names = [
        'Begüm Emirsoy', 'Moshe Malka', 'Jimmy Lu', 'Vikti Desai', 'Crystal Wu',
        'Sahib Singh', 'Andre Chow', 'Mohammed Ibrahim', 'Hunter Sevcik', 'Hao Zheng',
        'Jade Chen', 'Maitri Shah', 'Ricky Estrada', 'Nana Nimako', 'Sandeep Chaudhary',
        'Brian Wong', 'Jason Xie', 'Tejas Gururaja', 'Timothy Wing', 'Amber Wang',
    ]
    deleted = 0
    for name in goldman_names:
        _execute("DELETE FROM leads WHERE name ILIKE %s AND lower(company) = 'goldman sachs'", (name,))
        deleted += 1
    results["deleted_goldman_leads"] = deleted

    # Broader cleanup: clear York uni tag for any lead where location suggests New York
    _execute(
        "UPDATE leads SET university = '' WHERE lower(university) = 'york' "
        "AND (lower(location_country) LIKE '%%united states%%' OR lower(location_city) LIKE '%%new york%%')"
    )
    results["cleared_york_us"] = True

    # Fix Avis Budget Group university tag being set to company name
    _execute(
        "UPDATE leads SET university = '' WHERE lower(company) = 'avis budget group' "
        "AND lower(university) LIKE '%%avis%%'"
    )
    results["fixed_avis_university"] = True

    # Fix Nadine Chouari marked Relevant Team for Lazard (software engineer, not finance)
    _execute(
        "UPDATE leads SET lead_type = 'general' WHERE name ILIKE 'Nadine Chouari' "
        "AND lower(company) = 'lazard'"
    )
    results["fixed_nadine_chouari"] = True

    return jsonify({"status": "ok", **results})


@app.route("/api/admin/remove-na-eu-listings", methods=["POST"])
@require_admin
def admin_remove_na_eu_listings():
    """
    Delete all jobs sourced from the NA Finance and EU Finance Trackr pages
    (region IN ('US', 'EU'), source='trackr'), then remove leads for any
    company that has no remaining jobs after the deletion.
    """
    from db.database import fetchone, execute as _execute, USE_POSTGRES
    ph = "%s" if USE_POSTGRES else "?"

    # Diagnostic: check raw JSON for EU/US region values
    from db.database import fetchall
    sample = fetchall("SELECT DISTINCT location FROM jobs WHERE source='trackr' LIMIT 20")
    distinct_locations = [r.get("location") for r in sample]
    eu_raw = fetchall(
        "SELECT COUNT(*) AS cnt FROM jobs WHERE source='trackr' AND raw LIKE '%\"region\": \"EU\"%'"
        if not USE_POSTGRES else
        "SELECT COUNT(*) AS cnt FROM jobs WHERE source='trackr' AND raw::text LIKE '%\"region\": \"EU\"%'"
    )
    us_raw = fetchall(
        "SELECT COUNT(*) AS cnt FROM jobs WHERE source='trackr' AND raw LIKE '%\"region\": \"US\"%'"
        if not USE_POSTGRES else
        "SELECT COUNT(*) AS cnt FROM jobs WHERE source='trackr' AND raw::text LIKE '%\"region\": \"US\"%'"
    )
    eu_count_raw = (eu_raw[0] or {}).get("cnt", 0) if eu_raw else 0
    us_count_raw = (us_raw[0] or {}).get("cnt", 0) if us_raw else 0

    # Count before deletion for reporting
    before = (fetchone(
        f"SELECT COUNT(*) AS cnt FROM jobs WHERE source='trackr' AND location IN ({ph},{ph})",
        ("US", "EU"),
    ) or {}).get("cnt", 0)

    # Delete the job listings
    _execute(
        f"DELETE FROM jobs WHERE source='trackr' AND location IN ({ph},{ph})",
        ("US", "EU"),
    )

    # Remove leads for companies that no longer have any active jobs
    if USE_POSTGRES:
        _execute(
            """DELETE FROM leads
               WHERE lower(company) NOT IN (
                   SELECT DISTINCT lower(company) FROM jobs
                   WHERE company IS NOT NULL AND company != ''
               )"""
        )
    else:
        _execute(
            """DELETE FROM leads
               WHERE lower(company) NOT IN (
                   SELECT DISTINCT lower(company) FROM jobs
                   WHERE company IS NOT NULL AND company != ''
               )"""
        )

    after_jobs = (fetchone("SELECT COUNT(*) AS cnt FROM jobs WHERE source='trackr'") or {}).get("cnt", 0)

    return jsonify({
        "status":        "ok",
        "jobs_deleted":  before,
        "trackr_jobs_remaining": after_jobs,
        "debug_distinct_locations": distinct_locations,
        "debug_eu_in_raw": eu_count_raw,
        "debug_us_in_raw": us_count_raw,
    })


@app.route("/api/admin/fix-xmg-leads", methods=["POST"])
@require_admin
def admin_fix_xmg_leads():
    """
    Upsert XMG job + 6 curated leads with Groq-derived email format.
    Calls Groq to discover XMG's email domain/format, computes expected_email
    for each lead, and inserts the XMG job into jobs if not already present.
    """
    from db.database import upsert_lead, execute as _execute, save_email_format, fetchone as _fetchone, USE_POSTGRES
    from pipeline.lead_builder import _lookup_email_format_via_llm, _infer_email
    from datetime import date, datetime

    # 1. Groq email format lookup for XMG
    groq_result = _lookup_email_format_via_llm("XMG")
    fmt_code = "FL"
    domain   = ""
    if groq_result:
        fmt_code, domain = groq_result
        try:
            save_email_format("XMG", fmt_code, domain, source="groq")
        except Exception:
            pass
        app.logger.info(f"XMG email format (Groq): {fmt_code} @ {domain}")
    else:
        app.logger.warning("XMG email format lookup failed — emails will be empty")

    # 2. Ensure an XMG job exists so it appears on the opportunities page
    ph = "%s" if USE_POSTGRES else "?"
    existing_job = _fetchone(f"SELECT id FROM jobs WHERE lower(company) = lower({ph})", ("XMG",))
    if not existing_job:
        today = date.today().isoformat()
        now   = datetime.utcnow().isoformat()
        _execute(
            f"INSERT INTO jobs (title, company, url, source, industry, opening_date, created_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            ("Equity Trading and Research Interns", "XMG",
             "https://xmg-inc.com/careers", "trackr", "Finance", today, now),
        )
        app.logger.info("Inserted XMG job into jobs table")

    # 3. Clear stale XMG leads and re-insert with computed emails
    _execute("DELETE FROM leads WHERE lower(company) IN ('xmg', 'xmg inc', 'xmg, inc.')")

    _LEADS = [
        ("Ken Kemal",      "Chief Executive Officer",                            "https://www.linkedin.com/in/ken-kemal-6690921",      "exec",     1, ""),
        ("Julian Faber",   "Low Latency Quant C++ Software Engineer",            "https://www.linkedin.com/in/julian-faber-56454484",  "relevant", 2, ""),
        ("Garrett Nenner", "Co-Founder & CEO eZorro | Ex-Millennium & BofA",     "https://www.linkedin.com/in/garrett-nenner",         "exec",     3, ""),
        ("Sunny Stalham",  "Risk Manager at XMG Inc",                           "https://www.linkedin.com/in/sunny-stalham-881735193","relevant", 4, ""),
        ("Lev Butin",      "Master of Quantitative Finance @ Rutgers",           "https://www.linkedin.com/in/lev-butin-392726323",    "general",  5, "Rutgers"),
        ("William Lopez",  "Regional Sales Manager at XMG, Inc.",               "https://www.linkedin.com/in/william-lopez-aa457b",   "general",  6, ""),
    ]

    leads = []
    for name, title, li_url, lead_type, rank, uni in _LEADS:
        email = _infer_email(name, "XMG") if domain else ""
        leads.append({
            "name": name, "title": title,
            "company": "XMG", "university": uni, "snippet": "",
            "linkedin_url": li_url,
            "location_city": "", "location_country": "",
            "tenure_months": 0, "is_alumni": False,
            "dept_tag": "sales_trading", "lead_type": lead_type,
            "scraped_rank": rank,
            "job_title": "Equity Trading and Research Interns",
            "job_expected_email": email, "job_opening_date": "",
        })

    for lead in leads:
        upsert_lead(lead)

    emails_found = [l["job_expected_email"] for l in leads if l["job_expected_email"]]
    return jsonify({
        "status":        "ok",
        "email_format":  f"{fmt_code}@{domain}" if domain else "unknown",
        "emails_computed": len(emails_found),
        "sample_email":  emails_found[0] if emails_found else None,
        "inserted":      len(leads),
    })


@app.route("/api/admin/build-leads", methods=["POST"])
@require_admin
def admin_build_leads():
    data    = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    force   = bool(data.get("force", False))

    def _run():
        try:
            from pipeline.lead_builder import build_leads
            build_leads(company_filter=company, top_n=0 if company else 50, force=force)
        except Exception as exc:
            print(f"[admin/build-leads] error: {exc}")

    import threading
    threading.Thread(target=_run, daemon=True).start()
    label = company if company else "top-50 companies"
    return jsonify({"status": "triggered", "company": label, "force": force})


@app.route("/api/admin/leads/stats")
@require_admin
def admin_leads_stats():
    """Return aggregate stats about the pre-fetched leads pool."""
    try:
        from db.database import get_leads_stats
        stats = get_leads_stats()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"data": stats})


@app.route("/api/admin/scrape", methods=["POST"])
@require_admin
def admin_scrape():
    data = request.get_json(silent=True) or {}
    source_id = data.get("source_id")  # None = run all

    def _run():
        try:
            from pipeline.ingest import run_all_scrapers
            run_all_scrapers(source_ids=[source_id] if source_id else None)
        except Exception as exc:
            print(f"[admin/scrape] error: {exc}")

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered"})


@app.route("/api/admin/trackr-pipeline", methods=["POST"])
@require_admin
def admin_trackr_pipeline():
    """Run full Trackr pipeline: scrape + lead builder for underserved companies."""
    def _run():
        try:
            from scheduler.run import run_trackr_pipeline
            run_trackr_pipeline()
        except Exception as exc:
            print(f"[admin/trackr-pipeline] error: {exc}")

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered"})


@app.route("/api/admin/build-leads-sequential", methods=["POST"])
@require_admin
def admin_build_leads_sequential():
    """Build leads for a list of companies sequentially (avoids Serper rate limits)."""
    data      = request.get_json(silent=True) or {}
    companies = data.get("companies") or []
    force     = bool(data.get("force", True))

    def _run():
        import time as _t
        from pipeline.lead_builder import build_leads
        for company in companies:
            try:
                logger.info(f"[sequential leads] starting: {company}")
                build_leads(company_filter=company, top_n=0, force=force)
                logger.info(f"[sequential leads] done: {company}")
            except Exception as exc:
                logger.error(f"[sequential leads] error for {company}: {exc}")
            _t.sleep(5)  # brief pause between companies

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered", "companies": companies, "count": len(companies)})


@app.route("/api/admin/fix-email-formats", methods=["POST"])
@require_admin
def admin_fix_email_formats():
    from pipeline.lead_builder import fix_ats_email_formats
    n = fix_ats_email_formats()
    return jsonify({"status": "done", "fixed": n})


@app.route("/api/admin/send-notification", methods=["POST"])
@require_admin
def admin_send_notification():
    """Manually send the daily matches notification to a student by email."""
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    student = fetchone("SELECT * FROM students WHERE lower(email) = ?", (email,))
    if not student:
        return jsonify({"error": "student not found"}), 404
    # Also enable notify_matches if it wasn't set
    if not student.get("notify_matches"):
        update_student_fields(student["id"], {"notify_matches": True})
        student = dict(student)
        student["notify_matches"] = True
    from utils.notifications import send_daily_matches_ready
    ok = send_daily_matches_ready(dict(student))
    return jsonify({"status": "sent" if ok else "failed", "email": email})


@app.route("/api/admin/runs")
@require_admin
def admin_runs():
    try:
        rows = fetchall(
            "SELECT source_id, source_name, status, jobs_found, jobs_new, "
            "error_msg, duration_s, finished_at FROM scrape_runs "
            "ORDER BY finished_at DESC LIMIT 200"
        )
    except Exception:
        rows = []
    return jsonify({"data": {"runs": rows}})


@app.route("/api/admin/export/students")
@require_admin
def admin_export_students_csv():
    """Export all active students with today's match snapshot as a CSV download."""
    import csv, io
    rows = fetchall("""
        SELECT s.id, s.email, s.name, s.age, s.status,
               s.industries, s.company_size, s.bio, s.university,
               s.created_at, s.last_seen, s.deactivated_at,
               s.match1_name_title, s.match1_linkedin, s.match1_job_url,
               s.match2_name_title, s.match2_linkedin, s.match2_job_url,
               s.match3_name_title, s.match3_linkedin, s.match3_job_url,
               s.matches_updated_date,
               s.outlook_access_token, s.outlook_refresh_token, s.outlook_token_expiry
        FROM students s
        ORDER BY s.id ASC
    """)
    output = io.StringIO()
    fieldnames = [
        "id", "email", "name", "age", "status",
        "industries", "company_size", "bio", "university",
        "created_at", "last_seen", "deactivated_at",
        "match1_name_title", "match1_linkedin", "match1_job_url",
        "match2_name_title", "match2_linkedin", "match2_job_url",
        "match3_name_title", "match3_linkedin", "match3_job_url",
        "matches_updated_date",
        "outlook_access_token", "outlook_refresh_token", "outlook_token_expiry",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))
    csv_bytes = output.getvalue().encode("utf-8")
    from flask import Response
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )


@app.route("/api/admin/students")
@require_admin
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


@app.route("/api/admin/delete-students", methods=["POST"])
@require_admin
def admin_delete_students():
    """Delete students by email list. Cascades to matches, refresh_tokens, signals, card_queue."""
    from db.database import execute as db_execute
    data = request.get_json(silent=True) or {}
    emails = data.get("emails", [])
    if not emails:
        return jsonify({"error": "emails list required"}), 400
    rows = fetchall(
        "SELECT id, email FROM students WHERE email = ANY(%s)", (emails,)
    )
    ids = [r["id"] for r in rows]
    if not ids:
        return jsonify({"deleted": 0, "not_found": emails})
    for tbl in ("matches", "refresh_tokens", "signals", "card_queue"):
        try:
            db_execute(f"DELETE FROM {tbl} WHERE student_id = ANY(%s)", (ids,))
        except Exception:
            pass
    db_execute("DELETE FROM students WHERE id = ANY(%s)", (ids,))
    return jsonify({"deleted": len(ids), "emails": [r["email"] for r in rows]})


@app.route("/api/admin/regenerate-all-drafts", methods=["POST"])
@require_admin
def admin_regenerate_all_drafts():
    """Regenerate email subject + body for every match using the current template."""
    from pipeline.email_templates import template_standard
    from db.database import execute as db_execute, USE_POSTGRES

    rows = fetchall("""
        SELECT m.id,
               m.person_name, m.person_company,
               s.name  AS student_name,
               s.university AS student_university,
               s.bio   AS student_bio,
               j.title AS job_title,
               j.industry
        FROM matches m
        JOIN students s ON s.id = m.student_id
        JOIN jobs    j ON j.id = m.job_id
        WHERE m.status != 'sent'
    """)

    ph = "%s" if USE_POSTGRES else "?"
    updated = 0
    for r in rows:
        name_parts = (r.get("person_name") or "").split()
        first = name_parts[0] if name_parts else ""
        ctx = {
            "student_name":         r.get("student_name") or "",
            "student_university":   r.get("student_university") or "",
            "student_bio":          r.get("student_bio") or "",
            "recipient_first_name": first,
            "recipient_company":    r.get("person_company") or "",
            "job_department":       "",
            "industry_hint":        r.get("industry") or "this field",
        }
        subject, body = template_standard(ctx)
        db_execute(
            f"UPDATE matches SET email_subject = {ph}, email_body = {ph} WHERE id = {ph}",
            (subject, body, r["id"])
        )
        updated += 1

    app.logger.info(f"Regenerated drafts for {updated} matches")
    return jsonify({"status": "ok", "updated": updated})


@app.route("/api/admin/suppressions")
@require_admin
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
@app.route("/home")
def landing():
    return _send_html("newlandingdraft.html")


@app.route("/login")
def login():
    return _send_html("login.html")


@app.route("/signup")
def signup():
    return _send_html("inroad-signup.html")


@app.route("/onboarding")
def onboarding():
    return _send_html("inroad-onboarding.html")


@app.route("/dashboard")
def dashboard():
    return redirect("/opportunities", 301)


@app.route("/settings")
def settings_page():
    return _send_html("inroad-settings.html")


@app.route("/opportunities")
def opportunities_page():
    return _send_html("inroad-opportunities.html")


@app.route("/pipeline")
def pipeline_page():
    return _send_html("inroad-pipeline.html")


@app.route("/listings")
def listings_page():
    return _send_html("inroad-listings.html")


# In-process cache for /api/opportunities — recomputed at most once every 5 minutes
_opp_cache: dict = {"data": None, "expires_at": 0.0}

@app.route("/api/opportunities")
def api_opportunities():
    """Return all jobs with insider leads, formatted for the opportunities page."""
    import time as _time
    from datetime import datetime, timezone
    from db.database import USE_POSTGRES

    now = _time.time()
    if _opp_cache["data"] is not None and now < _opp_cache["expires_at"]:
        resp = jsonify(_opp_cache["data"])
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    # Firm type lookup by company name (used as section headers)
    _INDUSTRY_VERTICAL = {
        "Finance":    "Finance",
        "Technology": "Tech",
        "Law":        "Law",
        "Marketing":  "Marketing",
    }

    def infer_firm_type(industry: str, raw_json: str = "") -> str:
        if raw_json:
            try:
                import json as _j
                raw_data = _j.loads(raw_json)
                cats = raw_data.get("trackr_categories") or []
                if "Consulting" in cats:
                    return "Consulting"
                # Only fall back to Miscellaneous for events if industry has no known vertical
                if raw_data.get("trackr_type") == "events" and not _INDUSTRY_VERTICAL.get(industry or ""):
                    return "Miscellaneous"
            except Exception:
                pass
        return _INDUSTRY_VERTICAL.get(industry or "", "Miscellaneous")

    def extract_region(raw_json: str) -> str:
        if not raw_json:
            return "UK"
        try:
            import json as _j
            return _j.loads(raw_json).get("region") or "UK"
        except Exception:
            return "UK"

    _TRACKR_TYPE_LABEL = {
        "summer-internships":    "Summer Internship",
        "spring-weeks":          "Spring Week",
        "insight-programmes":    "Spring Week",
        "off-cycle-internships": "Off-Cycle Internship",
        "industrial-placements": "Industrial Placement",
        "graduate-programmes":   "Graduate Programme",
        "full-time-programmes":  "Graduate Programme",
        "training-contracts":    "Graduate Programme",
        "pre-uni":               "Pre-Uni",
        "events":                "Events",
    }

    def infer_programme_type(title: str, raw_json: str = "") -> str:
        import json as _json
        # Use the authoritative trackr_type stored in raw JSON when available
        if raw_json:
            try:
                raw_data = _json.loads(raw_json)
                tt = raw_data.get("trackr_type", "")
                if tt and tt in _TRACKR_TYPE_LABEL:
                    return _TRACKR_TYPE_LABEL[tt]
            except Exception:
                pass
        # Title-based fallback for legacy rows without trackr_type
        t = title.lower()
        if any(k in t for k in ['spring week', 'spring insight', 'spring into', 'spring programme', 'spring internship']):
            return 'Spring Week'
        if any(k in t for k in ['off-cycle', 'off cycle', 'offcycle']):
            return 'Off-Cycle Internship'
        if any(k in t for k in ['industrial placement', 'placement year', 'year in industry', 'sandwich', 'year placement']):
            return 'Industrial Placement'
        if any(k in t for k in ['pre-uni', 'pre uni', 'school leaver', 'year 12', 'year 13', 'sixth form']):
            return 'Pre-Uni'
        if any(k in t for k in ['event', 'open day', 'insight day', 'discovery', 'information session', 'networking']):
            return 'Events'
        if any(k in t for k in ['graduate programme', 'grad scheme', 'graduate scheme', 'grad programme',
                                  'analyst programme', 'graduate analyst', 'graduate rotational']):
            return 'Graduate Programme'
        if 'graduate' in t or ('grad ' in t and 'internship' not in t):
            return 'Graduate Programme'
        return 'Summer Internship'  # default for trackr internships

    def infer_division(title: str, industry: str) -> str:
        return ""  # kept for compat; firm type now drives sections

    def make_initials(name: str) -> str:
        parts = (name or "").strip().split()
        if not parts:
            return "?"
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()

    # Fetch jobs — compatible SQL for both Postgres and SQLite
    if USE_POSTGRES:
        order_clause = "ORDER BY opening_date DESC NULLS LAST, created_at DESC NULLS LAST"
    else:
        order_clause = "ORDER BY CASE WHEN opening_date IS NULL OR opening_date = '' THEN 1 ELSE 0 END, opening_date DESC, created_at DESC"

    rows = fetchall(
        "SELECT id, title, company, url, opening_date, closing_date, industry, created_at, raw, careers_site "
        "FROM jobs WHERE company IS NOT NULL AND company != '' "
        f"AND title IS NOT NULL AND title != '' AND source IN ('trackr', 'wttj') "
        f"AND lower(company) != 'trackr' {order_clause} LIMIT 10000"
    )

    if not rows:
        return jsonify({"jobs": [], "count": 0, "updated_at": datetime.now(timezone.utc).isoformat()})

    # Batch-fetch leads for all companies in a single query to avoid N+1
    companies = list({(r.get("company") or "").lower() for r in rows if r.get("company")})

    leads_by_company: dict = {}
    if companies:
        now_expr = "NOW()" if USE_POSTGRES else "datetime('now')"
        cols = "id, name, title, job_title, linkedin_url, job_expected_email, company, lead_type, is_alumni, university, tenure_months"
        if USE_POSTGRES:
            all_leads = fetchall(
                f"SELECT {cols} FROM leads "
                f"WHERE lower(company) = ANY(%s) "
                f"AND (stale_after IS NULL OR stale_after > {now_expr}) "
                f"ORDER BY CASE lead_type WHEN 'relevant' THEN 0 WHEN 'exec' THEN 1 WHEN 'hr' THEN 2 ELSE 3 END, "
                f"is_alumni DESC, tenure_months DESC",
                (companies,),
            )
        else:
            placeholders = ",".join(["?"] * len(companies))
            all_leads = fetchall(
                f"SELECT {cols} FROM leads "
                f"WHERE lower(company) IN ({placeholders}) "
                f"AND (stale_after IS NULL OR stale_after > {now_expr}) "
                f"ORDER BY CASE lead_type WHEN 'relevant' THEN 0 WHEN 'exec' THEN 1 WHEN 'hr' THEN 2 ELSE 3 END, "
                f"is_alumni DESC, tenure_months DESC",
                tuple(companies),
            )
        for l in all_leads:
            key = (l.get("company") or "").lower()
            leads_by_company.setdefault(key, []).append(l)

    jobs = []
    for r in rows:
        company = r.get("company") or ""
        title   = r.get("title") or ""

        raw_leads = leads_by_company.get(company.lower(), [])
        leads = []
        for l in raw_leads:
            name   = l.get("name") or ""
            badges = []
            if l.get("university"):
                badges.append("uni")
            is_alumni = l.get("is_alumni")
            if is_alumni and is_alumni not in (0, False, "0", "false"):
                badges.append("exp")
            leads.append({
                "id":             l.get("id"),
                "name":           name,
                "title":          l.get("title") or l.get("job_title") or "",
                "initials":       make_initials(name),
                "linkedin_url":   l.get("linkedin_url") or "",
                "expected_email": l.get("expected_email") or l.get("job_expected_email") or "",
                "badges":         badges,
                "university":     l.get("university") or "",
                "lead_type":      l.get("lead_type") or "relevant",
                "verified":       False,
            })

        _IND_LABEL = {"Finance": "Finance", "Law": "Law", "Technology": "Consulting"}
        import json as _json
        try:
            _raw_data = _json.loads(r.get("raw") or "{}")
        except Exception:
            _raw_data = {}
        jobs.append({
            "id":             r.get("id"),
            "company":        company,
            "programme":      title,
            "division":       infer_firm_type(r.get("industry") or "", r.get("raw") or ""),
            "programme_type": infer_programme_type(title, r.get("raw") or ""),
            "region":         extract_region(r.get("raw") or ""),
            "opening_date":   r.get("opening_date") or "",
            "closing_date":   r.get("closing_date") or "",
            "apply_url":      r.get("url") or "",
            "careers_site":   r.get("careers_site") or "",
            "logo_url":       _raw_data.get("logo_url") or "",
            "industry_label": _IND_LABEL.get(r.get("industry") or "", ""),
            "leads":          leads,
        })

    result = {
        "jobs":       jobs,
        "count":      len(jobs),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _opp_cache["data"]       = result
    _opp_cache["expires_at"] = _time.time() + 300  # 5-minute TTL
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/admin")
@require_admin
def admin_page():
    if not ADMIN_UI_ENABLED:
        return "", 404
    return _send_html("inroad-admin.html")


@app.route("/unsubscribe")
def unsubscribe_page():
    return _send_html("inroad-unsubscribe.html")


@app.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    """Consume an unsubscribe token and set notify_matches=False."""
    from utils.notifications import verify_magic_token
    data  = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify(error="missing token"), 400
    result = verify_magic_token(token)
    if not result or result.get("purpose") != "unsubscribe":
        return jsonify(error="invalid or expired token"), 400
    email = result["email"]
    execute("UPDATE students SET notify_matches = FALSE WHERE LOWER(email) = ?", (email,))
    logger.info(f"Unsubscribed {email} from match emails")
    return jsonify(status="unsubscribed", email=email)


@app.route("/privacy")
def privacy_page():
    return _send_html("privacy.html")


@app.route("/terms")
def terms_page():
    return _send_html("terms.html")


@app.route("/outlook-help")
def outlook_help_page():
    return _send_html("outlook-help.html")


@app.route("/metrics")
@require_admin
def metrics_page():
    return _send_html("inroad-metrics.html")


@app.route("/api/admin/debug-lastseen")
@require_admin
def debug_lastseen():
    from db.database import USE_POSTGRES
    rows = fetchall(
        "SELECT id, email, last_seen FROM students "
        "ORDER BY last_seen DESC NULLS LAST LIMIT 20"
        if USE_POSTGRES else
        "SELECT id, email, last_seen FROM students "
        "ORDER BY CASE WHEN last_seen IS NULL THEN 1 ELSE 0 END, last_seen DESC LIMIT 20"
    )
    return jsonify({"rows": [dict(r) for r in rows]})


@app.route("/api/flags")
def feature_flags():
    """Public endpoint returning enabled feature flags for the current environment."""
    return jsonify({
        "personalise": bool(os.environ.get("PERSONALISE_FEATURE")),
    })


@app.route("/api/admin/metrics")
@require_admin
def admin_metrics():
    from db.database import USE_POSTGRES
    if USE_POSTGRES:
        date_trunc  = "DATE_TRUNC('day', created_at)::date"
        seen_trunc  = "DATE_TRUNC('day', last_seen)::date"
        day30       = "NOW() - INTERVAL '30 days'"
        day30_date  = "CURRENT_DATE - INTERVAL '30 days'"
        today_expr  = "CURRENT_DATE"
    else:
        date_trunc  = "date(created_at)"
        seen_trunc  = "date(last_seen)"
        day30       = "datetime('now', '-30 days')"
        day30_date  = "date('now', '-30 days')"
        today_expr  = "date('now')"

    # Total accounts
    total_accounts = (fetchone("SELECT COUNT(*) AS n FROM students") or {}).get("n", 0)

    # New signups per day (last 30 days)
    acct_rows = fetchall(
        f"SELECT {date_trunc} AS date, COUNT(*) AS count FROM students "
        f"WHERE created_at >= {day30} GROUP BY 1 ORDER BY 1"
    )
    accounts_by_day = [{"date": str(r["date"]), "count": r["count"]} for r in acct_rows]

    # Unique site visits — cookie-based, covers logged-in and non-logged-in
    unique_today = (fetchone(
        f"SELECT COUNT(DISTINCT visitor_id) AS n FROM site_visits "
        f"WHERE visited_date = {today_expr}"
    ) or {}).get("n", 0)

    visitors_rows = fetchall(
        f"SELECT visited_date AS date, COUNT(DISTINCT visitor_id) AS count FROM site_visits "
        f"WHERE visited_date >= {day30_date} GROUP BY 1 ORDER BY 1"
    )
    visitors_all = [{"date": str(r["date"]), "count": r["count"]} for r in visitors_rows]

    # DAU / MAU — logged-in students who visited today / this month
    dau = (fetchone(
        f"SELECT COUNT(*) AS n FROM students "
        f"WHERE last_seen IS NOT NULL AND {seen_trunc} = {today_expr}"
    ) or {}).get("n", 0)

    mau = (fetchone(
        f"SELECT COUNT(*) AS n FROM students "
        f"WHERE last_seen IS NOT NULL AND last_seen >= {day30}"
    ) or {}).get("n", 0)

    # Email reveals
    if USE_POSTGRES:
        sig_trunc = "DATE_TRUNC('day', created_at)::date"
    else:
        sig_trunc = "date(created_at)"

    total_reveals = (fetchone(
        "SELECT COUNT(*) AS n FROM signals WHERE signal = 'email_revealed'"
    ) or {}).get("n", 0)

    reveals_today = (fetchone(
        f"SELECT COUNT(*) AS n FROM signals WHERE signal = 'email_revealed' AND {sig_trunc} = {today_expr}"
    ) or {}).get("n", 0)

    reveals_rows = fetchall(
        f"SELECT {sig_trunc} AS date, COUNT(*) AS count FROM signals "
        f"WHERE signal = 'email_revealed' AND created_at >= {day30} GROUP BY 1 ORDER BY 1"
    )
    reveals_by_day = [{"date": str(r["date"]), "count": r["count"]} for r in reveals_rows]

    return jsonify({"data": {
        "total_accounts":        total_accounts,
        "accounts_by_day":       accounts_by_day,
        "unique_visitors_today": unique_today,
        "visitors_all":          visitors_all,
        "dau":                   dau,
        "mau":                   mau,
        "total_reveals":         total_reveals,
        "reveals_today":         reveals_today,
        "reveals_by_day":        reveals_by_day,
    }})




@app.route("/contact")
def contact_page():
    return _send_html("contact.html")


@app.route("/api/contact", methods=["POST"])
def api_contact():
    """Forward a contact form submission to oliver@the-inroad.com via Resend."""
    import requests as req
    data    = request.get_json(silent=True) or {}
    name    = (data.get("name")    or "").strip()
    email   = (data.get("email")   or "").strip()
    subject = (data.get("subject") or "Contact form").strip()
    message = (data.get("message") or "").strip()

    if not email or not message:
        return jsonify({"error": "email and message required"}), 400

    from config.settings import RESEND_API_KEY
    if not RESEND_API_KEY:
        return jsonify({"error": "email not configured"}), 503

    body_html = f"""
    <p><strong>From:</strong> {name} &lt;{email}&gt;</p>
    <p><strong>Subject:</strong> {subject}</p>
    <hr>
    <p style="white-space:pre-wrap">{message}</p>
    """

    r = req.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from":    f"{FROM_NAME} <{FROM_EMAIL}>",
            "to":      ["oliver@the-inroad.com"],
            "reply_to": email,
            "subject": f"[inroad contact] {subject}",
            "html":    body_html,
        },
        timeout=10,
    )
    if r.status_code not in (200, 201):
        app.logger.error(f"[contact] Resend error {r.status_code}: {r.text[:200]}")
        return jsonify({"error": "Failed to send"}), 502

    return jsonify({"status": "sent"})


@app.route("/verify")
def verify_page():
    """Client-side page that exchanges a magic-link token for a JWT."""
    return send_from_directory(os.path.join(ROOT, "static"), "verify.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve files from the project-level static/ directory."""
    return send_from_directory(os.path.join(ROOT, "static"), filename)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(ROOT, "static"), "favicon.svg", mimetype="image/svg+xml")


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5001))
    debug = DEV_MODE
    print(f"[inroad] Starting on port {port} | production={IS_PRODUCTION} | debug={debug}")
    app.run(host="0.0.0.0", port=port, debug=debug)
