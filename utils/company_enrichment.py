"""
inroad — Company Enrichment

Enriches companies table with domain, size_band, sector, hq_city, hq_country.
Sources:
  1. Hardcoded known-companies dict (fast, no API)
  2. Clearbit Logo API (free, just for domain confirmation)
  3. Pattern inference from job data
"""
import re
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH
from db.database import db_conn

logger = logging.getLogger(__name__)

# ── Known company data ────────────────────────────────────────────────────────
# (domain, size_band, sector, hq_city, hq_country)
KNOWN_COMPANIES: dict[str, tuple] = {
    # Finance
    "Goldman Sachs":         ("goldmansachs.com",    "large",  "Investment Banking", "New York",    "US"),
    "JPMorgan Chase":        ("jpmorgan.com",         "large",  "Investment Banking", "New York",    "US"),
    "Morgan Stanley":        ("morganstanley.com",    "large",  "Investment Banking", "New York",    "US"),
    "Barclays":              ("barclays.com",         "large",  "Investment Banking", "London",      "UK"),
    "Deutsche Bank":         ("db.com",               "large",  "Investment Banking", "Frankfurt",   "DE"),
    "UBS":                   ("ubs.com",              "large",  "Finance",            "Zurich",      "CH"),
    "Credit Suisse":         ("credit-suisse.com",    "large",  "Finance",            "Zurich",      "CH"),
    "HSBC":                  ("hsbc.com",             "large",  "Finance",            "London",      "UK"),
    "BlackRock":             ("blackrock.com",        "large",  "Finance",            "New York",    "US"),
    "Citadel":               ("citadel.com",          "large",  "Finance",            "Chicago",     "US"),
    "Two Sigma":             ("twosigma.com",         "mid",    "Finance",            "New York",    "US"),
    "Jane Street":           ("janestreet.com",       "mid",    "Finance",            "New York",    "US"),
    "Jefferies":             ("jefferies.com",        "large",  "Investment Banking", "New York",    "US"),
    "Platinum Peak Holdings":("platinumpeakholdings.com","startup","Finance",        "London",      "UK"),
    # Consulting
    "McKinsey & Company":    ("mckinsey.com",         "large",  "Consulting",         "New York",    "US"),
    "Boston Consulting Group":("bcg.com",             "large",  "Consulting",         "Boston",      "US"),
    "Bain & Company":        ("bain.com",             "large",  "Consulting",         "Boston",      "US"),
    "Oliver Wyman":          ("oliverwyman.com",      "mid",    "Consulting",         "New York",    "US"),
    "Deloitte":              ("deloitte.com",         "large",  "Consulting",         "London",      "UK"),
    "KPMG":                  ("kpmg.com",             "large",  "Consulting",         "London",      "UK"),
    "EY":                    ("ey.com",               "large",  "Consulting",         "London",      "UK"),
    "PwC":                   ("pwc.com",              "large",  "Consulting",         "London",      "UK"),
    "Accenture":             ("accenture.com",        "large",  "Consulting",         "Dublin",      "IE"),
    "L.E.K. Consulting":     ("lek.com",              "mid",    "Consulting",         "London",      "UK"),
    # Technology
    "Stripe":                ("stripe.com",           "large",  "Technology",         "San Francisco","US"),
    "Anthropic":             ("anthropic.com",        "mid",    "Technology",         "San Francisco","US"),
    "OpenAI":                ("openai.com",           "mid",    "Technology",         "San Francisco","US"),
    "Databricks":            ("databricks.com",       "large",  "Technology",         "San Francisco","US"),
    "Figma":                 ("figma.com",            "large",  "Technology",         "San Francisco","US"),
    "Notion":                ("notion.so",            "mid",    "Technology",         "San Francisco","US"),
    "Canva":                 ("canva.com",            "large",  "Technology",         "Sydney",      "AU"),
    "Palantir":              ("palantir.com",         "large",  "Technology",         "Denver",      "US"),
    "Scale AI":              ("scale.com",            "mid",    "Technology",         "San Francisco","US"),
    # UK Fintech
    "Monzo":                 ("monzo.com",            "mid",    "Technology",         "London",      "UK"),
    "Revolut":               ("revolut.com",          "large",  "Technology",         "London",      "UK"),
    "Wise":                  ("wise.com",             "large",  "Technology",         "London",      "UK"),
    # Law
    "Clifford Chance":       ("cliffordchance.com",   "large",  "Law",                "London",      "UK"),
    "Linklaters":            ("linklaters.com",       "large",  "Law",                "London",      "UK"),
    "Allen & Overy":         ("allenovery.com",       "large",  "Law",                "London",      "UK"),
    "Freshfields":           ("freshfields.com",      "large",  "Law",                "London",      "UK"),
    "Slaughter and May":     ("slaughterandmay.com",  "large",  "Law",                "London",      "UK"),
    # VC
    "Sequoia Capital":       ("sequoiacap.com",       "mid",    "Venture Capital",    "Menlo Park",  "US"),
    "Index Ventures":        ("indexventures.com",    "mid",    "Venture Capital",    "London",      "UK"),
    "Andreessen Horowitz":   ("a16z.com",             "mid",    "Venture Capital",    "Menlo Park",  "US"),
    "General Catalyst":      ("generalcatalyst.com",  "mid",    "Venture Capital",    "Cambridge",   "US"),
    # Healthcare
    "AstraZeneca":           ("astrazeneca.com",      "large",  "Healthcare",         "Cambridge",   "UK"),
    "GlaxoSmithKline":       ("gsk.com",              "large",  "Healthcare",         "London",      "UK"),
    "Pfizer":                ("pfizer.com",           "large",  "Healthcare",         "New York",    "US"),
    # Media
    "Vox Media":             ("voxmedia.com",         "mid",    "Media & Journalism", "New York",    "US"),
    "Axios":                 ("axios.com",            "mid",    "Media & Journalism", "Arlington",   "US"),
    # Energy
    "E.ON":                  ("eon.com",              "large",  "Finance",            "Essen",       "DE"),
    "Shell":                 ("shell.com",            "large",  "Other",              "London",      "UK"),
    # Other
    "Unilever":              ("unilever.com",         "large",  "Marketing",          "London",      "UK"),
    "ByteDance":             ("bytedance.com",        "large",  "Technology",         "Beijing",     "CN"),
}


