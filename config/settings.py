"""
config/settings.py
All configuration read from environment variables.
Copy .env.example to .env for local dev.
"""

import os

# ── Core ────────────────────────────────────────────────────────────────────

# Set this to your Railway URL after first deploy, e.g. https://ccc.up.railway.app
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5001").rstrip("/")

SESSION_SECRET = os.environ.get(
    "SESSION_SECRET",
    "change-me-in-production-use-a-long-random-string"
)

# ── Email (Resend / SMTP) ───────────────────────────────────────────────────

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.resend.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER      = os.environ.get("SMTP_USER", "resend")
SMTP_PASS      = os.environ.get("SMTP_PASS", "")
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
FROM_NAME      = os.environ.get("FROM_NAME", "Coffee Chat Connect")

# ── APIs ────────────────────────────────────────────────────────────────────

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# ── Token / session config ──────────────────────────────────────────────────

MAGIC_LINK_EXPIRY_MINUTES = int(os.environ.get("MAGIC_LINK_EXPIRY_MINUTES", "30"))
MAGIC_LINK_RATE_LIMIT     = int(os.environ.get("MAGIC_LINK_RATE_LIMIT", "3"))
MAGIC_LINK_RATE_WINDOW    = int(os.environ.get("MAGIC_LINK_RATE_WINDOW", "10"))  # minutes

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))

# ── CORS ────────────────────────────────────────────────────────────────────

# Comma-separated list of allowed origins, e.g. https://coffeechatconnect.com
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5001,http://127.0.0.1:5001").split(",")
    if o.strip()
]

# ── Feature flags ───────────────────────────────────────────────────────────

DEV_MODE = os.environ.get("DEV_MODE", "false").lower() == "true"
