"""
CCC Backend — LinkedIn Lead Matcher (Phase 3)

Strategy:
1. For a given job (company + title + industry), build a search query
   targeting public LinkedIn profiles at that company.
2. Optionally prepend the student's university for alumni-first results.
3. Parse the search result snippets to extract: name, title, company, university.
4. Score each candidate on relevance (title match + alumni + seniority + tenure).
5. Return top N candidates — ephemeral, not stored as permanent person records.

Required env vars (pick one — checked in priority order):
    BRAVE_SEARCH_API_KEY  — Brave Search API (independent index, ~1k queries/month
                            for ~$5 credit; $5/1k after that)
                            https://api-dashboard.search.brave.com
                            NOTE: Bing Search API v7 was decommissioned August 11 2025.
                            BING_SEARCH_API_KEY will be ignored if set.

    SERPAPI_KEY           — SerpAPI (wraps Google, $50/mo for 5k searches,
                            100 free/month) https://serpapi.com

Fallback (no key set):
    Matcher returns empty results and logs a clear warning.
"""
import os
import re
import json
import logging
import urllib.parse
import time
from datetime import datetime, date
from pathlib import Path
from typing import Iterator

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import fetch_url, fetch_json, RequestError
from config.settings import SENIORITY_KEYWORDS, REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)

# ── API endpoints ────────────────────────────────────────────────────────────
# NOTE: Bing Search API v7 was decommissioned August 11 2025. Do not use.
BRAVE_ENDPOINT   = "https://api.search.brave.com/res/v1/web/search"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# ── Seniority level numbers for "2–4 levels above student" check ────────────
SENIORITY_RANK = {
    "intern":     0,
    "junior":     1,
    "mid":        2,
    "senior":     3,
    "leadership": 4,
}

# ── University name aliases for fuzzy matching ───────────────────────────────
UNI_ALIASES: dict[str, list[str]] = {
    # ── UK ───────────────────────────────────────────────────────────────────
    "ucl":              ["university college london", "ucl"],
    "lse":              ["london school of economics", "lse"],
    "imperial":         ["imperial college london", "imperial college", "imperial"],
    "kings":            ["king's college london", "kings college london", "kcl"],
    "oxford":           ["university of oxford", "oxford"],
    "cambridge":        ["university of cambridge", "cambridge"],
    "edinburgh":        ["university of edinburgh", "edinburgh"],
    "manchester":       ["university of manchester", "manchester"],
    "bristol":          ["university of bristol", "bristol"],
    "warwick":          ["university of warwick", "warwick"],
    "durham":           ["durham university", "durham"],
    "exeter":           ["university of exeter", "exeter"],
    "bath":             ["university of bath", "bath"],
    "glasgow":          ["university of glasgow", "glasgow"],
    "sheffield":        ["university of sheffield", "sheffield"],
    "southampton":      ["university of southampton", "southampton"],
    "nottingham":       ["university of nottingham", "nottingham"],
    "leeds":            ["university of leeds", "leeds"],
    "liverpool":        ["university of liverpool", "liverpool"],
    "birmingham":       ["university of birmingham", "birmingham"],
    "newcastle":        ["newcastle university", "newcastle"],
    "st_andrews":       ["university of st andrews", "st andrews", "saint andrews"],
    "cardiff":          ["cardiff university", "cardiff"],
    "york":             ["university of york", "york"],
    "lancaster":        ["lancaster university", "lancaster"],
    "leicester":        ["university of leicester", "leicester"],
    "reading":          ["university of reading", "reading"],
    "surrey":           ["university of surrey", "surrey"],
    "sussex":           ["university of sussex", "sussex"],
    "qmul":             ["queen mary university of london", "qmul", "queen mary"],
    "queens_belfast":   ["queens university belfast", "qub", "queen's university belfast"],
    "loughborough":     ["loughborough university", "loughborough"],
    "aberdeen":         ["university of aberdeen", "aberdeen"],
    "strathclyde":      ["university of strathclyde", "strathclyde"],
    "heriot_watt":      ["heriot-watt university", "heriot watt"],
    "soas":             ["soas university of london", "soas"],
    "rhul":             ["royal holloway university of london", "royal holloway"],
    "goldsmiths":       ["goldsmiths university of london", "goldsmiths"],
    "open":             ["open university", "the open university"],
    # ── US ───────────────────────────────────────────────────────────────────
    "harvard":          ["harvard university", "harvard"],
    "yale":             ["yale university", "yale"],
    "princeton":        ["princeton university", "princeton"],
    "stanford":         ["stanford university", "stanford"],
    "columbia":         ["columbia university", "columbia"],
    "penn":             ["university of pennsylvania", "upenn", "wharton"],
    "mit":              ["massachusetts institute of technology", "mit"],
    "berkeley":         ["uc berkeley", "university of california berkeley", "ucb", "cal"],
    "nyu":              ["new york university", "nyu"],
    "dartmouth":        ["dartmouth college", "dartmouth"],
    "brown":            ["brown university", "brown"],
    "cornell":          ["cornell university", "cornell"],
    "uchicago":         ["university of chicago", "uchicago"],
    "northwestern":     ["northwestern university", "northwestern"],
    "duke":             ["duke university", "duke"],
    "johns_hopkins":    ["johns hopkins university", "jhu", "johns hopkins"],
    "ucla":             ["university of california los angeles", "ucla"],
    "umich":            ["university of michigan", "umich", "michigan"],
    "georgetown":       ["georgetown university", "georgetown"],
    "cmu":              ["carnegie mellon university", "carnegie mellon", "cmu"],
    "vanderbilt":       ["vanderbilt university", "vanderbilt"],
    "uva":              ["university of virginia", "uva"],
    "unc":              ["university of north carolina", "unc", "chapel hill"],
    "usc":              ["university of southern california", "usc"],
    "gatech":           ["georgia institute of technology", "georgia tech", "gatech"],
    "purdue":           ["purdue university", "purdue"],
    "tufts":            ["tufts university", "tufts"],
    "boston_u":         ["boston university", "bu"],
    "northeastern":     ["northeastern university", "northeastern"],
    "bu":               ["boston university"],
    "umd":              ["university of maryland", "umd"],
    "ufl":              ["university of florida", "uf", "gators"],
    "ohio_state":       ["ohio state university", "osu"],
    "rutgers":          ["rutgers university", "rutgers"],
    "gw":               ["george washington university", "gwu"],
    "rice":             ["rice university", "rice"],
    "emory":            ["emory university", "emory"],
}


