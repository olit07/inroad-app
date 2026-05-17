"""
pipeline/lead_builder.py
Systematic lead pre-fetch: crawls Google (via Serper) for LinkedIn profiles
for every (company, department, location) combination derived from active
Trackr jobs, and stores the results in the `leads` DB table.

Also writes a JSONL training file for prompt improvement:
  data/leads_training.jsonl

Usage:
    python pipeline/lead_builder.py                    # run for all companies
    python pipeline/lead_builder.py --company "Goldman Sachs"
    python pipeline/lead_builder.py --dry-run          # parse only, no DB writes
"""
import os
import re
import sys
import json
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings   import (
    DEPT_MAP, TITLE_DEPT_MAP, TITLE_SEARCH_KEYWORD_MAP,
    UNI_FULL_NAMES, REGION_LOCATION_FALLBACK,
    COMPANY_EMAIL_FORMATS,
)
from db.database       import fetchall, upsert_lead, USE_POSTGRES, get_email_format, save_email_format
from pipeline.matcher  import LinkedInMatcher, _extract_university, _extract_tenure, _extract_location, _infer_seniority_from_title

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Per-run cache: company (lowercase) → (fmt_code, domain) or None
_email_format_cache: dict[str, tuple[str, str] | None] = {}


# ── Lead validation helpers ───────────────────────────────────────────────────

_CO_STOPWORDS = {
    'group', 'management', 'capital', 'asset', 'global', 'partners',
    'investment', 'financial', 'services', 'limited', 'company', 'and',
    'the', 'of', 'co', 'fund', 'funds', 'bank', 'trust', 'wealth',
}


def _company_name_overlap(a: str, b: str) -> bool:
    """True if a and b refer to the same company.

    For long names uses significant-word overlap.  For short / hyphenated names
    (acronyms like 'G-P', 'UBS') where word overlap is unreliable, strips
    hyphens and requires an exact token match — preventing e.g. 'NHS' from
    being accepted as a match for 'G-P'.
    """
    if not a or not b:
        return True  # no data to validate — allow through

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s.lower())

    words_a = {w for w in a.lower().split() if len(w) > 3 and w not in _CO_STOPWORDS}
    words_b = {w for w in b.lower().split() if len(w) > 3 and w not in _CO_STOPWORDS}

    if not words_b:
        # Company being searched is short (e.g. "G-P", "UBS") or all words are stopwords.
        # First try: normalised form appears as a whole token in the parsed company.
        b_norm = _norm(b)
        a_tokens = {_norm(t) for t in a.split()}
        if b_norm in a_tokens:
            return True
        # Multi-word short names like "BDA Partners": check each individual non-stopword
        # word of b (length > 1) against a_tokens so "BDA" matches "bda" in the title.
        b_parts = [_norm(w) for w in b.split() if w.lower() not in _CO_STOPWORDS and len(w) > 1]
        return bool(b_parts and any(p in a_tokens for p in b_parts))

    if not words_a:
        # Parsed company is short; accept only if it appears as a token in the
        # searched company name.
        a_norm = _norm(a)
        b_tokens = {_norm(t) for t in b.split()}
        return a_norm in b_tokens

    return bool(words_a & words_b)


_TWO_DATE_PAT = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}'
    r'\s*[-–]\s*'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}',
    re.IGNORECASE,
)


def _snippet_role_is_past(snippet: str) -> bool:
    """True if the first date range in the snippet is a closed (past) range."""
    return bool(_TWO_DATE_PAT.search(snippet[:250]))


# Output path for training dataset
TRAINING_FILE = Path(__file__).parent.parent / "data" / "leads_training.jsonl"


# ── Query builders ────────────────────────────────────────────────────────────

def _infer_location_from_url(url: str) -> str:
    """Infer region from job URL when jobs.location is blank."""
    u = url.lower()
    if "uk.linkedin" in u or ".co.uk" in u or "/uk/" in u or "greenhouse.io" not in u and "uk" in u:
        return "UK"
    if "linkedin.com/jobs" in u and "uk" not in u:
        return "US"
    # Greenhouse/Lever/Ashby hosted roles default to US unless company is known UK
    if any(x in u for x in ["greenhouse.io", "lever.co", "ashbyhq.com", "workday.com"]):
        return "US"
    return "UK"  # safe default for Trackr which is primarily UK-focused


def _full_uni_name(university: str) -> str:
    """Expand a short university name to its full official name for Google search."""
    key = university.strip().lower()
    # Direct lookup
    if key in UNI_FULL_NAMES:
        return UNI_FULL_NAMES[key]
    # Already a full name (contains "University" or "College") — use as-is
    if "university" in key or "college" in key or "school of" in key:
        return university.strip()
    # No match — return as-is (better than silently dropping it)
    return university.strip()


