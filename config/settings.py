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
FROM_NAME      = os.environ.get("FROM_NAME", "inroad")

# ── APIs ────────────────────────────────────────────────────────────────────

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
PDL_API_KEY    = os.environ.get("PDL_API_KEY", "")

# ── Token / session config ──────────────────────────────────────────────────

MAGIC_LINK_EXPIRY_MINUTES = int(os.environ.get("MAGIC_LINK_EXPIRY_MINUTES", "30"))
MAGIC_LINK_RATE_LIMIT     = int(os.environ.get("MAGIC_LINK_RATE_LIMIT", "3"))
MAGIC_LINK_RATE_WINDOW    = int(os.environ.get("MAGIC_LINK_RATE_WINDOW", "10"))  # minutes

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))

JWT_SECRET = os.environ.get("JWT_SECRET", "change-jwt-secret-in-production")
JWT_ACCESS_TTL_MINUTES = int(os.environ.get("JWT_ACCESS_TTL_MINUTES", "15"))
JWT_REFRESH_TTL_DAYS = int(os.environ.get("JWT_REFRESH_TTL_DAYS", "30"))

# ── CORS ────────────────────────────────────────────────────────────────────

# Comma-separated list of allowed origins, e.g. https://coffeechatconnect.com
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5001,http://127.0.0.1:5001").split(",")
    if o.strip()
]

# ── Feature flags ───────────────────────────────────────────────────────────

DEV_MODE = os.environ.get("DEV_MODE", "false").lower() == "true"

# ── Pipeline ─────────────────────────────────────────────────────────────────

DAILY_MATCH_QUOTA   = int(os.environ.get("DAILY_MATCH_QUOTA",   "3"))
CLOSING_SOON_DAYS   = int(os.environ.get("CLOSING_SOON_DAYS",   "7"))
FRESHNESS_DECAY_DAYS = int(os.environ.get("FRESHNESS_DECAY_DAYS", "30"))

# Path to the SQLite database (ignored when DATABASE_URL is set)
DB_PATH = os.environ.get("DB_PATH", "")

# ── Scraper / pipeline constants ──────────────────────────────────────────────

REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "1.0"))
REQUEST_TIMEOUT       = int(os.environ.get("REQUEST_TIMEOUT", "15"))
MAX_RETRIES           = int(os.environ.get("MAX_RETRIES", "3"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

SENIORITY_KEYWORDS = {
    "intern":     ["intern", "internship", "placement", "summer analyst", "spring week"],
    "junior":     ["junior", "graduate", "grad", "entry level", "new grad", "trainee"],
    "mid":        ["analyst", "specialist", "engineer", "consultant", "advisor"],
    "senior":     ["senior", "lead", "principal", "staff", "experienced"],
    "leadership": ["manager", "director", "vp", "vice president", "head of", "partner",
                   "managing director", "md", "chief"],
}

INDUSTRIES = [
    "Finance", "Investment Banking", "Technology", "Software Engineering",
    "Product Management", "Consulting", "Strategy", "Marketing", "Growth",
    "Law", "Healthcare", "Media & Journalism", "Design & UX",
    "Data & Analytics", "Real Estate", "Non-profit & Policy",
    "Venture Capital", "Other",
]
