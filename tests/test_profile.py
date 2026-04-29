"""
tests/test_profile.py
Profile endpoint integration tests — PATCH /api/me and DELETE /api/me.
"""

import os
import sys
import pytest

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Shared fixture (mirrors the one in test_auth.py — kept local so each
# test module is independently runnable)
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    """
    Flask test client backed by a fresh SQLite temp database.
    DEV_MODE=true, IS_PRODUCTION=False, send_magic_link no-op.
    """
    db_file = str(tmp_path / "test_profile.db")

    env_overrides = {
        "DEV_MODE": "true",
        "DATABASE_URL": "",
        "ALLOWED_ORIGINS": "http://localhost:5001",
        "JWT_SECRET": "test-secret",
        "JWT_ACCESS_TTL_MINUTES": "15",
        "JWT_REFRESH_TTL_DAYS": "30",
    }

    with patch.dict(os.environ, env_overrides):
        import importlib

        import config.settings as settings_mod
        importlib.reload(settings_mod)

        import db.database as db_mod
        db_mod.SQLITE_PATH = db_file
        db_mod.USE_POSTGRES = False
        db_mod.init_db()

        import api.auth as auth_mod
        importlib.reload(auth_mod)

        import api.server as server_mod
        importlib.reload(server_mod)

        with patch.object(server_mod, "send_magic_link", return_value=True):
            server_mod.app.config["TESTING"] = True
            server_mod.IS_PRODUCTION = False

            yield server_mod.app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_student(email="profile@example.com", name=None):
    import db.database as db_mod
    student = db_mod.get_student_by_email(email)
    if not student:
        student = db_mod.create_student(email)
    if name:
        db_mod.update_student_fields(student["id"], {"name": name})
        student = db_mod.get_student_by_id(student["id"])
    return student


def _make_token(student_id):
    from api.auth import make_access_token
    return make_access_token(student_id)


def _seed_refresh_token(student_id):
    import db.database as db_mod
    from api.auth import make_refresh_token_str
    token_str = make_refresh_token_str()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=30)
    ).isoformat()
    db_mod.create_refresh_token(student_id, token_str, expires_at)
    return token_str


# ===========================================================================
# PATCH /api/me
# ===========================================================================

class TestPatchMe:
    def test_patch_me_updates_name(self, app):
        """PATCH /api/me with {"name": "Bob"} returns the student with name updated."""
        student = _seed_student("patchname@example.com")
        token = _make_token(student["id"])

        resp = app.patch(
            "/api/me",
            json={"name": "Bob"},
            headers={"Authorization": f"Bearer {token}"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Bob"
        assert data["email"] == "patchname@example.com"

    def test_patch_me_partial_update(self, app):
        """Only supplied fields are changed; other fields remain unchanged."""
        student = _seed_student("partial@example.com", name="Alice")
        token = _make_token(student["id"])

        # Patch only the bio; name should remain "Alice"
        resp = app.patch(
            "/api/me",
            json={"bio": "My bio"},
            headers={"Authorization": f"Bearer {token}"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["bio"] == "My bio"
        assert data["name"] == "Alice"  # unchanged

    def test_patch_me_requires_jwt(self, app):
        """PATCH /api/me without Authorization header returns 401."""
        resp = app.patch(
            "/api/me",
            json={"name": "Unauthorised"},
            content_type="application/json",
        )
        assert resp.status_code == 401

    def test_patch_me_multiple_fields(self, app):
        """PATCH /api/me with multiple fields updates all of them."""
        student = _seed_student("multifield@example.com")
        token = _make_token(student["id"])

        resp = app.patch(
            "/api/me",
            json={"name": "Charlie", "bio": "Hello", "university": "Oxford"},
            headers={"Authorization": f"Bearer {token}"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Charlie"
        assert data["bio"] == "Hello"
        assert data["university"] == "Oxford"


# ===========================================================================
# DELETE /api/me
# ===========================================================================

class TestDeleteMe:
    def test_delete_me_deactivates(self, app):
        """DELETE /api/me returns {"status": "deactivated"} and sets deactivated_at."""
        student = _seed_student("deactivate@example.com")
        token = _make_token(student["id"])

        resp = app.delete(
            "/api/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "deactivated"

        # Student row should have deactivated_at set
        import db.database as db_mod
        updated = db_mod.get_student_by_id(student["id"])
        assert updated["deactivated_at"] is not None

    def test_delete_me_revokes_tokens(self, app):
        """After DELETE /api/me the refresh token no longer works."""
        student = _seed_student("revokeall@example.com")
        refresh_token_str = _seed_refresh_token(student["id"])
        access_token = _make_token(student["id"])

        # Delete the account
        del_resp = app.delete(
            "/api/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert del_resp.status_code == 200

        # Attempting to refresh should now return 401
        refresh_resp = app.post(
            "/auth/refresh",
            headers={"Cookie": f"inroad_refresh={refresh_token_str}"},
        )
        assert refresh_resp.status_code == 401

    def test_delete_me_requires_jwt(self, app):
        """DELETE /api/me without Authorization header returns 401."""
        resp = app.delete("/api/me")
        assert resp.status_code == 401