# Per-dept positive keywords: a lead is only "Relevant Team" if their title
# contains at least one of these for the programme's dept_tag.
# Titles that don't match any keyword → "general" regardless of dept.
_DEPT_RELEVANT_KW: dict[str, set[str]] = {
    "investment_banking": {
        "investment banking", "m&a", "mergers", "advisory", "corporate finance",
        "capital markets", "dcm", "ecm", "leveraged finance", "restructuring",
        "analyst", "associate", "vice president", "director", "managing director",
        "banker",
    },
    "sales_trading": {
        "trader", "trading", "sales", "markets", "fixed income", "fx",
        "derivatives", "flow", "structured products", "analyst", "associate",
    },
    "asset_management": {
        "portfolio manager", "portfolio analyst", "fund manager", "investment manager",
        "private equity", "venture capital", "private credit", "direct lending",
        "credit analyst", "credit associate", "asset management", "wealth management",
        "investor relations", "capital formation", "infrastructure analyst",
        "real estate", "private markets", "investment analyst", "investment associate",
        "analyst", "associate",
    },
    "equity_research": {
        "equity research", "research analyst", "sell-side", "analyst", "associate",
    },
    "risk": {
        "risk analyst", "risk manager", "risk associate", "credit risk",
        "market risk", "operational risk", "actuary", "actuarial",
    },
    "quant": {
        "quantitative", "quant", "structurer", "strat",
    },
    "consulting": {
        "consultant", "consulting", "strategy", "advisory", "business analyst",
        "associate consultant", "management consultant",
    },
    "law_corporate": {"solicitor", "lawyer", "trainee", "partner", "legal", "counsel"},
    "law_finance":   {"solicitor", "lawyer", "trainee", "partner", "legal", "counsel"},
    "law_disputes":  {"solicitor", "lawyer", "trainee", "partner", "legal", "counsel"},
    "law_tech":      {"solicitor", "lawyer", "trainee", "partner", "legal", "counsel"},
    "healthcare": {
        "clinical", "pharmaceutical", "pharma", "biotech", "health economist",
        "life sciences", "medical", "healthcare analyst",
    },
    "software_engineering": {
        "software engineer", "software developer", "backend", "frontend",
        "full stack", "fullstack", "swe", "programmer",
    },
    "data_ml": {
        "data scientist", "machine learning", "ml engineer", "ai researcher",
        "ai engineer", "data analyst", "analytics engineer", "nlp", "computer vision",
    },
    "product": {"product manager", "product analyst", "product owner", "pm"},
    "infrastructure": {
        "devops", "infrastructure engineer", "platform engineer", "sre",
        "site reliability", "cloud engineer",
    },
    "design": {"designer", "ux", "ui designer", "product designer", "ux researcher"},
    "marketing": {
        "marketing", "brand manager", "growth", "content", "communications",
        "digital marketing", "pr manager",
    },
    "general": {"analyst", "associate", "consultant", "advisor"},
}


def _classify_lead_type(lead_title: str, dept_tag: str) -> str:
    """Return 'relevant' only if the lead's title matches the programme's field.
    Uses dept-specific positive keyword matching — no title → 'general'."""
    t = lead_title.lower()
    relevant_kw = _DEPT_RELEVANT_KW.get(dept_tag, _DEPT_RELEVANT_KW["general"])
    return "relevant" if any(kw in t for kw in relevant_kw) else "general"


def _dept_from_title(title: str) -> str:
    """
    Map a job title to a single DEPT_MAP key by scanning title keywords.
    Returns the first matching dept_tag from TITLE_DEPT_MAP, or
    'general' as a safe default (searches for 'analyst').
    """
    title_lower = title.lower()
    for keywords, dept_tag in TITLE_DEPT_MAP:
        if any(kw in title_lower for kw in keywords):
            return dept_tag
    return "general"


def _search_keyword_from_title(title: str, dept_tag: str) -> str:
    """
    Return the most specific LinkedIn search keyword for a given job title.
    Checks TITLE_SEARCH_KEYWORD_MAP first (title-specific), then falls back
    to the first keyword in DEPT_MAP[dept_tag].
    """
    title_lower = title.lower()
    for triggers, keyword in TITLE_SEARCH_KEYWORD_MAP:
        if any(t in title_lower for t in triggers):
            return keyword
    return (DEPT_MAP.get(dept_tag) or ["consultant"])[0]


def _guess_domain_fallback(company: str) -> str:
    """Fallback domain guess if Claude call fails."""
    name = company.lower()
    for suffix in [
        " capital management", " asset management", " investment management",
        " wealth management", " portfolio management",
        " & co.", " & co", " and co", " & company", " llp", " llc",
        " inc.", " inc", " ltd", " plc", " group", " partners",
        " advisors", " advisory", " associates", " holdings",
        " securities", " financial", " services", " ventures",
        " consulting", " international", " global", " management",
    ]:
        name = name.replace(suffix, "")
    name = re.sub(r"[^a-z0-9]", "", name)
    return f"{name}.com" if name else ""


# Known ATS / job-portal domains that are NEVER real employee email domains
_ATS_DOMAINS = {
    "workday.com", "myworkday.com", "myworkdaysite.com", "wd1.myworkdaysite.com",
    "wd3.myworkdaysite.com", "wd5.myworkdaysite.com",
    "myworkdayjobs.com", "wd1.myworkdayjobs.com", "wd3.myworkdayjobs.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "tal.net",
    "taleo.net", "icims.com", "smartrecruiters.com", "jobvite.com",
    "successfactors.com", "bamboohr.com", "workable.com", "recruitee.com",
    "ultipro.com", "ultipro.net", "recruiting.ultipro.com",
    "linkedin.com", "indeed.com", "glassdoor.com",
}


def _is_ats_domain(domain: str) -> bool:
    """Return True if domain is a job-portal / ATS platform, not a company email domain."""
    d = domain.lower().strip()
    # Exact match
    if d in _ATS_DOMAINS:
        return True
    # Suffix match (e.g. "pjtpartners.wd1.myworkdayjobs.com")
    for bad in _ATS_DOMAINS:
        if d.endswith("." + bad) or d == bad:
            return True
    return False


