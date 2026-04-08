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

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

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
    # ── Bulge bracket banks ───────────────────────────────────────────────────
    "goldman sachs": "large", "jp morgan": "large", "jpmorgan": "large",
    "j.p. morgan": "large", "morgan stanley": "large", "barclays": "large",
    "hsbc": "large", "blackrock": "large", "ubs": "large",
    "deutsche bank": "large", "citi": "large", "citigroup": "large",
    "citibank": "large", "bank of america": "large", "bnp paribas": "large",
    "société générale": "large", "societe generale": "large",
    "nomura": "large", "macquarie": "large", "macquarie group": "large",
    "credit suisse": "large", "mufg": "large", "smbc": "large",
    "mizuho": "large", "santander": "large", "rabobank": "large",
    "standard chartered": "large", "lloyds banking group": "large",
    "natwest markets": "large", "natwest": "large",
    "bank of england": "large", "commerzbank": "large",
    "ing": "large", "abn amro": "large", "intesa sanpaolo": "large",
    "crédit agricole": "large", "credit agricole": "large",
    "natixis": "large", "bbva": "large", "scotiabank": "large",
    "bmo capital markets": "large", "rbc capital markets": "large",
    "cibc": "large", "td securities": "large", "td cowen": "large",
    "btg pactual": "large", "icbc standard bank": "large",
    "bank of china": "large", "cicc": "large", "citic clsa": "large",
    # ── Elite boutiques ───────────────────────────────────────────────────────
    "lazard": "mid", "lazard asset management": "mid",
    "rothschild": "mid", "rothschild & co": "mid", "rothschild & co.": "mid",
    "evercore": "mid", "moelis": "mid", "moelis & company": "mid",
    "moelis & co": "mid", "pjt partners": "mid",
    "centerview partners": "mid", "perella weinberg partners": "mid",
    "greenhill": "mid", "houlihan lokey": "mid",
    "jefferies": "mid", "lincoln international": "mid",
    "baird": "mid", "william blair": "mid", "stifel": "mid",
    "raymond james": "mid", "stephens": "mid",
    "piper sandler": "mid", "canaccord genuity": "mid",
    "oppenheimer & co": "mid", "wedbush securities": "mid",
    "harris williams": "mid", "dc advisory": "mid",
    "raine group": "mid", "liontree": "mid", "torch partners": "mid",
    "arma partners": "mid", "fenchurch advisory": "mid",
    "gleacher shacklock": "mid", "hannam & partners": "mid",
    "panmure liberum": "mid", "peel hunt": "mid", "berenberg": "mid",
    "numis": "mid", "investec": "mid",
    "stout": "mid", "stax": "mid", "solomon partners": "mid",
    # ── Asset management ──────────────────────────────────────────────────────
    "bridgewater associates": "large", "man group": "large",
    "wellington management": "large", "t. rowe price": "large",
    "t rowe price": "large", "fidelity investments": "large",
    "fidelity international": "large",
    "franklin templeton": "large", "vanguard": "large",
    "invesco": "large", "schroders": "large", "aberdeen group": "large",
    "abrdn": "large", "columbia threadneedle investments": "large",
    "pgim": "large", "pimco": "large", "nuveen": "large",
    "legal & general": "large", "aviva investors": "large",
    "axa investment managers": "large", "dws": "large",
    "m&g": "large", "neuberger berman": "large",
    "alliancebernstein": "large", "principal asset management": "large",
    "state street": "large", "bny mellon": "large",
    "northern trust": "large", "charles schwab": "large",
    "blackstone": "large", "kkr": "large", "carlyle": "large",
    "the carlyle group": "large", "apollo global management": "large",
    "ares management": "large", "tpg": "large",
    "brookfield": "large", "hamilton lane": "large",
    "harbourvest": "mid", "stepstone group": "mid",
    "partners group": "large", "ardian": "large",
    "pantheon capital management": "mid",
    "cambridge associates": "mid", "gresham investment management": "mid",
    "impax asset management": "mid", "ninety one": "mid",
    "sands capital": "mid", "brown advisory": "mid",
    "orbis": "mid", "dodge & cox": "mid", "harris associates": "mid",
    "dimensional fund advisors": "mid", "mfs": "mid",
    "putnam": "mid", "tcw": "mid", "robeco": "mid",
    "pictet asset management": "mid", "julius baer": "mid",
    "balyasny asset management": "mid", "marshall wace": "mid",
    "bluecrest capital management": "mid", "brevan howard": "mid",
    "rokos capital management": "mid", "capula investment management": "mid",
    "gsa capital": "mid", "g-research": "mid", "quadrature": "mid",
    "point72": "mid", "millennium management": "large",
    "schonfeld": "mid", "exoduspoint": "mid",
    "sculptor capital management": "mid", "soros fund management": "mid",
    "tudor group": "mid", "caxton associates": "mid",
    "aqr capital management": "mid", "two sigma": "mid",
    "citadel": "large", "citadel securities": "large",
    "jane street": "large", "optiver": "large", "flow traders": "mid",
    "imc trading": "mid", "akuna capital": "mid", "drw": "mid",
    "jump trading": "mid", "da vinci trading": "startup",
    "tower research capital": "mid", "hudson river trading": "mid",
    "virtu financial": "mid", "susquehanna international group": "large",
    "xtz markets": "mid", "xtx markets": "mid", "qube rt": "mid",
    "wintermute": "startup", "b2c2": "startup", "flowdesk": "startup",
    "marvel": "mid",  # note: Marex
    "marex": "mid", "marex spectron": "mid",
    "mckinsey": "large", "mckinsey & company": "large",
    # ── Private equity ────────────────────────────────────────────────────────
    "bain capital": "large", "tpg capital": "large",
    "advent international": "mid", "cinven": "mid",
    "cvc": "mid", "apax partners": "mid",
    "warburg pincus": "large", "general atlantic": "mid",
    "vista equity partners": "mid", "francisco partners": "mid",
    "silver lake": "large", "thoma bravo": "mid",
    "insight partners": "large", "tiger global": "mid",
    "summit partners": "mid", "llr partners": "mid",
    "gtcr": "mid", "roark capital group": "mid",
    "h.i.g. capital": "mid", "k1 investment management": "mid",
    "cd&r": "mid", "stone point capital": "mid",
    "towerbrook capital partners": "mid", "bridgepoint": "mid",
    "nordic capital": "mid", "triton": "mid",
    "ik partners": "mid", "equistone": "mid",
    "waterland private equity": "mid", "pai partners": "mid",
    "cerberus capital management": "large",
    "oaktree capital management": "large", "blue owl capital": "large",
    "sixth street": "mid", "psg equity": "mid",
    "eurazeo": "large", "tikehau capital": "mid",
    "hayfin capital management": "mid", "kartesia": "startup",
    "antin infrastructure partners": "mid", "infrared capital partners": "mid",
    "arcus infrastructure partners": "mid",
    "access holdings": "startup", "access capital partners": "startup",
    "alpine investors": "mid", "audax group": "mid",
    "nonantum capital partners": "startup",
    "17capital": "mid", "lgt capital partners": "mid",
    "mubadala capital": "large", "gic": "large", "temasek": "large",
    "cdpq": "large", "australiansuper": "large", "psp investments": "large",
    "norges bank": "large", "lothian pension fund": "mid",
    "uss": "mid", "lgt": "large", "lgt wealth management": "mid",
    # ── Big 4 / consulting ────────────────────────────────────────────────────
    "deloitte": "large", "pwc": "large", "kpmg": "large", "ey": "large",
    "ernst & young": "large", "ey-parthenon": "large",
    "bcg": "large", "boston consulting group": "large",
    "bain & company": "large", "oliver wyman": "large",
    "roland berger": "large", "arthur d. little": "mid",
    "kearney": "large", "l.e.k. consulting": "mid",
    "simon-kucher": "mid", "oc&c": "mid",
    "pa consulting": "mid", "fp": "mid", "fti consulting": "large",
    "alvarez & marsal": "mid", "alixpartners": "mid",
    "kroll": "mid", "berkley research group": "mid",
    "charles river associates": "mid", "brg": "mid",
    "compass lexecon": "mid", "oxera": "mid", "frontier economics": "mid",
    "cornerstone research": "mid", "the brattle group": "mid",
    "nera": "mid", "analysis group": "mid",
    "turner & townsend": "large", "arcadis": "large",
    "jacobs": "large", "wood mackenzie": "mid",
    "rystad energy": "mid", "ihs markit": "large",
    "gartner": "large", "forrester": "mid",
    "guidehouse": "mid", "protiviti": "large",
    "nextwave consulting": "startup",
    "baringa": "mid", "alpha fmc": "mid",
    "q5 partners": "startup", "strategy&": "large",
    # ── Law firms ─────────────────────────────────────────────────────────────
    "clifford chance": "large", "freshfields": "large",
    "freshfields bruckhaus deringer": "large",
    "linklaters": "large", "allen & overy": "large",
    "a&o shearman": "large", "a&o": "large",
    "slaughter and may": "large", "herbert smith freehills": "large",
    "hsf": "large", "hogan lovells": "large", "baker mckenzie": "large",
    "latham & watkins": "large", "sullivan & cromwell": "large",
    "kirkland & ellis": "large", "white & case": "large",
    "skadden": "large", "skadden arps": "large",
    "norton rose fulbright": "large", "dentons": "large",
    "clyde & co": "large", "cms": "large",
    "ashurst": "large", "eversheds sutherland": "large",
    "bird & bird": "large", "fieldfisher": "mid",
    "simmons & simmons": "mid", "travers smith": "mid",
    "macfarlanes": "mid", "charles russell speechlys": "mid",
    "stephenson harwood": "mid", "osborne clarke": "mid",
    "pinsent masons": "large", "shoosmiths": "mid",
    "browne jacobson": "mid", "weightmans": "mid",
    "squire patton boggs": "large", "mayer brown": "large",
    "paul hastings": "large", "greenberg traurig": "large",
    "fried frank": "mid", "gibson dunn": "large",
    "dechert llp": "large", "goodwin": "large",
    "morgan lewis": "large", "mcguirewoods": "mid",
    "arnold & porter": "large", "bryan cave leighton paisner": "large",
    "addleshaw goddard": "mid", "gowling wlg": "large",
    "burges salmon": "mid", "foot anstey": "mid",
    "trowers & hamlins": "mid", "mills & co": "mid",
    "gateley": "mid", "shakespeare martineau": "mid",
    "womble bond dickinson": "mid", "tlt": "mid",
    "michelmores": "mid", "forsters": "mid",
    "farrer & co": "mid", "payne hicks beach": "startup",
    "withers": "mid", "irwin mitchell": "large",
    "hill dickinson": "mid", "hfw": "mid",
    "watson farley & williams": "mid", "wallker morris": "mid",
    "walker morris": "mid", "dwf": "large",
    "rpc": "mid", "lewis silkin": "mid",
    "lge": "mid", "kennedys": "mid",
    "peters & peters": "startup", "bristows": "mid",
    # ── Tech ──────────────────────────────────────────────────────────────────
    "google": "large", "amazon": "large", "microsoft": "large",
    "meta": "large", "apple": "large", "salesforce": "large",
    "bloomberg": "large", "databricks": "mid", "snowflake": "mid",
    "palantir": "mid", "amd": "large", "arm": "large", "nvidia": "large",
    "qualcomm": "large", "intel": "large", "cisco": "large",
    "oracle": "large", "ibm": "large", "sap": "large",
    "autodesk": "large", "ebay": "large", "uber": "large",
    "netflix": "large", "spotify": "mid", "snap": "mid",
    "tiktok": "large", "tencent": "large",
    "cloudflare": "mid", "crowdstrike": "mid", "splunk": "mid",
    "pagerduty": "mid", "datadog": "mid", "mongodb": "mid",
    "confluent": "mid", "hashicorp": "mid", "gitlab": "mid",
    "jetbrains": "mid", "figma": "mid", "canva": "mid",
    "squarespace": "mid", "hubspot": "large",
    "zendesk": "large", "twilio": "mid", "sendgrid": "mid",
    "stripe": "mid", "adyen": "mid", "checkout.com": "mid",
    "revolut": "mid", "monzo": "mid", "wise": "mid",
    "starling bank": "mid", "oaknorth": "mid",
    "deepmind": "mid", "openai": "mid", "anthropic": "startup",
    "perplexity": "startup", "bending spoons": "startup",
    "deliveroo": "mid", "ocado group": "large",
    "bae systems": "large", "rolls-royce": "large",
    "airbus": "large", "thales": "large", "leonardo": "large",
    "ge": "large", "general electric": "large",
    "siemens": "large", "abb": "large",
    "astrazeneca": "large", "gsk": "large",
    "johnson & johnson": "large", "pfizer": "large",
    "bristol myers squibb": "large",
    "bp": "large", "shell": "large", "equinor": "large",
    "exxonmobil": "large", "rwe": "large", "e.on": "large",
    "national gas": "large", "national grid": "large",
    "bt": "large", "sky": "large", "vodafone": "large",
    "motorola solutions": "large", "keysight": "large",
    "f5": "large", "nutanix": "large", "te connectivity": "large",
    "viasat": "large", "ciena": "large", "viator": "mid",
    "waters corporation": "large", "pexip": "mid",
    "aveva": "large", "factset": "mid", "msci": "mid",
    "s&p global": "large", "moody's": "large",
    "fitch ratings": "mid", "london stock exchange group": "large",
    "lseg": "large", "cboe global markets": "large",
    "tradeweb": "mid", "cme group": "large", "ice": "large",
    "nasdaq": "large", "marketaxess": "mid",
    "bg group": "mid", "bpost": "mid",
    "mastercard": "large", "visa": "large", "paypal": "large",
    "american express": "large", "capital one": "large",
    "citizens financial group": "large",
    "huntington national bank": "mid",
    "texas capital bank": "mid", "firstbank": "mid",
    "five rings": "mid", "imc trading": "mid",
    "mako trading": "startup",
    # ── Accountancy / advisory ────────────────────────────────────────────────
    "grant thornton": "large", "rsmuk": "large", "rsm": "large",
    "rsm us": "large", "bdo global": "large", "bdo": "large",
    "forvis mazars": "large", "mazars": "large",
    "pkf littlejohn": "mid", "saffery": "mid",
    "haysmacintyre": "mid", "crowe": "large",
    "menzies": "mid", "johnston carmichael": "mid",
    "isio": "mid", "lcpq": "mid", "lcp": "mid",
    "barnett waddingham": "mid", "hymans robertson": "mid",
    "first actuarial": "mid", "mercer": "large", "wtwl": "large",
    "wtw": "large", "aon": "large", "marsh mclennan": "large",
    "lockton": "mid", "howden": "mid", "guy carpenter": "large",
    "swiss re": "large", "munich re": "large",
    "zurich insurance": "large", "hiscox": "mid",
    "convex insurance": "startup", "allied world": "mid",
    "markel": "mid", "ryan specialty": "mid",
    "hamilton insurance group": "mid",
    # ── Real estate ───────────────────────────────────────────────────────────
    "jll": "large", "cbre": "large", "cushman & wakefield": "large",
    "knight frank": "large", "savills": "large",
    "colliers": "large", "eastdil secured": "mid",
    "lasalle": "large", "brookfield": "large",
    "greystar": "large", "hines": "large",
    "nuveen": "large", "tishman speyer": "large",
    "starwood capital group": "mid", "british land": "large",
    "canary wharf group": "mid", "related argent": "mid",
    "northwood investors": "mid", "harrison street": "mid",
    "cabot properties": "mid", "w. p. carey": "large",
    "welltower": "large", "altus group": "mid",
    "carter jonas": "mid", "activumsg": "startup",
    # ── Financial services / insurance ────────────────────────────────────────
    "hargreaves lansdown": "large", "st james's place": "large",
    "evelyn partners": "large", "quilter": "large",
    "royal london": "large", "legal & general": "large",
    "nationwide": "large", "lloyds banking group": "large",
    "canada life": "large", "phoenix group": "large",
    "pension insurance corporation": "mid",
    "rothesay": "mid", "just": "mid",
    "brown shipley": "mid", "coutts": "mid",
    "weatherbys private bank": "startup",
    "arbuthnot latham": "mid", "handelsbanken": "large",
    "aj bell": "mid", "hargreaves lansdown": "large",
    "interactive investor": "mid", "moneysupermarket": "mid",
    "compare the market": "large",
    "burford capital": "mid", "ithaca energy": "mid",
    "7im": "mid", "aberdeen group": "large",
    "allianz global investors": "large", "allianz insurance": "large",
    "baillie gifford": "mid", "caledonia investments": "mid",
    "charles taylor": "mid", "ageas": "large",
    "aegon": "large",
    # ── Misc / multi-sector ───────────────────────────────────────────────────
    "ibm": "large", "accenture": "large", "capgemini": "large",
    "infosys": "large", "wipfli": "large", "wipro": "large",
    "tata consultancy services": "large", "tcs": "large",
    "hp": "large", "lenovo": "large", "nxp semiconductors": "large",
    "astrazeneca": "large", "pfizer": "large", "merck": "large",
    "abbvie": "large", "novartis": "large", "roche": "large",
    "l'oréal": "large", "loreal": "large",
    "p&g": "large", "unilever": "large",
    "red bull": "large", "tesco": "large", "sainsbury's": "large",
    "marriott international": "large", "hilton": "large",
    "british airways": "large", "eurostar": "large",
    "network rail": "large", "national highways": "large",
    "severn trent": "large", "centrica": "large",
    "national audit office": "large",
    "financial conduct authority": "large", "ebrd": "large",
    "uk atomic energy authority": "mid",
    "police scotland": "large",
    "trainline": "mid", "dojo": "startup",
    "dunelm": "large", "ocado group": "large",
    "sky": "large", "universal music group": "large",
    "the economist": "mid", "financial times": "large",
    "the wall street journal": "large",
    "warner bros. discovery": "large",
    "the walt disney company": "large",
    "dow jones": "large", "bloomberg": "large",
    "pitchbook": "mid", "preqin": "mid",
    "verisk": "large", "ihs markit": "large",
    "epic games": "large", "ubisoft": "large",
    "niantic": "mid", "king": "large",
    "roku": "mid", "the trade desk": "mid",
    "squarepoint capital": "mid",
    "glg": "mid", "alphasights": "mid",
    "teneo": "mid",
    "coinbase": "mid", "ripple": "startup",
    "blockchain.com": "startup", "copper.co": "startup",
}