def _normalise_uni(raw: str) -> str:
    """Lowercase and strip punctuation for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]", "", raw.lower()).strip()


def is_alumni(person_university: str, student_university: str) -> bool:
    """Return True if person attended the same university as the student."""
    if not person_university or not student_university:
        return False

    p = _normalise_uni(person_university)
    s = _normalise_uni(student_university)

    # Direct substring match
    if s in p or p in s:
        return True

    # Alias lookup
    for canonical, aliases in UNI_ALIASES.items():
        p_match = any(a in p for a in aliases)
        s_match = any(a in s for a in aliases)
        if p_match and s_match:
            return True

    return False


# ── Query builder ─────────────────────────────────────────────────────────────

def build_search_query(
    company:            str,
    job_title:          str,
    student_university: str = "",
    max_results:        int = 8,
) -> str:
    """
    Build a Bing/Google search query to surface LinkedIn profiles
    of professionals at `company` relevant to the `job_title` team.

    Example output:
        site:linkedin.com/in "Goldman Sachs" "risk" ("analyst" OR "associate") "UCL"
    """
    # Extract 1-2 keywords from the job title (team/function)
    team_keywords = _extract_team_keywords(job_title)

    # Seniority range: 1–2 levels above the student
    seniority_terms = ["analyst", "associate", "manager", "director", "vice president"]

    parts = [f'site:linkedin.com/in']
    parts.append(f'"{company}"')

    if team_keywords:
        kw_str = " OR ".join(f'"{k}"' for k in team_keywords[:2])
        parts.append(f"({kw_str})")

    seniority_str = " OR ".join(f'"{s}"' for s in seniority_terms[:3])
    parts.append(f"({seniority_str})")

    if student_university:
        # Alumni boost — append university name
        uni_clean = student_university.strip().strip('"')
        parts.append(f'"{uni_clean}"')

    return " ".join(parts)


def _extract_team_keywords(job_title: str) -> list[str]:
    """Pull meaningful function/team keywords from a job title."""
    STOP = {
        "analyst", "associate", "intern", "graduate", "junior", "senior",
        "manager", "director", "officer", "executive", "programme", "program",
        "summer", "spring", "winter", "2026", "2025", "2027", "new", "grad",
        "role", "position", "opening", "job", "full", "time", "part",
    }
    words = re.findall(r"[a-zA-Z&]+", job_title.lower())
    keywords = [w for w in words if w not in STOP and len(w) > 3]
    return keywords[:3]


# ── Search backends ──────────────────────────────────────────────────────────

class LinkedInMatcher:
    """
    Surfaces LinkedIn profile leads for a given job.
    Uses Brave Search API (primary); falls back to SerpAPI; logs if neither available.
    """

    def __init__(self):
        self.serper_key = os.environ.get("SERPER_API_KEY", "")   # primary — serper.dev
        self.brave_key  = os.environ.get("BRAVE_SEARCH_API_KEY", "") # secondary
        self.serp_key   = os.environ.get("SERPAPI_KEY", "")      # fallback
        # Warn if someone has the old dead Bing key set
        if os.environ.get("BING_SEARCH_API_KEY"):
            logger.warning(
                "BING_SEARCH_API_KEY is set but the Bing Search API v7 was "
                "decommissioned on August 11 2025 and no longer works. "
                "Set SERPER_API_KEY instead — free trial at https://serper.dev"
            )
        self._last_req  = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_req
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)
        self._last_req = time.time()

    def find_leads(
        self,
        company:            str,
        job_title:          str,
        student_university: str = "",
        n:                  int = 8,
    ) -> list[dict]:
        """
        Return up to `n` lead dicts for the given company/job.
        Tries the primary company name first, then falls back to variants if <3 results.
        Each lead: {name, title, company, university, linkedin_url,
                    tenure_months, is_alumni, snippet}
        """
        if not (self.serper_key or self.brave_key or self.serp_key):
            logger.warning(
                "No search API key set. LinkedIn matching is disabled. "
                "Set SERPER_API_KEY — free trial at https://serper.dev (recommended, $0.30/1k queries)."
            )
            return []

        # Build queries using primary name + variants
        variants   = company_name_variants(company)
        all_leads  = []
        seen_urls  = set()

        for variant in variants[:2]:  # max 2 variants to limit API calls
            query = build_search_query(variant, job_title, student_university)
            logger.info(f"Search query: {query}")

            results = []
            if self.serper_key:
                results = self._serper_search(query, count=n)
            elif self.brave_key:
                results = self._brave_search(query, count=n)
            elif self.serp_key:
                results = self._serp_search(query, n=n)

            for r in results:
                lead = self._parse_snippet(r)
                if not lead:
                    continue
                url = lead.get("linkedin_url", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                lead["is_alumni"] = is_alumni(
                    lead.get("university", ""), student_university
                )
                all_leads.append(lead)

            if len(all_leads) >= n:
                break  # enough results from first variant

        return all_leads[:n]

    # ── Brave Search backend (PRIMARY) ───────────────────────────────────────
    def _brave_search(self, query: str, count: int = 10) -> list[dict]:
        """
        Brave's independent web index — the correct replacement for Bing v7.
        Bing Search API v7 was decommissioned August 11 2025.
        Brave: https://api-dashboard.search.brave.com
        Pricing: ~$5/1k requests. New accounts get ~$5 credit (~1k free queries).
        """
        self._throttle()
        params = urllib.parse.urlencode({
            "q":           query,
            "count":       min(count, 20),   # Brave max 20 per request
            "country":     "GB",
            "search_lang": "en",
            "safesearch":  "off",
        })
        url = f"{BRAVE_ENDPOINT}?{params}"
        headers = {
            "Accept":               "application/json",
            "Accept-Encoding":      "gzip",
            "X-Subscription-Token": self.brave_key,
        }
        try:
            raw  = fetch_url(url, headers=headers)
            data = json.loads(raw.decode("utf-8", errors="replace"))
            results = data.get("web", {}).get("results", [])
            return [
                {
                    "url":     r.get("url", ""),
                    "name":    r.get("title", ""),
                    "snippet": r.get("description", ""),
                }
                for r in results
            ]
        except Exception as e:
            logger.error(f"Brave search failed: {e}")
            return []

    # ── Serper backend (Google) ───────────────────────────────────────────────
    def _serper_search(self, query: str, count: int = 10) -> list[dict]:
        """
        Serper.dev — Google search API. $0.30/1k queries, free trial.
        Returns results in a format compatible with _parse_snippet().
        """
        self._throttle()
        payload = json.dumps({"q": query, "num": min(count, 10), "gl": "gb", "hl": "en"}).encode()
        import urllib.request
        req = urllib.request.Request(
            SERPER_ENDPOINT,
            data=payload,
            headers={
                "X-API-KEY":    self.serper_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            # Normalise Serper's organic results to the same shape _parse_snippet expects
            organic = data.get("organic", [])
            normalised = []
            for item in organic:
                normalised.append({
                    "name":    item.get("title", ""),
                    "url":     item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                })
            return normalised
        except Exception as e:
            logger.error(f"Serper search failed: {e}")
            return []

    # ── SerpAPI backend ───────────────────────────────────────────────────────
    def _serp_search(self, query: str, n: int = 10) -> list[dict]:
        self._throttle()
        url = (
            f"{SERPAPI_ENDPOINT}"
            f"?engine=google&q={urllib.parse.quote(query)}"
            f"&api_key={self.serp_key}&num={n}&gl=gb&hl=en"
        )
        try:
            data = fetch_json(url)
            organic = data.get("organic_results", [])
            # Normalise to Bing-style format
            return [
                {
                    "url":     r.get("link", ""),
                    "name":    r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                }
                for r in organic
            ]
        except Exception as e:
            logger.error(f"SerpAPI search failed: {e}")
            return []

    # ── Snippet parser ────────────────────────────────────────────────────────
    def _parse_snippet(self, result: dict) -> dict | None:
        """
        Extract structured lead data from a search result snippet.

        Bing result format:
        {
          "name": "John Smith - Risk Analyst - Goldman Sachs | LinkedIn",
          "url":  "https://uk.linkedin.com/in/john-smith-...",
          "snippet": "John Smith. Risk Analyst at Goldman Sachs. UCL Economics 2019."
        }
        """
        raw_name    = result.get("name", "")
        url         = result.get("url", result.get("link", ""))
        snippet     = result.get("snippet", "")

        if "linkedin.com/in/" not in url:
            return None

        # Parse "John Smith - Risk Analyst - Goldman Sachs | LinkedIn"
        name, title, company = "", "", ""
        title_parts = re.split(r"\s*[\|–\-]\s*", raw_name)
        title_parts = [p.strip() for p in title_parts if p.strip()]

        if title_parts:
            # Remove "LinkedIn" suffix
            title_parts = [p for p in title_parts if p.lower() not in ("linkedin",)]

        if len(title_parts) >= 1:
            name = title_parts[0]
        if len(title_parts) >= 2:
            title = title_parts[1]
        if len(title_parts) >= 3:
            company = title_parts[2]

        # Validate — must have at least a name
        if not name or len(name.split()) < 1:
            return None

        # Extract university from snippet
        university = _extract_university(snippet)

        # Extract tenure hint ("X years at Y", "since YYYY")
        tenure_months = _extract_tenure(snippet)

        # Clean LinkedIn URL
        linkedin_url = re.sub(r"\?.*$", "", url)  # strip query params

        return {
            "name":           name,
            "title":          title,
            "company":        company,
            "university":     university,
            "linkedin_url":   linkedin_url,
            "tenure_months":  tenure_months,
            "snippet":        snippet[:300],
        }


def _extract_university(text: str) -> str:
    """Try to find a university name in a snippet."""
    text_lower = text.lower()
    for canonical, aliases in UNI_ALIASES.items():
        for alias in aliases:
            if alias in text_lower:
                return alias.title()
    # Generic patterns: "University of X", "X University"
    m = re.search(
        r"\b(university of [a-z ]+|[a-z ]+ university|[a-z ]+ college)\b",
        text_lower,
    )
    if m:
        return m.group(0).strip().title()
    return ""


def _extract_tenure(text: str) -> int:
    """Extract approximate tenure in months from snippet text."""
    # "3 years" → 36, "18 months" → 18, "since 2022" → approx
    m = re.search(r"(\d+)\s+year", text, re.I)
    if m:
        return int(m.group(1)) * 12

    m = re.search(r"(\d+)\s+month", text, re.I)
    if m:
        return int(m.group(1))

    m = re.search(r"since\s+(20\d{2})", text, re.I)
    if m:
        years = datetime.utcnow().year - int(m.group(1))
        return max(0, years * 12)

    return 0


# ── Relevance scoring ─────────────────────────────────────────────────────────

def score_lead(
    lead:               dict,
    job:                dict,
    student:            dict,
) -> float:
    """
    Score a lead (0–100) for a given job and student profile.

    Components:
        Title match to hiring team   40 pts
        Alumni status                25 pts
        Seniority fit                20 pts   (2–4 levels above student = intern/junior)
        Tenure fit                   15 pts   (1–5 years = sweet spot)
    """
    score = 0.0

    # ── Title match (40 pts) ──────────────────────────────────────────────────
    job_title  = job.get("title", "").lower()
    lead_title = lead.get("title", "").lower()
    team_kws   = _extract_team_keywords(job_title)
    if lead_title:
        matches = sum(1 for k in team_kws if k in lead_title)
        score += min(40, matches * 15)
    # Partial: if company name matches
    if lead.get("company", "").lower() in job.get("company_name", "").lower():
        score += 5  # small confirmation bonus

    # ── Alumni (25 pts) ───────────────────────────────────────────────────────
    if lead.get("is_alumni"):
        score += 25

    # ── Seniority fit (20 pts) ────────────────────────────────────────────────
    lead_seniority   = _infer_seniority_from_title(lead.get("title", ""))
    student_seniority = "intern"  # students are always at intern/junior level
    diff = SENIORITY_RANK.get(lead_seniority, 2) - SENIORITY_RANK.get(student_seniority, 0)
    if 1 <= diff <= 3:
        score += 20
    elif diff == 4:
        score += 8   # very senior — less approachable

    # ── Tenure fit (15 pts) ───────────────────────────────────────────────────
    tenure = lead.get("tenure_months", 0)
    if 12 <= tenure <= 60:
        score += 15
    elif tenure > 0:
        score += 7

    return round(min(score, 100), 1)


def _infer_seniority_from_title(title: str) -> str:
    t = title.lower()
    for band, keywords in SENIORITY_KEYWORDS.items():
        if any(k in t for k in keywords):
            return band
    return "mid"


# ── Company name variants ─────────────────────────────────────────────────────

COMPANY_VARIANTS: dict[str, list[str]] = {
    "Goldman Sachs":         ["Goldman Sachs", "Goldman", "GS"],
    "JPMorgan Chase":        ["JPMorgan Chase", "JPMorgan", "J.P. Morgan", "JP Morgan"],
    "Morgan Stanley":        ["Morgan Stanley"],
    "Barclays":              ["Barclays", "Barclays Capital", "BarCap"],
    "Deutsche Bank":         ["Deutsche Bank", "DB"],
    "Credit Suisse":         ["Credit Suisse", "CS"],
    "HSBC":                  ["HSBC", "HSBC Holdings"],
    "McKinsey & Company":    ["McKinsey & Company", "McKinsey", "McK"],
    "Boston Consulting Group": ["Boston Consulting Group", "BCG"],
    "Bain & Company":        ["Bain & Company", "Bain"],
    "Oliver Wyman":          ["Oliver Wyman"],
    "Google":                ["Google", "Alphabet", "Google LLC"],
    "Meta":                  ["Meta", "Facebook", "Meta Platforms"],
    "Apple":                 ["Apple", "Apple Inc"],
    "Amazon":                ["Amazon", "AWS", "Amazon Web Services"],
    "Microsoft":             ["Microsoft", "MSFT"],
    "Citadel":               ["Citadel", "Citadel LLC", "Citadel Securities"],
    "Two Sigma":             ["Two Sigma", "Two Sigma Investments"],
    "Jane Street":           ["Jane Street", "Jane Street Capital"],
    "BlackRock":             ["BlackRock", "BlackRock Inc"],
    "Stripe":                ["Stripe", "Stripe Inc"],
    "Revolut":               ["Revolut", "Revolut Ltd"],
    "Monzo":                 ["Monzo", "Monzo Bank"],
    "Deloitte":              ["Deloitte", "Deloitte & Touche"],
    "KPMG":                  ["KPMG"],
    "EY":                    ["EY", "Ernst & Young"],
    "PwC":                   ["PwC", "PricewaterhouseCoopers"],
    "Accenture":             ["Accenture"],
    "Palantir":              ["Palantir", "Palantir Technologies"],
    "Databricks":            ["Databricks"],
}

COMPANY_PRESTIGE: dict[str, int] = {
    # Tier 1 — highest brand recognition for students
    "Goldman Sachs": 10, "McKinsey & Company": 10, "Citadel": 10,
    "Jane Street": 10, "Google": 10, "Meta": 9, "Apple": 9,
    "Morgan Stanley": 9, "JPMorgan Chase": 9, "Amazon": 9,
    "Boston Consulting Group": 9, "Bain & Company": 9,
    "BlackRock": 8, "Microsoft": 8, "Stripe": 8, "Two Sigma": 8,
    "Barclays": 7, "Deutsche Bank": 7, "Deloitte": 7, "KPMG": 7,
    "EY": 7, "PwC": 7, "Accenture": 7, "Oliver Wyman": 7,
    "Monzo": 6, "Revolut": 6, "Palantir": 8, "Databricks": 7,
}


def company_name_variants(company: str) -> list[str]:
    """Return search-friendly name variants for a company."""
    for canonical, variants in COMPANY_VARIANTS.items():
        if company.lower() in [v.lower() for v in variants]:
            return variants
    # Default: just the original
    return [company]


def _company_prestige_score(company: str) -> int:
    for canonical, score in COMPANY_PRESTIGE.items():
        if canonical.lower() in company.lower() or company.lower() in canonical.lower():
            return score
    return 3  # unknown company baseline


# ── Upgraded scorer ───────────────────────────────────────────────────────────

# Stop words for title keyword extraction
_TITLE_STOP_WORDS = {
    "a", "an", "the", "and", "or", "of", "in", "at", "to", "for",
    "with", "on", "by", "is", "as", "be", "are", "was", "were",
}

# Company-size bucket mapping
_SIZE_MAP: dict[str, str] = {}
_SIZE_STARTUP_TOKENS  = {"startup", "small", "under", "seed", "early", "series"}
_SIZE_MID_TOKENS      = {"mid", "medium", "growth", "scale"}
_SIZE_LARGE_TOKENS    = {"large", "enterprise", "corporate", "big", "multinational"}


def _normalise_company_size(raw: str | None) -> str | None:
    """Map a free-text company size string to 'startup', 'mid', or 'large'."""
    if not raw:
        return None
    r = raw.lower()
    if any(t in r for t in _SIZE_STARTUP_TOKENS):
        return "startup"
    if any(t in r for t in _SIZE_MID_TOKENS):
        return "mid"
    if any(t in r for t in _SIZE_LARGE_TOKENS):
        return "large"
    # Pass-through for exact values already normalised
    if r in ("startup", "mid", "large"):
        return r
    return None


def _title_relevance_pts(job_title: str, lead_title: str) -> float:
    """Return 0/10/15/20 pts based on keyword overlap between job and lead titles."""
    if not job_title or not lead_title:
        return 0.0
    # Tokenise job title: split on spaces, dashes, commas; filter stop words; up to 3 words
    raw_words = re.split(r"[\s\-,/]+", job_title.lower())
    keywords = [
        w for w in raw_words
        if w and w not in _TITLE_STOP_WORDS and len(w) > 2
    ][:3]
    if not keywords:
        return 0.0
    lead_lower = lead_title.lower()
    matches = sum(1 for k in keywords if k in lead_lower)
    if matches >= 3:
        return 20.0
    elif matches == 2:
        return 15.0
    elif matches == 1:
        return 10.0
    return 0.0


def score_lead_v2(
    lead:    dict,
    job:     dict,
    student: dict,
) -> tuple[float, dict]:
    """
    Extended lead scorer returning (score, breakdown_dict).

    Components (total 100):
      industry_match     25 pts  — student industry preferences vs job industry
      company_size       10 pts  — student size preference vs job company size
      title_relevance    20 pts  — job title keywords found in lead title
      alumni             20 pts  — shared university
      seniority_fit      15 pts  — 1-3 levels above intern
      tenure_fit         10 pts  — sweet spot 12-36 months
    """
    breakdown: dict[str, float] = {
        "industry_match":  0.0,
        "company_size":    0.0,
        "title_relevance": 0.0,
        "alumni":          0.0,
        "seniority_fit":   0.0,
        "tenure_fit":      0.0,
    }
    score = 0.0

    # ── Industry match (25pts) ──
    raw_industries = student.get("industries") or "[]"
    if isinstance(raw_industries, str):
        try:
            student_industries = json.loads(raw_industries)
        except (ValueError, TypeError):
            student_industries = []
    else:
        student_industries = list(raw_industries)

    job_industry = (job.get("industry") or "").strip()
    ind_pts = 0.0
    if job_industry and student_industries:
        job_ind_lower = job_industry.lower()
        student_inds_lower = [s.lower() for s in student_industries]
        if job_ind_lower in student_inds_lower:
            ind_pts = 25.0
        elif any(
            job_ind_lower in si or si in job_ind_lower
            for si in student_inds_lower
        ):
            ind_pts = 12.0
    breakdown["industry_match"] = ind_pts
    score += ind_pts

    # ── Company size match (10pts) ──
    student_size = _normalise_company_size(student.get("company_size"))
    job_size     = _normalise_company_size(job.get("company_size"))
    size_pts = 10.0 if (student_size and job_size and student_size == job_size) else 0.0
    breakdown["company_size"] = size_pts
    score += size_pts

    # ── Title relevance (20pts) ──
    job_title  = job.get("title", "") or ""
    lead_title = lead.get("title") or ""
    trel_pts   = _title_relevance_pts(job_title, lead_title)
    breakdown["title_relevance"] = trel_pts
    score += trel_pts

    # ── Alumni (20pts) ──
    alumni_pts = 20.0 if lead.get("is_alumni") else 0.0
    breakdown["alumni"] = alumni_pts
    score += alumni_pts

    # ── Seniority fit (15pts) ──
    lead_seniority = _infer_seniority_from_title(lead.get("title") or "")
    diff = SENIORITY_RANK.get(lead_seniority, 2) - SENIORITY_RANK.get("intern", 0)
    if 1 <= diff <= 3:
        sen_pts = 15.0
    elif diff == 4:
        sen_pts = 5.0
    else:
        sen_pts = 0.0
    breakdown["seniority_fit"] = sen_pts
    score += sen_pts

    # ── Tenure fit (10pts) — 12-36 months is sweet spot ──
    tenure = lead.get("tenure_months") or 0
    if 12 <= tenure <= 36:
        ten_pts = 10.0
    elif 6 <= tenure < 12 or 36 < tenure <= 60:
        ten_pts = 6.0
    elif tenure > 0:
        ten_pts = 3.0
    else:
        ten_pts = 0.0
    breakdown["tenure_fit"] = ten_pts
    score += ten_pts

    total = round(min(score, 100), 1)
    return total, breakdown


# ── Batch lead finder ─────────────────────────────────────────────────────────

def batch_find_leads(
    jobs:    list[dict],
    student: dict,
    n_per_job: int = 5,
    db_path = None,
) -> dict[int, list[dict]]:
    """
    Efficiently find leads for multiple jobs at once.
    Returns {job_id: [lead_dicts]}.
    Deduplicates LinkedIn URLs across all jobs.
    Uses profile cache when available.
    """
    from pipeline.profile_cache import get_cached, put_cached, init_cache
    if db_path:
        try:
            init_cache(db_path)
        except Exception:
            pass

    matcher    = LinkedInMatcher()
    all_leads: dict[int, list[dict]] = {}
    seen_urls:  set[str] = set()

    for job in jobs:
        job_id   = job.get("id", 0)
        company  = job.get("company_name", "")
        title    = job.get("title", "")
        uni      = student.get("university", "")

        leads = matcher.find_leads(
            company            = company,
            job_title          = title,
            student_university = uni,
            n                  = n_per_job + 3,   # fetch a few extra for dedup headroom
        )

        # Deduplicate across jobs, check cache
        fresh_leads = []
        for lead in leads:
            url = lead.get("linkedin_url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)

            # Check profile cache
            if url and db_path:
                cached = get_cached(url, db_path)
                if cached:
                    lead.update({k: v for k, v in cached.items() if k in
                                 ("name", "title", "company", "university", "tenure_months")})
                    lead["cache_hit"] = True
                else:
                    lead["cache_hit"] = False
                    if url:
                        try:
                            put_cached(url, lead, db_path)
                        except Exception:
                            pass

            fresh_leads.append(lead)
            if len(fresh_leads) >= n_per_job:
                break

        all_leads[job_id] = fresh_leads

    return all_leads