def get_company_data(company_name: str) -> dict | None:
    """Look up known company data. Returns dict or None."""
    for name, data in KNOWN_COMPANIES.items():
        if name.lower() == company_name.lower() or name.lower() in company_name.lower():
            return {
                "name":       name,
                "domain":     data[0],
                "size_band":  data[1],
                "sector":     data[2],
                "hq_city":    data[3],
                "hq_country": data[4],
            }
    return None


def infer_domain_from_name(company_name: str) -> str:
    """Best-guess domain from company name."""
    slug = re.sub(r"[^a-z0-9]", "", company_name.lower().strip())
    return f"{slug}.com" if slug else ""


def enrich_companies(db_path=DB_PATH) -> dict:
    """
    Enrich all companies in the jobs table.
    Upserts into companies table with known data.
    Returns summary.
    """
    enriched = 0
    skipped  = 0

    with db_conn(db_path) as conn:
        company_names = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT company_name FROM jobs WHERE is_active=1"
            ).fetchall()
        ]

    for company_name in company_names:
        data = get_company_data(company_name)
        domain = data["domain"] if data else infer_domain_from_name(company_name)
        size   = data["size_band"] if data else "mid"
        sector = data["sector"] if data else ""
        city   = data["hq_city"] if data else ""
        country= data["hq_country"] if data else ""

        with db_conn(db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM companies WHERE name=?", (company_name,)
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE companies SET domain=?, size_band=?, sector=?,
                       hq_city=?, hq_country=? WHERE name=?""",
                    (domain, size, sector, city, country, company_name)
                )
                skipped += 1
            else:
                conn.execute(
                    """INSERT INTO companies (name, domain, size_band, sector, hq_city, hq_country)
                       VALUES (?,?,?,?,?,?)""",
                    (company_name, domain, size, sector, city, country)
                )
                enriched += 1

    logger.info(f"Company enrichment: {enriched} new, {skipped} updated, {len(company_names)} total")
    return {"enriched": enriched, "updated": skipped, "total": len(company_names)}


def get_company_size_for_student(company_name: str, db_path=DB_PATH) -> str:
    """Return size_band for a company, checking DB first then known dict."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT size_band FROM companies WHERE name=?", (company_name,)
        ).fetchone()
        if row and row[0]:
            return row[0]

    data = get_company_data(company_name)
    return data["size_band"] if data else "mid"