def _lookup_email_format_via_llm(company: str) -> tuple[str, str] | None:
    """
    Ask Groq (Llama 3) to determine the email format and domain for a company.
    Returns (fmt_code, domain) or None on failure.
    fmt_code: "FL" = firstname.lastname, "fL" = flastname,
              "f.L" = f.lastname, "F_L" = firstname_lastname, "F" = firstname
    """
    import requests as _req
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_EMAILLLM_API_KEY", "")
    if not api_key:
        return None
    prompt = (
        f"What is the corporate email format and domain for employees at \"{company}\"?\n\n"
        "IMPORTANT: Return the company's OWN email domain (e.g. snap.com, rothschildandco.com). "
        "Do NOT return job portal or ATS domains like workday.com, greenhouse.io, lever.co, "
        "tal.net, taleo.net, myworkdayjobs.com, or any similar hiring platform.\n\n"
        "Reply with ONLY a JSON object, no explanation:\n"
        "{\"format\": \"firstname.lastname\", \"domain\": \"company.com\"}\n\n"
        "Format must be one of: firstname.lastname | firstinitiallastname | firstinitial.lastname | "
        "firstname_lastname | firstname\n\n"
        "If unsure, make your best guess based on the company name."
    )
    try:
        resp = _req.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
                "temperature": 0,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        domain = (data.get("domain") or "").strip().lower().lstrip("@")
        fmt_str = (data.get("format") or "").strip().lower()
        fmt_map = {
            "firstname.lastname":    "FL",
            "firstinitiallastname":  "fL",
            "firstinitial.lastname": "f.L",
            "firstname_lastname":    "F_L",
            "firstname":             "F",
        }
        fmt = fmt_map.get(fmt_str, "FL")
        if not domain or _is_ats_domain(domain):
            logger.debug(f"LLM returned ATS/invalid domain '{domain}' for {company}, discarding")
            return None
        return fmt, domain
    except Exception as e:
        logger.debug(f"LLM email lookup failed for {company}: {e}")
        return None


def _get_email_format(company: str) -> tuple[str, str]:
    """
    Return (fmt_code, domain) for a company.
    Priority: settings hardcoded → in-memory cache → DB → Groq LLM → domain guess fallback.
    Persists new Groq results to DB so they're not re-fetched on future runs.
    """
    key = company.strip().lower()

    # 0. Hardcoded formats in settings.py (highest confidence)
    if key in COMPANY_EMAIL_FORMATS:
        return COMPANY_EMAIL_FORMATS[key]

    # 1. In-memory cache (fastest, avoids repeated DB hits within a run)
    if key in _email_format_cache:
        cached = _email_format_cache[key]
        return cached if cached else ("FL", _guess_domain_fallback(company))

    # 2. Persistent DB cache (skip Groq for already-known companies)
    try:
        db_result = get_email_format(company)
        if db_result:
            _email_format_cache[key] = db_result
            return db_result
    except Exception:
        pass

    # 3. Groq LLM lookup for new companies
    result = _lookup_email_format_via_llm(company)
    if result:
        _email_format_cache[key] = result
        logger.info(f"  Email format (Groq) for {company}: {result[0]} @ {result[1]}")
        try:
            save_email_format(company, result[0], result[1], source="groq")
        except Exception:
            pass
        return result

    # 4. Fallback: guess domain from company name
    domain = _guess_domain_fallback(company)
    return ("FL", domain)


_CREDENTIAL_SUFFIXES = {
    "cfa", "mba", "phd", "cpa", "ca", "acca", "frm", "cima",
    "cfp", "caia", "msc", "bsc", "ba", "llb", "llm", "md",
    "fca", "aca", "fcca", "cia", "cfe", "pmp", "esq",
}


def _strip_credentials(parts: list[str]) -> list[str]:
    """Remove trailing credential tokens (CFA, MBA, PhD, etc.) from a split name."""
    while parts:
        token = re.sub(r"[^a-z]", "", parts[-1].lower())
        if token in _CREDENTIAL_SUFFIXES:
            parts = parts[:-1]
        else:
            break
    return parts


def _infer_email(name: str, company: str) -> str:
    """Build expected email from name + company using Claude-determined format."""
    fmt, domain = _get_email_format(company)
    if not domain:
        return ""
    parts = _strip_credentials(name.strip().split())
    if len(parts) < 2:
        return ""
    first = re.sub(r"[^a-z]", "", parts[0].lower())
    last  = re.sub(r"[^a-z]", "", parts[-1].lower())
    if not first or not last:
        return ""
    if fmt == "FL":  return f"{first}.{last}@{domain}"
    if fmt == "fL":  return f"{first[0]}{last}@{domain}"
    if fmt == "f.L": return f"{first[0]}.{last}@{domain}"
    if fmt == "F_L": return f"{first}_{last}@{domain}"
    if fmt == "F":   return f"{first}@{domain}"
    return ""


def fix_ats_email_formats() -> int:
    """
    1. Delete company_email_formats rows where the stored domain is an ATS platform
       (e.g. workday.com, tal.net), then re-lookup via Groq for those companies.
    2. Scan ALL matches and leads for expected emails using ATS domains and rebuild them.
    Returns total number of rows fixed.
    """
    from db.database import fetchall as _fetchall, execute as _execute
    total_fixed = 0

    # ── 1. Fix company_email_formats table ───────────────────────────────────
    rows = _fetchall("SELECT company, fmt_code, domain FROM company_email_formats")
    bad_companies = [r["company"] for r in rows if _is_ats_domain(r["domain"])]
    if bad_companies:
        logger.info(f"Found {len(bad_companies)} ATS-domain entries to fix: {bad_companies}")
        for company in bad_companies:
            _execute("DELETE FROM company_email_formats WHERE lower(company) = lower(?)", (company,))
            _email_format_cache.pop(company.strip().lower(), None)
            result = _lookup_email_format_via_llm(company)
            if result:
                from db.database import save_email_format as _save
                _save(company, result[0], result[1], source="groq")
                _email_format_cache[company.strip().lower()] = result
                logger.info(f"  Fixed {company}: {result[0]} @ {result[1]}")
            else:
                domain = _guess_domain_fallback(company)
                from db.database import save_email_format as _save
                _save(company, "FL", domain, source="fallback")
                logger.info(f"  Fallback {company}: FL @ {domain}")
            total_fixed += 1
    else:
        logger.info("No ATS email format entries found in company_email_formats")

    # ── 2. Fix leads.job_expected_email for affected companies ───────────────
    for company in bad_companies:
        leads = _fetchall(
            "SELECT id, name, company FROM leads WHERE lower(company) = lower(?)", (company,)
        )
        for lead in leads:
            new_email = _infer_email(lead["name"], lead["company"])
            if new_email:
                _execute(
                    "UPDATE leads SET job_expected_email = ? WHERE id = ?",
                    (new_email, lead["id"]),
                )

    # ── 3. Fix matches.expected_email for any ATS-domain email (always runs) ─
    all_matches = _fetchall(
        "SELECT id, person_name, person_company, expected_email FROM matches "
        "WHERE expected_email IS NOT NULL AND expected_email != ''"
    )
    matches_fixed = 0
    for m in all_matches:
        email = m.get("expected_email", "") or ""
        if "@" not in email:
            continue
        domain = email.split("@", 1)[1]
        if _is_ats_domain(domain):
            new_email = _infer_email(m.get("person_name", ""), m.get("person_company", ""))
            _execute(
                "UPDATE matches SET expected_email = ? WHERE id = ?",
                (new_email or None, m["id"]),
            )
            logger.info(f"  Match {m['id']}: {email!r} → {new_email!r}")
            matches_fixed += 1

    logger.info(
        f"Done: fixed {total_fixed} company format entries, {matches_fixed} match emails"
    )
    return total_fixed + matches_fixed


