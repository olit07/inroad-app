#!/usr/bin/env python3
"""
inroad — First-run setup script

Run this once after cloning the project:
  python3 setup.py

Does:
1. Creates the SQLite database and all tables
2. Seeds the ats_targets table with 80+ known companies
3. Validates environment variables
4. Prints a checklist of what needs API keys
"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config.settings import DB_PATH
from db.database import init_db, db_conn
from pipeline.profile_cache import init_cache

# ── ANSI colours ─────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[94m"; RST = "\033[0m"
def ok(s):   print(f"  {G}✓{RST}  {s}")
def warn(s): print(f"  {Y}○{RST}  {s}")
def err(s):  print(f"  {R}✗{RST}  {s}")
def hdr(s):  print(f"\n{B}{'─'*55}{RST}\n  {s}\n{B}{'─'*55}{RST}")

# ── ATS targets ───────────────────────────────────────────────────────────────
GREENHOUSE_TARGETS = [
    # Finance
    ("Citadel",              "citadel"),
    ("Two Sigma",            "twosigma"),
    ("Jane Street",          "janestreet"),
    ("Virtu Financial",      "virtu"),
    ("Bridgewater",          "bridgewater"),
    # Big Tech
    ("Stripe",               "stripe"),
    ("Airbnb",               "airbnb"),
    ("Databricks",           "databricks"),
    ("Figma",                "figma"),
    ("Notion",               "notion"),
    ("Airtable",             "airtable"),
    ("Asana",                "asana"),
    ("Plaid",                "plaid"),
    ("Robinhood",            "robinhood"),
    ("Coinbase",             "coinbase"),
    ("Scale AI",             "scaleai"),
    ("Anthropic",            "anthropic"),
    ("OpenAI",               "openai"),
    ("Cohere",               "cohere"),
    # Consulting
    ("Oliver Wyman",         "oliverwyman"),
    ("L.E.K. Consulting",    "lek"),
    # Growth
    ("HubSpot",              "hubspot"),
    ("Zendesk",              "zendesk"),
    ("Intercom",             "intercom"),
    ("Klaviyo",              "klaviyo"),
    # Design
    ("Miro",                 "miro"),
    # VC
    ("Andreessen Horowitz",  "a16z"),
    ("Accel",                "accel"),
    # Healthcare
    ("Tempus",               "tempus"),
    ("Ro",                   "ro"),
    # Media
    ("Vox Media",            "voxmedia"),
    ("Substack",             "substack"),
    # UK Fintech
    ("Wise",                 "wise"),
    ("Revolut",              "revolut"),
    ("Monzo",                "monzo"),
    ("OakNorth",             "oaknorth"),
    # US Tech
    ("Ramp",                 "ramp"),
    ("Linear",               "linear"),
    ("Vercel",               "vercel"),
    ("Coda",                 "coda"),
    # Consulting
    ("Korn Ferry",           "kornferry"),
    # VC
    ("General Atlantic",     "generalatlantic"),
    ("Coatue",               "coatue"),
    ("Lightspeed",           "lightspeedvp"),
    # Healthcare
    ("Lyra Health",          "lyrahealth"),
    ("Hinge Health",         "hingehealth"),
    # Media
    ("The Athletic",         "theathletic"),
    ("Axios",                "axios"),
]

LEVER_TARGETS = [
    # Quant / Finance
    ("Point72",              "point72"),
    ("D.E. Shaw",            "deshaw"),
    ("Millennium",           "millennium"),
    ("Schonfeld",            "schonfeld"),
    # Tech
    ("Canva",                "canva"),
    ("Rippling",             "rippling"),
    ("Carta",                "carta"),
    ("Mercury",              "mercury"),
    ("Retool",               "retool"),
    ("Amplitude",            "amplitude"),
    ("Mixpanel",             "mixpanel"),
    # Consulting
    ("Bain & Company",       "bain"),
    ("Booz Allen Hamilton",  "boozallen"),
    # Growth
    ("Iterable",             "iterable"),
    # VC
    ("General Catalyst",     "generalcatalyst"),
    ("Bessemer",             "bvp"),
    # Healthcare
    ("Oscar Health",         "oscar"),
    # Non-profit
    ("Code for America",     "codeforamerica"),
    ("Chan Zuckerberg",      "chanzuckerberg"),
    # Real Estate
    ("Opendoor",             "opendoor"),
    ("Compass",              "compass"),
    # Climate
    ("Watershed",            "watershed"),
    ("Pachama",              "pachama"),
    # Legaltech
    ("Clio",                 "clio"),
    # Fintech
    ("Unit",                 "unit"),
    ("Moov",                 "moov"),
    # EdTech
    ("Maven",                "maven"),
]


def seed_ats_targets():
    with db_conn(DB_PATH) as conn:
        for company, token in GREENHOUSE_TARGETS:
            conn.execute(
                """INSERT OR IGNORE INTO ats_targets
                   (company_name, ats_type, board_token) VALUES (?,?,?)""",
                (company, "greenhouse", token)
            )
        for company, token in LEVER_TARGETS:
            conn.execute(
                """INSERT OR IGNORE INTO ats_targets
                   (company_name, ats_type, board_token) VALUES (?,?,?)""",
                (company, "lever", token)
            )
        total = conn.execute("SELECT COUNT(*) FROM ats_targets").fetchone()[0]
    return total


def check_env():
    checks = [
        ("ANTHROPIC_API_KEY",   "Claude email drafts",    True),
        ("SERPER_API_KEY",      "LinkedIn matching",      True),
        ("SERPAPI_KEY",         "LinkedIn matching (alt)", False),
        ("REED_API_KEY",        "Reed.co.uk jobs",         False),
        ("ADZUNA_APP_ID",       "Adzuna jobs",             False),
        ("ADZUNA_APP_KEY",      "Adzuna jobs",             False),
        ("TRACKR_SESSION_COOKIE", "Bristol Trackr jobs",   False),
        ("HUNTER_API_KEY",      "Email verification",      False),
    ]
    has_search = bool(os.environ.get("BING_SEARCH_API_KEY") or os.environ.get("SERPAPI_KEY"))
    results = {}
    for var, desc, critical in checks:
        val = os.environ.get(var, "")
        results[var] = {"set": bool(val), "desc": desc, "critical": critical}
    return results, has_search


def write_env_template():
    template = """# inroad Environment Variables
