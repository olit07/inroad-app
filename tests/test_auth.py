"""
tests/test_auth.py
Auth test suite — unit tests (no DB/Flask) and integration tests.
"""

import os
import sys
import tempfile
import pytest
import jwt as pyjwt

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Make sure the project root is importable regardless of how pytest is invoked
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ===========================================================================
# Unit tests — no DB, no Flask app
# ===========================================================================

class TestMakeAccessToken:
    """api.auth.make_access_token produces a well-formed JWT."""

    def test_make_access_token_structure(self):
        from api.auth import make_access_token
        from config import settings

        token = make_access_token(42)
        payload = pyjwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])

        assert payload["sub"] == 42
        assert "exp" in payload
        assert "iat" in payload
        # exp should be in the future
        assert payload["exp"] > datetime.now(timezone.utc).timestamp()


class TestVerifyAccessToken:
    """api.auth.verify_access_token handles valid, expired, and tampered tokens."""

    def test_verify_access_token_valid(self):
        from api.auth import make_access_token, verify_access_token

        token = make_access_token(7)
        payload = verify_access_token(token)

        assert payload is not None
        assert payload["sub"] == 7

    def test_verify_access_token_expired(self):
        from api.auth import verify_access_token
        from config import settings

        # Build a token whose exp is already in the past
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        expired_token = pyjwt.encode(
            {"sub": 99, "iat": past - timedelta(minutes=15), "exp": past},
            settings.JWT_SECRET,
            algorithm="HS256",
        )

        assert verify_access_token(expired_token) is None

    def test_verify_access_token_bad_signature(self):
        from api.auth import verify_access_token

        # Sign with a different secret
        token = pyjwt.encode(
            {
                "sub": 1,
                "iat": datetime.now(timezone.utc),
                "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
            },
            "totally-wrong-secret",
            algorithm="HS256",
        )

        assert verify_access_token(token) is None


class TestRequireJwtDecorator:
    """require_jwt decorator rejects requests without a valid Bearer token."""

    def _make_mini_app(self):
        """Minimal Flask app with one protected route."""
        from flask import Flask, jsonify
        from api.auth import require_jwt

        mini = Flask(__name__)

        @mini.route("/protected")
        @require_jwt
        def protected():
            return jsonify({"ok": True})

        return mini

    def test_require_jwt_missing_header(self):
        client = self._make_mini_app().test_client()
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert b"missing token" in resp.data

    def test_require_jwt_bad_token(self):
        client = self._make_mini_app().test_client()
        resp = client.get(
            "/protected",
            headers={"Authorization": "Bearer this.is.garbage"},
        )
        assert resp.status_code == 401
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()


# ===========================================================================
# Integration test fixtures
# ===========================================================================

@pytest.fixture()
def app(tmp_path):
    """
    Flask test client backed by a fresh SQLite temp database.

    Strategy:
    - Point db.database.SQLITE_PATH at a temp file via env var DATABASE_URL
      absence + monkey-patching so no Postgres is involved.
    - Force DEV_MODE=true so /auth/magic-link returns dev_token.
    - Force IS_PRODUCTION=False so cookies use Lax samesite (works without HTTPS).
    - Patch send_magic_link to a no-op.
    """
    db_file = str(tmp_path / "test.db")

    # Patch env/settings before any imports resolve values
    env_overrides = {
        "DEV_MODE": "true",
        "DATABASE_URL": "",          # force SQLite branch
        "ALLOWED_ORIGINS": "http://localhost:5001",
        "JWT_SECRET": "test-secret",
        "JWT_ACCESS_TTL_MINUTES": "15",
        "JWT_REFRESH_TTL_DAYS": "30",
    }

    with patch.dict(os.environ, env_overrides):
        import importlib

        # Re-import settings so DEV_MODE etc. pick up patched env
        import config.settings as settings_mod
        importlib.reload(settings_mod)

        # Re-import db.database so SQLITE_PATH picks up the temp file path
        import db.database as db_mod
        db_mod.SQLITE_PATH = db_file
        db_mod.USE_POSTGRES = False

        # Ensure the DB is initialised with the temp path
        db_mod.init_db()

        # Reload api.auth so it binds to the reloaded settings
        import api.auth as auth_mod
        importlib.reload(auth_mod)

        # Reload api.server last — it imports from both settings and db
        import api.server as server_mod
        importlib.reload(server_mod)

        # Patch send_magic_link on the reloaded server module
        with patch.object(server_mod, "send_magic_link", return_value=True):
            server_mod.app.config["TESTING"] = True
            server_mod.app.config["SERVER_NAME"] = None

            # Override IS_PRODUCTION so cookies work in test client (no HTTPS)
            server_mod.IS_PRODUCTION = False

            yield server_mod.app.test_client()


def _seed_student(email="test@example.com"):
    """Insert a student row directly and return the row dict."""
    import db.database as db_mod
    student = db_mod.get_student_by_email(email)
    if not student:
        student = db_mod.create_student(email)
    return student


def _seed_magic_token(email, token, minutes_from_now=10):
    """Insert a magic token for the given email.

    SQLite compares expires_at as a plain string using its datetime() format
    (``YYYY-MM-DD HH:MM:SS``, no T separator, no timezone offset).  We store
    the timestamp in that same format so the ``expires_at > datetime('now')``
    check in get_and_consume_token behaves correctly.
    """
    import db.database as db_mod

    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    # Use SQLite-compatible format: "YYYY-MM-DD HH:MM:SS" (UTC, no offset)
    expires_at = dt.strftime("%Y-%m-%d %H:%M:%S")
    db_mod.create_magic_token(email, token, expires_at)