# ── Lead pre-fetch: title → department mapping ────────────────────────────────
# Ordered list — first matching entry wins. Check most-specific patterns first.

TITLE_DEPT_MAP = [
    # Investment banking / M&A (check before generic "invest")
    (["investment banking", "m&a", "mergers", "dcm", "ecm", "leveraged finance",
      "capital markets", "fig ", "debt advisory", "restructuring", "ipo",
      "equity capital", "debt capital", "advisory intern", "ib analyst",
      "ib associate", "corporate finance"], "investment_banking"),
    # Quant (before sales_trading — "quant trader" should map here not sales_trading)
    (["quantitative", "quant trader", "quant research", "quant analyst",
      "quant intern", "strat intern", "structur"], "quant"),
    # Sales & trading / markets
    (["sales trading", "sales & trading", "fixed income", "derivatives",
      "fx trading", "equity sales", "trader", "markets intern", "trading intern",
      "flow trading", "market making"], "sales_trading"),
    # Equity research
    (["equity research", "research analyst", "sell-side", "research associate",
      "equity analyst"], "equity_research"),
    # Risk
    (["risk analyst", "risk manag", "credit risk", "market risk",
      "operational risk", "risk intern", "actuarial", "actuary"], "risk"),
    # Asset / wealth / portfolio / PE / VC / real estate
    (["asset management", "portfolio manag", "wealth manag", "fund manag",
      "investment manag", "private equity", "pe intern", "buyout",
      "venture capital", "vc intern", "real estate invest", "property invest",
      "credit invest", "special situations", "private credit",
      "infrastructure invest", "private markets"], "asset_management"),
    # Software engineering (before generic "engineer" / "data")
    (["software engineer", "software developer", "swe ", "backend", "frontend",
      "full stack", "mobile engineer", "platform engineer intern",
      "fullstack", "full-stack"], "software_engineering"),
    # Data / ML
    (["data scientist", "data analyst", "data engineer", "machine learning",
      "ml engineer", "analytics engineer", "data intern", "ai intern",
      "nlp ", "computer vision"], "data_ml"),
    # Product
    (["product manager", "product analyst", "product management",
      "product intern", "product owner"], "product"),
    # Infrastructure / devops
    (["devops", "infrastructure engineer", "sre ", "cloud engineer",
      "platform engineer", "site reliability"], "infrastructure"),
    # Design / UX
    (["ux designer", "product designer", "ui designer", "design intern",
      "ux intern", "user experience"], "design"),
    # Law
    (["training contract", "trainee solicitor", "trainee lawyer",
      "paralegal", "solicitor intern", "legal intern", "law intern"], "law_corporate"),
    # Generic engineer catch-all
    (["engineer", "engineering intern", "engineering placement"], "software_engineering"),
    # Generic "investments" / "finance" catch-all
    (["investment intern", "investment analyst", "investments intern",
      "finance intern", "financial analyst", "financial intern"], "investment_banking"),
]


