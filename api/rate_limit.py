"""
inroad — Simple in-memory rate limiter for the Flask API.

Limits:
  - 60 requests/minute per IP for general endpoints
  - 10 requests/minute per IP for /api/students/register
  - 30 requests/minute per IP for /api/matches/today

Uses a sliding window counter stored in-process memory.
For production, replace with Redis-backed rate limiting.
"""
import time
import logging
from collections import defaultdict, deque
from functools import wraps
from flask import request, jsonify

logger = logging.getLogger(__name__)

# {ip: deque of timestamps}
_windows: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(deque))

LIMITS = {
    "default":  (300, 60),   # 300 req / 60s
    "register": (10,  60),   # 10 req / 60s
    "matches":  (60,  60),   # 60 req / 60s
    "admin":    (200, 60),   # 200 req / 60s
}


def _check_rate(ip: str, bucket: str) -> bool:
    """Returns True if request is allowed, False if rate limited."""
    max_reqs, window_secs = LIMITS.get(bucket, LIMITS["default"])
    now  = time.time()
    dq   = _windows[ip][bucket]

    # Evict old timestamps
    while dq and dq[0] < now - window_secs:
        dq.popleft()

    if len(dq) >= max_reqs:
        return False

    dq.append(now)
    return True


def rate_limit(bucket: str = "default"):
    """Decorator that applies rate limiting to a Flask route."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            ip = ip.split(",")[0].strip()

            if not _check_rate(ip, bucket):
                logger.warning(f"Rate limited: {ip} on {bucket}")
                return jsonify({
                    "ok": False,
                    "error": "Rate limit exceeded. Please wait before retrying."
                }), 429

            return fn(*args, **kwargs)
        return wrapper
    return decorator


def init_rate_limiting(app):
    """Apply rate limiting to /api/ and /auth/ routes only."""
    @app.before_request
    def check_global_rate():
        path = request.path
        # Only rate-limit API and auth endpoints — not static files or HTML pages
        if not (path.startswith("/api/") or path.startswith("/auth/")):
            return

        ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
        ip = ip.split(",")[0].strip()

        # Determine bucket
        if "register" in path:
            bucket = "register"
        elif "today" in path:
            bucket = "matches"
        elif "admin" in path:
            bucket = "admin"
        else:
            bucket = "default"

        if not _check_rate(ip, bucket):
            logger.warning(f"Global rate limit: {ip} → {path}")
            return jsonify({
                "ok": False,
                "error": "Too many requests. Please slow down."
            }), 429