# ===========================================================================
# Integration tests
# ===========================================================================

class TestMagicLinkFlow:
    def test_magic_link_flow(self, app):
        """POST /auth/magic-link returns status=sent and a dev_token in dev mode."""
        resp = app.post(
            "/auth/magic-link",
            json={"email": "user@example.com"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "sent"
        # DEV_MODE is true, so dev_token must be present
        assert "dev_token" in data
        assert len(data["dev_token"]) > 10


class TestVerifyEndpoint:
    def test_verify_issues_jwt(self, app):
        """GET /auth/verify with a valid magic token returns access_token + sets cookie."""
        email = "verify@example.com"
        raw_token = "abc123verifytoken"
        _seed_student(email)
        _seed_magic_token(email, raw_token)

        resp = app.get(f"/auth/verify?token={raw_token}")
        assert resp.status_code == 200

        data = resp.get_json()
        assert "access_token" in data
        assert len(data["access_token"]) > 10

        # Refresh cookie should be set
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "ccc_refresh" in cookie_header

    def test_verify_expired_token_redirects(self, app):
        """GET /auth/verify with an expired magic token redirects to /signup?error=expired."""
        email = "expired@example.com"
        raw_token = "expiredtoken999"
        _seed_student(email)
        # Seed with a token that expired in the past
        _seed_magic_token(email, raw_token, minutes_from_now=-5)

        resp = app.get(f"/auth/verify?token={raw_token}")
        # Expect a redirect to /signup?error=expired
        assert resp.status_code in (302, 301)
        assert "expired" in resp.headers.get("Location", "")


class TestRefreshTokenRotation:
    def _do_verify(self, app, email, raw_token):
        _seed_student(email)
        _seed_magic_token(email, raw_token)
        resp = app.get(f"/auth/verify?token={raw_token}")
        assert resp.status_code == 200
        data = resp.get_json()
        # Extract Set-Cookie value for ccc_refresh
        cookies = resp.headers.getlist("Set-Cookie")
        refresh_cookie = None
        for c in cookies:
            if "ccc_refresh" in c:
                # "ccc_refresh=<value>; Path=..."
                refresh_cookie = c.split(";")[0].split("=", 1)[1]
                break
        return data["access_token"], refresh_cookie

    def test_refresh_token_rotation(self, app):
        """POST /auth/refresh rotates the token and returns a new access_token."""
        access_token, refresh_cookie = self._do_verify(
            app, "rotate@example.com", "rotatetoken123"
        )

        resp = app.post(
            "/auth/refresh",
            headers={"Cookie": f"ccc_refresh={refresh_cookie}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "access_token" in data
        # New access token should be different (different iat/exp)
        # (theoretically could be same within same second, but in practice won't be)
        assert data["access_token"] is not None

        # Old refresh token should now be revoked
        import db.database as db_mod
        old_row = db_mod.get_refresh_token(refresh_cookie)
        assert old_row is not None
        assert old_row["revoked_at"] is not None

        # New refresh cookie should be set
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "ccc_refresh" in cookie_header

    def test_refresh_with_revoked_token(self, app):
        """POST /auth/refresh with a revoked token returns 401."""
        _, refresh_cookie = self._do_verify(
            app, "revoked@example.com", "revokedtoken456"
        )

        # Manually revoke it
        import db.database as db_mod
        db_mod.revoke_refresh_token(refresh_cookie)

        resp = app.post(
            "/auth/refresh",
            headers={"Cookie": f"ccc_refresh={refresh_cookie}"},
        )
        assert resp.status_code == 401
        data = resp.get_json()
        assert "revoked" in data.get("error", "").lower()


class TestLogout:
    def test_logout_revokes_token(self, app):
        """POST /auth/logout revokes the refresh token in the DB."""
        email = "logout@example.com"
        raw_token = "logouttoken789"
        _seed_student(email)
        _seed_magic_token(email, raw_token)

        verify_resp = app.get(f"/auth/verify?token={raw_token}")
        cookies = verify_resp.headers.getlist("Set-Cookie")
        refresh_cookie = None
        for c in cookies:
            if "ccc_refresh" in c:
                refresh_cookie = c.split(";")[0].split("=", 1)[1]
                break

        assert refresh_cookie is not None

        resp = app.post(
            "/auth/logout",
            headers={"Cookie": f"ccc_refresh={refresh_cookie}"},
        )
        assert resp.status_code == 200

        import db.database as db_mod
        row = db_mod.get_refresh_token(refresh_cookie)
        assert row is not None
        assert row["revoked_at"] is not None


class TestGetMe:
    def test_get_me_requires_jwt(self, app):
        """GET /api/me without Authorization header returns 401."""
        resp = app.get("/api/me")
        assert resp.status_code == 401

    def test_get_me_with_valid_jwt(self, app):
        """GET /api/me with valid Bearer token returns the student row."""
        email = "getme@example.com"
        student = _seed_student(email)

        from api.auth import make_access_token
        token = make_access_token(student["id"])

        resp = app.get(
            "/api/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["email"] == email
        assert data["id"] == student["id"]