# ── Lead pre-fetch: university short name → full official name ────────────────
# Used when building Serper queries so Google matches the full name in profiles.

UNI_FULL_NAMES = {
    # UK
    "ucl":       "University College London",
    "lse":       "London School of Economics",
    "imperial":  "Imperial College London",
    "king's":    "King's College London",
    "kcl":       "King's College London",
    "oxford":    "University of Oxford",
    "cambridge": "University of Cambridge",
    "edinburgh": "University of Edinburgh",
    "manchester": "University of Manchester",
    "bristol":   "University of Bristol",
    "warwick":   "University of Warwick",
    "durham":    "Durham University",
    "exeter":    "University of Exeter",
    "bath":      "University of Bath",
    "glasgow":   "University of Glasgow",
    "sheffield": "University of Sheffield",
    "southampton": "University of Southampton",
    "nottingham": "University of Nottingham",
    "leeds":     "University of Leeds",
    "liverpool": "University of Liverpool",
    "birmingham": "University of Birmingham",
    "newcastle": "Newcastle University",
    "st andrews": "University of St Andrews",
    "cardiff":   "Cardiff University",
    "york":      "University of York",
    "lancaster": "Lancaster University",
    "leicester": "University of Leicester",
    "reading":   "University of Reading",
    "surrey":    "University of Surrey",
    "sussex":    "University of Sussex",
    "qmul":      "Queen Mary University of London",
    "qub":       "Queen's University Belfast",
    "loughborough": "Loughborough University",
    "strathclyde": "University of Strathclyde",
    "heriot-watt": "Heriot-Watt University",
    "soas":      "SOAS University of London",
    # US
    "harvard":   "Harvard University",
    "yale":      "Yale University",
    "princeton": "Princeton University",
    "stanford":  "Stanford University",
    "columbia":  "Columbia University",
    "upenn":     "University of Pennsylvania",
    "wharton":   "University of Pennsylvania",
    "mit":       "Massachusetts Institute of Technology",
    "berkeley":  "UC Berkeley",
    "nyu":       "New York University",
    "dartmouth": "Dartmouth College",
    "brown":     "Brown University",
    "cornell":   "Cornell University",
    "uchicago":  "University of Chicago",
    "northwestern": "Northwestern University",
    "duke":      "Duke University",
    "jhu":       "Johns Hopkins University",
    "ucla":      "University of California Los Angeles",
    "umich":     "University of Michigan",
    "georgetown": "Georgetown University",
    "cmu":       "Carnegie Mellon University",
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