def _build_query_alumni(company: str, location: str, university_full: str, dept_keyword: str) -> str:
    """site:linkedin.com/in "dept keyword" "Company" "City" "Full University Name" """
    return f'site:linkedin.com/in "{dept_keyword}" "{company}" "{location}" "{university_full}"'


def _build_query_broad(company: str, location: str, dept_keyword: str) -> str:
    """site:linkedin.com/in "dept keyword" "Company" "City" """
    return f'site:linkedin.com/in "{dept_keyword}" "{company}" "{location}"'


# ── Snippet parser (standalone, mirrors matcher._parse_snippet) ───────────────

def _parse_snippet(result: dict, university: str = "", dept_tag: str = "") -> dict | None:
    """
    Parse a raw Serper organic result into a structured lead dict.
    Returns None if the result is not a valid LinkedIn profile URL.
    """
    import re as _re
    raw_name = result.get("name", "")
    url      = result.get("url", result.get("link", ""))
    snippet  = result.get("snippet", "")

    if "linkedin.com/in/" not in url:
        return None

    # Split on | · – -
    title_parts = _re.split(r"\s*[|·–\-]\s*", raw_name)
    title_parts = [p.strip() for p in title_parts if p.strip()]
    title_parts = [p for p in title_parts if p.lower() not in ("linkedin",)]

    name    = title_parts[0] if len(title_parts) >= 1 else ""
    title   = title_parts[1] if len(title_parts) >= 2 else ""
    company = title_parts[2] if len(title_parts) >= 3 else ""

    if not name or len(name.split()) < 1:
        return None

    found_uni = _extract_university(snippet)
    is_alumni = bool(
        university and (
            university.lower() in snippet.lower() or
            (found_uni and university.lower() in found_uni.lower())
        )
    )

    tenure_months            = _extract_tenure(snippet)
    location_city, location_country = _extract_location(snippet)
    linkedin_url             = _re.sub(r"\?.*$", "", url)

    return {
        "name":             name,
        "title":            title,
        "company":          company,
        "university":       found_uni,
        "linkedin_url":     linkedin_url,
        "snippet":          snippet[:300],
        "location_city":    location_city,
        "location_country": location_country,
        "tenure_months":    tenure_months,
        "is_alumni":        is_alumni,
        "dept_tag":         dept_tag,
        "seniority":        _infer_seniority_from_title(title),
        "scraped_rank":     result.get("_rank", 0),
    }


# ── Training file writer ──────────────────────────────────────────────────────