# Copy this to .env and fill in your keys
# Then run: source .env

# ── REQUIRED for full functionality ───────────────────────────────
export ANTHROPIC_API_KEY=sk-ant-...         # Claude email generation
export BING_SEARCH_API_KEY=...              # LinkedIn profile search (Azure free tier)

# ── OPTIONAL: alternative to Bing ─────────────────────────────────
# export SERPAPI_KEY=...                    # SerpAPI (alternative search)

# ── OPTIONAL: more job sources ────────────────────────────────────
# export REED_API_KEY=...                   # Reed.co.uk developer API (free)
# export ADZUNA_APP_ID=...                  # Adzuna developer API (free)
# export ADZUNA_APP_KEY=...                 # Adzuna developer API (free)
# export TRACKR_SESSION_COOKIE=...          # Bristol Trackr session cookie

# ── OPTIONAL: email verification ──────────────────────────────────
# export HUNTER_API_KEY=...                 # Hunter.io (free tier: 25/mo)
"""
    env_path = Path(__file__).parent / ".env.template"
    env_path.write_text(template)
    return env_path


if __name__ == "__main__":
    print(f"\n{'═'*55}")
    print(f"  inroad Setup")
    print(f"{'═'*55}")

    hdr("1. Database")
    init_db(DB_PATH)
    ok(f"Schema created at {DB_PATH}")
    init_cache(DB_PATH)
    ok("Profile cache table ready")

    hdr("2. ATS targets")
    n = seed_ats_targets()
    ok(f"{n} ATS company targets seeded (Greenhouse + Lever)")

    hdr("3. Environment variables")
    env_results, has_search = check_env()
    for var, info in env_results.items():
        if info["set"]:
            ok(f"{var:<30} {info['desc']}")
        elif info["critical"]:
            warn(f"{var:<30} NOT SET — {info['desc']} disabled")
        else:
            print(f"  {'  ':<3}{var:<30} optional ({info['desc']})")

    hdr("4. .env template")
    env_path = write_env_template()
    ok(f"Template written to {env_path}")
    print(f"       Edit it and run: source {env_path}")

    hdr("5. Quick start")
    print(f"  python3 cli.py scrape --source greenhouse_feed   # pull jobs (no keys needed)")
    print(f"  python3 cli.py scrape --source lever_feed        # pull more jobs")
    print(f"  python3 cli.py stats                             # check DB")
    print(f"  python3 api/server.py                            # start API on :5001")
    print(f"  python3 scheduler/run.py                         # start daily scheduler")
    print()

    if not has_search:
        print(f"  {Y}⚠  No search API key — LinkedIn matching disabled.")
        print(f"     Get a free Bing Search key at portal.azure.com{RST}")
    if not env_results["ANTHROPIC_API_KEY"]["set"]:
        print(f"  {Y}⚠  No ANTHROPIC_API_KEY — using template emails (still works).{RST}")

    print(f"\n  {G}Setup complete.{RST} Open inroad-admin.html in your browser.\n")
