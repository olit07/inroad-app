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
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "contact@the-inroad.com")
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

# ── Lead pre-fetch: company size lookup ──────────────────────────────────────

COMPANY_SIZE_LOOKUP = {
    # large
    "goldman sachs": "large", "jp morgan": "large", "jpmorgan": "large",
    "morgan stanley": "large", "barclays": "large", "hsbc": "large",
    "blackrock": "large", "ubs": "large", "deutsche bank": "large",
    "citi": "large", "citigroup": "large", "citibank": "large",
    "bank of america": "large", "bnp paribas": "large",
    "société générale": "large", "societe generale": "large",
    "nomura": "large", "macquarie": "large", "credit suisse": "large",
    "lazard": "mid", "rothschild": "mid", "rothschild & co": "mid",
    "evercore": "mid", "moelis": "mid", "moelis & company": "mid",
    "pjt partners": "mid",
    "google": "large", "amazon": "large", "microsoft": "large",
    "meta": "large", "apple": "large", "salesforce": "large",
    "two sigma": "mid", "citadel": "large", "citadel securities": "large",
    "jane street": "large", "bloomberg": "large", "databricks": "mid",
    "snowflake": "mid", "palantir": "mid",
    "deloitte": "large", "pwc": "large", "kpmg": "large", "ey": "large",
    "ernst & young": "large",
    "mckinsey": "large", "mckinsey & company": "large",
    "bcg": "large", "boston consulting group": "large",
    "bain": "large", "bain & company": "large",
    "clifford chance": "large", "freshfields": "large",
    "freshfields bruckhaus deringer": "large",
    "linklaters": "large", "allen & overy": "large", "a&o": "large",
    "slaughter and may": "large",
    "herbert smith freehills": "large", "hsf": "large",
    "hogan lovells": "large", "baker mckenzie": "large",
    "latham & watkins": "large", "sullivan & cromwell": "large",
    "kirkland & ellis": "large",
    # mid
    "revolut": "mid", "monzo": "mid", "wise": "mid", "stripe": "mid",
    "deepmind": "mid",
    # startup
}

# ── Lead pre-fetch: department → search keywords map ────────────────────────

DEPT_MAP = {
    "investment_banking": ["investment banking", "M&A", "analyst", "associate", "vice president"],
    "sales_trading":      ["sales trading", "trading", "trader", "analyst", "associate"],
    "asset_management":   ["asset management", "portfolio manager", "analyst", "associate"],
    "equity_research":    ["equity research", "research analyst", "analyst", "associate"],
    "risk":               ["risk", "risk analyst", "risk manager", "analyst", "associate"],
    "quant":              ["quantitative analyst", "quant", "quantitative researcher", "structurer"],
    "software_engineering": ["software engineer", "engineer", "developer", "swe"],
    "product":            ["product manager", "product analyst", "PM"],
    "data_ml":            ["data scientist", "machine learning engineer", "data analyst", "ML engineer"],
    "infrastructure":     ["infrastructure engineer", "devops", "platform engineer", "SRE"],
    "design":             ["product designer", "UX designer", "designer"],
    "law_corporate":      ["trainee", "associate", "partner", "solicitor", "M&A"],
    "law_finance":        ["trainee", "associate", "partner", "solicitor", "finance"],
    "law_disputes":       ["trainee", "associate", "partner", "solicitor", "litigation"],
    "law_tech":           ["trainee", "associate", "solicitor", "technology", "IP"],
}

# Maps Trackr industry → relevant DEPT_MAP keys
INDUSTRY_DEPT_MAP = {
    "Finance":            ["investment_banking", "sales_trading", "asset_management",
                           "equity_research", "risk", "quant"],
    "Investment Banking": ["investment_banking", "sales_trading", "risk", "quant"],
    "Technology":         ["software_engineering", "product", "data_ml", "infrastructure", "design"],
    "Software Engineering": ["software_engineering", "product", "data_ml"],
    "Data & Analytics":   ["data_ml", "software_engineering"],
    "Law":                ["law_corporate", "law_finance", "law_disputes", "law_tech"],
}

# Fallback city per region if job has no location field
REGION_LOCATION_FALLBACK = {"UK": "London", "US": "New York", "EU": "Paris"}