def _save_training_records(
    company:   str,
    dept_name: str,
    location:  str,
    query:     str,
    query_type: str,
    page:      int,
    raw_results: list[dict],
    parsed:    list[dict | None],
    university: str = "",
) -> None:
    TRAINING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRAINING_FILE, "a", encoding="utf-8") as f:
        for raw, parsed_lead in zip(raw_results, parsed):
            record = {
                "company":    company,
                "dept":       dept_name,
                "job_location": location,
                "query_type": query_type,
                "university": university,
                "query":      query,
                "page":       page,
                "raw":        raw,
                "parsed":     parsed_lead,
                "verified":   None,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Main build loop ───────────────────────────────────────────────────────────


_FINANCE_SOURCES = {
    "trackr_summer_internships", "trackr_spring_weeks", "trackr_off_cycle",
    "trackr_industrial_placements", "trackr_grad_programmes", "trackr_events",
}


def _resolve_vertical(company: str) -> str | None:
    """Return 'UK Finance', 'UK Consulting', 'UK Technology', or 'UK Law' based on the company's Trackr jobs."""
    import json as _json
    rows = fetchall(
        "SELECT source, industry, raw FROM jobs WHERE lower(company) = lower(%s) AND source LIKE 'trackr%%' LIMIT 10"
        if USE_POSTGRES else
        "SELECT source, industry, raw FROM jobs WHERE lower(company) = lower(?) AND source LIKE 'trackr%' LIMIT 10",
        (company,),
    )
    for r in rows:
        src = r.get("source") or ""
        ind = (r.get("industry") or "").lower()
        try:
            cats = _json.loads(r.get("raw") or "{}").get("trackr_categories") or []
        except Exception:
            cats = []
        if "Consulting" in cats:
            return "UK Consulting"
        if src in _FINANCE_SOURCES or (src == "trackr" and "finance" in ind):
            return "UK Finance"
        if src == "trackr" and "technology" in ind:
            return "UK Tech"
        if src == "trackr" and "law" in ind:
            return "UK Law"
    return None


def build_leads(
    company_filter: str = "",
    university:     str = "",
    dry_run:        bool = False,
    max_companies:  int = 0,
    days_back:      int = 0,
    top_n:          int = 50,
    force:          bool = False,
) -> int:
    """
    Main entry point. Fetches leads for the top_n most recently opened Trackr companies.
    Returns total leads upserted.

    - Selects the top_n distinct companies ordered by most recent opening_date.
    - For each company/dept pair: runs Query B (broad) + one query per Russell Group uni.
    - Parses snippets, stores in leads table, deduplicates by linkedin_url.

    top_n: number of most-recent companies to process (default 50).
    days_back: if > 0, also filter to jobs opened in the last N days (additive).
    """
    from config.settings import RUSSELL_GROUP_UNIS

    matcher = LinkedInMatcher()
    if not matcher.serper_key and not matcher.searxng_url and not matcher.brave_key and not matcher.serp_key:
        logger.warning("No search backend configured — will use DDG (free fallback)")
    elif not matcher.serper_key:
        logger.info(f"Serper key not set — using {'SearXNG' if matcher.searxng_url else 'DDG'} backend")

    # Select top_n most recent distinct companies from trackr
    where_clauses = ["source = 'trackr'", "lower(company) != 'trackr'", "company IS NOT NULL", "company != ''"]
    params: list = []
    if company_filter:
        where_clauses.append("lower(company) = lower(?)" if not USE_POSTGRES else "lower(company) = lower(%s)")
        params.append(company_filter)
    if days_back > 0:
        if USE_POSTGRES:
            where_clauses.append(f"opening_date <> '' AND opening_date::date >= CURRENT_DATE - INTERVAL '{days_back} days'")
        else:
            where_clauses.append(f"opening_date >= date('now', '-{days_back} days')")
    where = " WHERE " + " AND ".join(where_clauses)

    if USE_POSTGRES:
        rows = fetchall(
            f"SELECT DISTINCT ON (lower(company)) company, title, location, url, opening_date "
            f"FROM jobs{where} "
            f"ORDER BY lower(company), opening_date DESC NULLS LAST",
            params or None,
        )
    else:
        rows = fetchall(
            f"SELECT company, title, location, url, MAX(opening_date) AS opening_date "
            f"FROM jobs{where} "
            f"GROUP BY lower(company) "
            f"ORDER BY MAX(opening_date) DESC",
            params or None,
        )
    rows = sorted(rows, key=lambda r: r.get("opening_date") or "", reverse=True)
    if top_n > 0 and not company_filter:
        rows = rows[:top_n]

    if not rows:
        logger.warning("No jobs found in DB — run scraper first")
        return 0

    logger.info(f"Building leads for {len(rows)} company/title rows")
    total_upserted = 0
    seen_pairs: set = set()  # (company, dept) to avoid duplicate crawl in same run

    uni_full = _full_uni_name(university) if university else ""

    for row in rows:
        company      = (row.get("company") or "").strip()
        job_title    = (row.get("title") or "").strip()
        job_url      = (row.get("url") or "").strip()
        location     = (row.get("location") or "").strip()
        opening_date = (row.get("opening_date") or "").strip()

        if not company:
            continue

        # Use raw jobs.location; infer from URL if blank
        if not location:
            location = _infer_location_from_url(job_url)
        # Translate region codes to cities for Serper query (needed for search quality)
        search_location = REGION_LOCATION_FALLBACK.get(location, location) or "London"

        dept_name    = _dept_from_title(job_title)
        dept_keyword = _search_keyword_from_title(job_title, dept_name)

        pair = (company.lower(), dept_name)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        # Skip if leads already exist in DB for this (company, dept) pair
        if not dry_run and not force:
            existing = fetchall(
                "SELECT 1 FROM leads WHERE lower(company)=lower(?) AND dept_tag=? LIMIT 1",
                (company, dept_name),
            )
            if existing:
                logger.info(f"  Skipping {company} / {dept_name} — leads already in DB")
                continue

        if max_companies and len(seen_pairs) > max_companies:
            logger.info(f"Reached max_companies={max_companies}, stopping.")
            break

        # Query A — alumni first (only when university provided)
        leads_a: list[dict] = []
        if uni_full:
            query_a = _build_query_alumni(company, search_location, uni_full, dept_keyword)
            logger.info(f"  Query A: {query_a[:100]}")
            raw_a_p1 = matcher._search(query_a, count=10, page=1)
            raw_a_p2 = matcher._search(query_a, count=10, page=2)
            for i, r in enumerate(raw_a_p1): r["_rank"] = i + 1
            for i, r in enumerate(raw_a_p2): r["_rank"] = i + 11
            raw_a    = _dedup(raw_a_p1 + raw_a_p2)
            parsed_a = [_parse_snippet(r, university=university, dept_tag=dept_name) for r in raw_a]
            leads_a  = [l for l in parsed_a if l]
            if not dry_run:
                _save_training_records(company, dept_name, location, query_a, "alumni", 1, raw_a_p1, [_parse_snippet(r, university, dept_name) for r in raw_a_p1], university)
                _save_training_records(company, dept_name, location, query_a, "alumni", 2, raw_a_p2, [_parse_snippet(r, university, dept_name) for r in raw_a_p2], university)

        # Query B — broad (dept + company + city, no university)
        query_b = _build_query_broad(company, search_location, dept_keyword)
        logger.info(f"  Query B: {query_b[:100]}")
        try:
            raw_b_p1 = matcher._search(query_b, count=10, page=1)
            raw_b_p2 = matcher._search(query_b, count=10, page=2)
        except RuntimeError as e:
            if "SERPER_CREDITS_EXHAUSTED" in str(e):
                logger.critical("🚨 SERPER CREDITS EXHAUSTED during Query B — stopping")
                logger.info(f"Lead build stopped early — {total_upserted} leads upserted so far")
                return total_upserted
            raise
        for i, r in enumerate(raw_b_p1): r["_rank"] = i + 1
        for i, r in enumerate(raw_b_p2): r["_rank"] = i + 11
        raw_b    = _dedup(raw_b_p1 + raw_b_p2)
        parsed_b = [_parse_snippet(r, university=university, dept_tag=dept_name) for r in raw_b]
        leads_b  = [l for l in parsed_b if l]
        if not dry_run:
            _save_training_records(company, dept_name, location, query_b, "broad", 1, raw_b_p1, [_parse_snippet(r, university, dept_name) for r in raw_b_p1], university)
            _save_training_records(company, dept_name, location, query_b, "broad", 2, raw_b_p2, [_parse_snippet(r, university, dept_name) for r in raw_b_p2], university)

        # Query RG — one query per Russell Group university
        leads_rg: list[dict] = []
        for rg_uni in RUSSELL_GROUP_UNIS:
            query_rg = _build_query_alumni(company, search_location, rg_uni, dept_keyword)
            logger.debug(f"  Query RG ({rg_uni}): {query_rg[:100]}")
            try:
                raw_rg_p1 = matcher._search(query_rg, count=10, page=1)
                raw_rg_p2 = matcher._search(query_rg, count=10, page=2)
            except RuntimeError as e:
                if "SERPER_CREDITS_EXHAUSTED" in str(e):
                    logger.critical("🚨 SERPER CREDITS EXHAUSTED during RG queries — stopping")
                    logger.info(f"Lead build stopped early — {total_upserted} leads upserted so far")
                    return total_upserted
                raise
            for i, r in enumerate(raw_rg_p1): r["_rank"] = i + 1
            for i, r in enumerate(raw_rg_p2): r["_rank"] = i + 11
            raw_rg = _dedup(raw_rg_p1 + raw_rg_p2)
            parsed_rg = [_parse_snippet(r, university=rg_uni, dept_tag=dept_name) for r in raw_rg]
            leads_rg.extend([l for l in parsed_rg if l])
        logger.info(f"  {company} / {dept_name}: {len(leads_rg)} RG leads across {len(RUSSELL_GROUP_UNIS)} unis")

        # Merge A + B + RG, deduplicate by linkedin_url
        # Stamp job metadata onto each lead — snippet parsing can't reliably extract these
        _REGION_COUNTRY = {"UK": "united kingdom", "US": "united states"}
        all_leads: dict[str, dict] = {}
        for lead in leads_a + leads_b + leads_rg:
            # Fix A: skip if the parsed company from the LinkedIn title doesn't match
            # the company we searched for (catches location/name collisions, e.g. Jonathan
            # Erbe at LSEG appearing in a Brookfield search because "Brookfield, WI" is
            # his location).
            #
            # When only 2 title parts were found (name | employer | LinkedIn), the employer
            # lands in lead["title"] and lead["company"] is empty. Fall back to title for
            # validation in that case.
            parsed_co = lead.get("company", "") or lead.get("title", "")
            if parsed_co and not _company_name_overlap(parsed_co, company):
                logger.debug(
                    f"  Skipping {lead.get('name')!r}: parsed company {parsed_co!r} "
                    f"doesn't overlap with search company {company!r}"
                )
                continue

            # Fix D: skip if the person's own name contains a word that IS the entire
            # company name (or vice versa) — they were likely surfaced because Google
            # matched their name against the company query
            # (e.g. "Anton K." found for company "Anton").
            # Only fires when the company name is a single significant word (i.e. is short
            # enough to also be a personal name), to avoid false-positives on multi-word
            # company names like "Goldman Sachs" where "Goldman" could be a surname.
            company_stripped = company.strip().lower()
            company_words = [w for w in company_stripped.split() if w not in _CO_STOPWORDS]
            if len(company_words) == 1:
                person_name_words = set(lead.get("name", "").lower().split())
                if company_words[0] in person_name_words:
                    logger.debug(
                        f"  Skipping {lead.get('name')!r}: person name contains "
                        f"single-word company name {company!r}"
                    )
                    continue

            # Fix B: skip if the highlighted role in the snippet is a past role
            # (closed date range like "Aug 2015 – Sep 2015"), which means Google
            # found them via a historical position, not their current employer.
            if _snippet_role_is_past(lead.get("snippet", "")):
                logger.debug(
                    f"  Skipping {lead.get('name')!r}: snippet shows past role only"
                )
                continue

            lead["company"]            = company       # always the company we searched for
            lead["job_title"]          = job_title     # the job title that triggered this search
            lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
            lead["job_opening_date"]   = opening_date  # opening date from jobs table
            # Use region only to fill location_country when snippet didn't find one.
            # Never overwrite location_city — the snippet parser extracts actual cities
            # (e.g. "London"); replacing with "UK" causes 0/25 pts in the scorer.
            if not lead.get("location_country") and location in _REGION_COUNTRY:
                lead["location_country"] = _REGION_COUNTRY[location]
            lead["lead_type"] = _classify_lead_type(lead.get("title", ""), dept_name)
            url = lead.get("linkedin_url", "")
            if url and url not in all_leads:
                all_leads[url] = lead

        logger.info(f"  {company} / {dept_name}: {len(all_leads)} relevant leads (tier 1)")

        # ── Tier 2: Senior / Exec ────────────────────────────────────────────────
        for exec_kw in ["director", "partner"]:
            q_exec = f'site:linkedin.com/in "{exec_kw}" "{company}" "{search_location}"'
            for pg in [1, 2]:
                try:
                    raw_exec = matcher._search(q_exec, count=10, page=pg)
                except RuntimeError as e:
                    if "SERPER_CREDITS_EXHAUSTED" in str(e):
                        logger.critical("🚨 SERPER CREDITS EXHAUSTED during exec queries — stopping")
                        return total_upserted
                    logger.warning(f"  Exec query failed: {e}")
                    continue
                for i, r in enumerate(raw_exec): r["_rank"] = i + 1
                for r in _dedup(raw_exec):
                    lead = _parse_snippet(r, university="", dept_tag=dept_name)
                    if not lead: continue
                    parsed_co = lead.get("company", "") or lead.get("title", "")
                    if parsed_co and not _company_name_overlap(parsed_co, company): continue
                    if _snippet_role_is_past(lead.get("snippet", "")): continue
                    lead["company"] = company
                    lead["job_title"] = job_title
                    lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
                    lead["job_opening_date"] = opening_date
                    lead["lead_type"] = "exec"
                    if not lead.get("location_country") and location in _REGION_COUNTRY:
                        lead["location_country"] = _REGION_COUNTRY[location]
                    url = lead.get("linkedin_url", "")
                    if url and url not in all_leads:
                        all_leads[url] = lead

        # ── Tier 3: HR / Recruiter ───────────────────────────────────────────────
        q_hr = f'site:linkedin.com/in "recruiter" "{company}" "{search_location}"'
        for pg in [1, 2]:
            try:
                raw_hr = matcher._search(q_hr, count=10, page=pg)
            except RuntimeError as e:
                if "SERPER_CREDITS_EXHAUSTED" in str(e):
                    logger.critical("🚨 SERPER CREDITS EXHAUSTED during HR queries — stopping")
                    return total_upserted
                logger.warning(f"  HR query failed: {e}")
                continue
            for i, r in enumerate(raw_hr): r["_rank"] = i + 1
            for r in _dedup(raw_hr):
                lead = _parse_snippet(r, university="", dept_tag="hr")
                if not lead: continue
                parsed_co = lead.get("company", "") or lead.get("title", "")
                if parsed_co and not _company_name_overlap(parsed_co, company): continue
                lead["company"] = company
                lead["job_title"] = job_title
                lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
                lead["job_opening_date"] = opening_date
                lead["lead_type"] = "hr"
                if not lead.get("location_country") and location in _REGION_COUNTRY:
                    lead["location_country"] = _REGION_COUNTRY[location]
                url = lead.get("linkedin_url", "")
                if url and url not in all_leads:
                    all_leads[url] = lead

        # ── Tier 4: General fallback (only if < 25 total leads) ─────────────────
        if len(all_leads) < 25:
            q_gen = f'site:linkedin.com/in "{company}" "{search_location}"'
            for pg in [1, 2, 3]:
                if len(all_leads) >= 25:
                    break
                try:
                    raw_gen = matcher._search(q_gen, count=10, page=pg)
                except RuntimeError as e:
                    if "SERPER_CREDITS_EXHAUSTED" in str(e):
                        logger.critical("🚨 SERPER CREDITS EXHAUSTED during general fallback — stopping")
                        return total_upserted
                    logger.warning(f"  General fallback query failed: {e}")
                    continue
                for i, r in enumerate(raw_gen): r["_rank"] = i + 1
                for r in _dedup(raw_gen):
                    lead = _parse_snippet(r, university="", dept_tag="general")
                    if not lead: continue
                    parsed_co = lead.get("company", "") or lead.get("title", "")
                    if parsed_co and not _company_name_overlap(parsed_co, company): continue
                    if _snippet_role_is_past(lead.get("snippet", "")): continue
                    lead["company"] = company
                    lead["job_title"] = job_title
                    lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
                    lead["job_opening_date"] = opening_date
                    lead["lead_type"] = "general"
                    if not lead.get("location_country") and location in _REGION_COUNTRY:
                        lead["location_country"] = _REGION_COUNTRY[location]
                    url = lead.get("linkedin_url", "")
                    if url and url not in all_leads:
                        all_leads[url] = lead

        # ── Tier 5: No-location fallback (small/niche companies with 0 leads) ────
        if len(all_leads) < 5:
            q_noloc = f'site:linkedin.com/in "{dept_keyword}" "{company}"'
            logger.info(f"  Tier 5 (no-location fallback): {q_noloc[:100]}")
            for pg in [1, 2, 3]:
                if len(all_leads) >= 25:
                    break
                try:
                    raw_noloc = matcher._search(q_noloc, count=10, page=pg)
                except RuntimeError as e:
                    if "SERPER_CREDITS_EXHAUSTED" in str(e):
                        logger.critical("🚨 SERPER CREDITS EXHAUSTED during no-location fallback — stopping")
                        return total_upserted
                    logger.warning(f"  No-location fallback query failed: {e}")
                    break
                for i, r in enumerate(raw_noloc): r["_rank"] = i + 1
                for r in _dedup(raw_noloc):
                    lead = _parse_snippet(r, university="", dept_tag=dept_name)
                    if not lead: continue
                    parsed_co = lead.get("company", "") or lead.get("title", "")
                    if parsed_co and not _company_name_overlap(parsed_co, company): continue
                    if _snippet_role_is_past(lead.get("snippet", "")): continue
                    lead["company"] = company
                    lead["job_title"] = job_title
                    lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
                    lead["job_opening_date"] = opening_date
                    lead["lead_type"] = _classify_lead_type(lead.get("title", ""), dept_name)
                    url = lead.get("linkedin_url", "")
                    if url and url not in all_leads:
                        all_leads[url] = lead
            # Also try pure company name with no keywords
            if len(all_leads) < 5:
                q_bare = f'site:linkedin.com/in "{company}"'
                logger.info(f"  Tier 5b (bare company): {q_bare}")
                for pg in [1, 2]:
                    if len(all_leads) >= 25:
                        break
                    try:
                        raw_bare = matcher._search(q_bare, count=10, page=pg)
                    except RuntimeError as e:
                        if "SERPER_CREDITS_EXHAUSTED" in str(e):
                            return total_upserted
                        break
                    for i, r in enumerate(raw_bare): r["_rank"] = i + 1
                    for r in _dedup(raw_bare):
                        lead = _parse_snippet(r, university="", dept_tag="general")
                        if not lead: continue
                        parsed_co = lead.get("company", "") or lead.get("title", "")
                        if parsed_co and not _company_name_overlap(parsed_co, company): continue
                        if _snippet_role_is_past(lead.get("snippet", "")): continue
                        lead["company"] = company
                        lead["job_title"] = job_title
                        lead["job_expected_email"] = _infer_email(lead.get("name", ""), company)
                        lead["job_opening_date"] = opening_date
                        lead["lead_type"] = _classify_lead_type(lead.get("title", ""), dept_name)
                        url = lead.get("linkedin_url", "")
                        if url and url not in all_leads:
                            all_leads[url] = lead

        # ── Tier 6: No-company broadened search (< 5 leads after all above) ────
        # For very small / niche companies with no indexed LinkedIn presence, drop
        # the company name entirely and search by role + city so students still get
        # relevant contacts to reach out to.
        if len(all_leads) < 5:
            q_broad = f'site:linkedin.com/in "{dept_keyword}" "{search_location}"'
            logger.info(f"  Tier 6 (broadened, no company): {q_broad[:100]}")
            for pg in [1, 2]:
                if len(all_leads) >= 25:
                    break
                try:
                    raw_broad = matcher._search(q_broad, count=10, page=pg)
                except RuntimeError as e:
                    if "SERPER_CREDITS_EXHAUSTED" in str(e):
                        logger.critical("🚨 SERPER CREDITS EXHAUSTED during broadened fallback — stopping")
                        return total_upserted
                    logger.warning(f"  Tier 6 query failed: {e}")
                    break
                for i, r in enumerate(raw_broad):
                    r["_rank"] = i + 1
                for r in _dedup(raw_broad):
                    lead = _parse_snippet(r, university="", dept_tag=dept_name)
                    if not lead:
                        continue
                    if _snippet_role_is_past(lead.get("snippet", "")):
                        continue
                    lead["company"]            = company
                    lead["job_title"]          = job_title
                    lead["job_expected_email"] = ""
                    lead["job_opening_date"]   = opening_date
                    lead["lead_type"]          = _classify_lead_type(lead.get("title", ""), dept_name)
                    if not lead.get("location_country") and location in _REGION_COUNTRY:
                        lead["location_country"] = _REGION_COUNTRY[location]
                    url = lead.get("linkedin_url", "")
                    if url and url not in all_leads:
                        all_leads[url] = lead

        logger.info(f"  {company} / {dept_name}: {len(all_leads)} total leads (all tiers)")

        # Cap "relevant" leads at 20% of total — demote excess to "general"
        all_leads_list = list(all_leads.values())
        relevant_leads = [l for l in all_leads_list if l.get("lead_type") == "relevant"]
        max_relevant   = max(1, round(len(all_leads_list) * 0.20))
        if len(relevant_leads) > max_relevant:
            for lead in relevant_leads[max_relevant:]:
                lead["lead_type"] = "general"
            logger.info(f"  {company}: capped relevant leads {len(relevant_leads)} → {max_relevant} (20% of {len(all_leads_list)})")

        # Resolve vertical for this company from its Trackr job sources
        vertical = _resolve_vertical(company)

        if not dry_run:
            for lead in all_leads.values():
                lead["vertical"] = vertical
                try:
                    upsert_lead(lead)
                    total_upserted += 1
                except Exception as e:
                    logger.warning(f"  upsert_lead failed: {e}")

    logger.info(f"Lead build complete — {total_upserted} leads upserted")
    return total_upserted


class SerperCreditsExhausted(Exception):
    pass


def _dedup(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            out.append(r)
    return out


def cleanup_stale_leads() -> int:
    """
    Mark existing leads stale where the snippet shows a past (closed date range) role.
    Uses Fix B logic (_snippet_role_is_past) on all leads without stale_after set.
    Returns count of leads marked stale.
    """
    from db.database import fetchall as _fetchall, execute as _execute
    leads = _fetchall(
        "SELECT id, name, company, snippet FROM leads WHERE stale_after > NOW()"
    )
    marked = 0
    for lead in leads:
        snippet = lead.get("snippet") or ""
        if _snippet_role_is_past(snippet):
            _execute(
                "UPDATE leads SET stale_after = NOW() WHERE id = ?",  # expires immediately
                (lead["id"],),
            )
            logger.info(
                f"  Marked stale: {lead.get('name')!r} @ {lead.get('company')!r} "
                f"(id={lead['id']})"
            )
            marked += 1
    logger.info(f"cleanup_stale_leads: {marked} leads marked stale out of {len(leads)} checked")
    return marked


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build leads pool from Google/Serper")
    parser.add_argument("--company",    default="", help="Filter to a single company name")
    parser.add_argument("--university", default="", help="University to use for alumni Query A")
    parser.add_argument("--dry-run",            action="store_true", help="Parse only, no DB writes")
    parser.add_argument("--max-companies",      type=int, default=0, help="Stop after N unique company/dept pairs")
    parser.add_argument("--days-back",          type=int, default=0, help="Only include jobs posted in the last N days")
    parser.add_argument("--cleanup-stale-leads", action="store_true", help="Mark existing leads with past-role snippets as stale")
    parser.add_argument("--force", action="store_true", help="Re-scrape even if leads already exist for a company/dept pair")
    args = parser.parse_args()

    if args.cleanup_stale_leads:
        from db.database import init_db
        init_db()
        n = cleanup_stale_leads()
        print(f"Done — {n} leads marked stale")
    else:
        n = build_leads(
            company_filter = args.company,
            university     = args.university,
            dry_run        = args.dry_run,
            max_companies  = args.max_companies,
            days_back      = args.days_back,
            force          = args.force,
        )
        print(f"Done — {n} leads upserted")
