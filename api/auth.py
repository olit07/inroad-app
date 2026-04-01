"""
api/auth.py
JWT utilities and the require_jwt decorator.
"""

import jwt
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, jsonify, g
from config import settings
from db import database as db


def make_access_token(student_id: int) -> str:
    """Issue a short-lived JWT access token."""
    payload = {
        "sub": student_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_ACCESS_TTL_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def make_refresh_token_str() -> str:
    """Generate a random opaque refresh token string."""
    return secrets.token_urlsafe(48)


def verify_access_token(token: str) -> dict | None:
    """Decode and verify JWT. Returns payload dict or None."""
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def require_jwt(f):
    """Decorator: require a valid JWT Bearer token. Sets g.student_id."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing token"}), 401
        token = auth_header[7:]
        payload = verify_access_token(token)
        if not payload:
            return jsonify({"error": "invalid or expired token"}), 401
        g.student_id = payload["sub"]
        return f(*args, **kwargs)
    return decorated
