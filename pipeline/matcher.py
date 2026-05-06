"""
inroad Backend — LinkedIn Lead Matcher (Phase 3)

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
SERPER_ENDPOINT  = "https://google.serper.dev/search"

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
    "york":             ["university of york", "york university"],
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
    "unc":              ["university of north carolina", "unc chapel hill", "chapel hill"],
    "usc":              ["university of southern california", "usc"],
    "gatech":           ["georgia institute of technology", "georgia tech", "gatech"],
    "purdue":           ["purdue university", "purdue"],
    "tufts":            ["tufts university", "tufts"],
    "boston_u":         ["boston university"],
    "northeastern":     ["northeastern university", "northeastern"],
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
        self.pdl_key     = os.environ.get("PDL_API_KEY", "")       # primary — education data
        self.apollo_key  = os.environ.get("APOLLO_API_KEY", "")    # secondary — verified data
        self.hunter_key  = os.environ.get("HUNTER_API_KEY", "")    # tertiary — domain people search
        self.serper_key   = os.environ.get("SERPER_API_KEY", "")    # primary
        self.serper_key_2 = os.environ.get("SERPER_API_KEY_2", "") # backup 1
        self.serper_key_3 = os.environ.get("SERPER_API_KEY_3", "") # backup 2
        self.brave_key   = os.environ.get("BRAVE_SEARCH_API_KEY", "") # fallback
        self.serp_key    = os.environ.get("SERPAPI_KEY", "")       # fallback
        # Warn if someone has the old dead Bing key set
        if os.environ.get("BING_SEARCH_API_KEY"):
            logger.warning(
                "BING_SEARCH_API_KEY is set but the Bing Search API v7 was "
                "decommissioned on August 11 2025 and no longer works. "
                "Set SERPER_API_KEY instead — free trial at https://serper.dev"
            )
        self._last_req      = 0.0
        self._pdl_cache     = {}   # company_lower → list[dict], avoids duplicate PDL calls
        self._snippet_cache = {}   # company_lower → list[dict], one Serper call per company

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
        Apollo is used as primary source if APOLLO_API_KEY is set — returns verified
        current employer and education history for reliable alumni detection.
        Falls back to Serper/Brave/SerpAPI snippet search if Apollo unavailable.
        Each lead: {name, title, company, university, linkedin_url,
                    tenure_months, is_alumni, snippet, email}
        """
        all_leads = []
        seen_urls = set()

        # ── PDL primary path (education history mandatory) ───────────────────
        if self.pdl_key:
            pdl_leads = self._pdl_search(company, job_title, student_university, n=n * 2)
            for lead in pdl_leads:
                url = lead.get("linkedin_url", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url or lead["name"])
                all_leads.append(lead)

            if len(all_leads) >= 3:
                logger.info(f"PDL returned {len(all_leads)} leads for {company}")
                return all_leads[:n]

        # ── Apollo secondary path (verified data) ────────────────────────────
        if self.apollo_key:
            raw_people = self._apollo_search(company, job_title, n=n * 2)
            for person in raw_people:
                lead = self._apollo_person_to_lead(person)
                if not lead:
                    continue
                url = lead.get("linkedin_url", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                # Alumni: check every education entry against student university
                edu_list = lead.pop("education", [])
                lead["is_alumni"] = any(
                    is_alumni(edu.get("school_name", ""), student_university)
                    for edu in edu_list
                )
                # Use first education entry as university string
                if edu_list:
                    lead["university"] = edu_list[0].get("school_name", "")
                all_leads.append(lead)

            if len(all_leads) >= 3:
                logger.info(f"Apollo returned {len(all_leads)} leads for {company}")
                return all_leads[:n]

        # ── Hunter.io domain search (secondary) ─────────────────────────────
        if self.hunter_key and len(all_leads) < 3:
            hunter_leads = self._hunter_search(company, job_title, student_university, n=n * 2)
            for lead in hunter_leads:
                url = lead.get("linkedin_url", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url or lead["name"])
                all_leads.append(lead)

            if len(all_leads) >= 3:
                logger.info(f"Hunter returned {len(all_leads)} leads for {company}")
                return all_leads[:n]

        # ── Fallback: snippet search (Serper / Brave / SerpAPI) ─────────────
        # Only fires when primary sources (PDL/Apollo) returned nothing at all.
        # One call per company max — results cached for the session lifetime.
        if not (self.serper_key or self.brave_key or self.serp_key):
            logger.warning(
                "No search API key set. Set SERPER_API_KEY as fallback."
            )
            return all_leads[:n]

        cache_key = company.lower().strip()
        if cache_key in self._snippet_cache:
            cached = self._snippet_cache[cache_key]
            logger.info(f"Snippet cache hit for '{company}' ({len(cached)} leads)")
            for lead in cached:
                url = lead.get("linkedin_url", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url or lead["name"])
                lead["is_alumni"] = is_alumni(lead.get("university", ""), student_university)
                all_leads.append(lead)
            return all_leads[:n]

        # Single query — use the primary company name variant only
        query = build_search_query(company, job_title, student_university)
        logger.info(f"Snippet search (1 call): {query}")

        results = []
        if self.serper_key:
            results = self._serper_search(query, count=10)
        elif self.brave_key:
            results = self._brave_search(query, count=10)
        elif self.serp_key:
            results = self._serp_search(query, n=10)

        fresh_leads = []
        for r in results:
            lead = self._parse_snippet(r)
            if not lead:
                continue
            lead["location_country"] = ""
            lead["location_city"]    = ""
            lead["tenure_months"]    = 0
            url = lead.get("linkedin_url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            lead["is_alumni"] = is_alumni(lead.get("university", ""), student_university)
            lead["email"] = ""
            fresh_leads.append(lead)
            all_leads.append(lead)

        self._snippet_cache[cache_key] = fresh_leads  # cache regardless of count
        return all_leads[:n]

    # ── Apollo People Search backend ─────────────────────────────────────────
    APOLLO_ENDPOINT = "https://api.apollo.io/api/v1/mixed_people/search"

    def _apollo_search(self, company: str, job_title: str, n: int = 10) -> list[dict]:
        """
        Search Apollo People API for professionals at `company` relevant to `job_title`.
        Auth via x-api-key header (URL param is deprecated per Apollo's notice).
        Returns raw Apollo person dicts.
        """
        import urllib.request as _urlreq

        team_kw = _extract_team_keywords(job_title)
        title_filters = team_kw[:2] + ["analyst", "associate", "manager", "vice president"]

        payload = json.dumps({
            "organization_names": [company],
            "person_titles":      title_filters,
            "page":               1,
            "per_page":           min(n, 10),
        }).encode()

        req = _urlreq.Request(
            self.APOLLO_ENDPOINT,
            data=payload,
            headers={
                "x-api-key":     self.apollo_key,
                "Content-Type":  "application/json",
                "accept":        "application/json",
            },
            method="POST",
        )
        try:
            with _urlreq.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            people = data.get("people", [])
            logger.info(f"Apollo: {len(people)} results for '{company}'")
            return people
        except Exception as e:
            logger.error(f"Apollo search failed: {e}")
            return []

    def _apollo_person_to_lead(self, person: dict) -> dict | None:
        """Convert Apollo API person dict → internal lead dict format."""
        name = (person.get("name") or "").strip()
        if not name:
            return None

        linkedin_url = person.get("linkedin_url") or ""
        if linkedin_url:
            linkedin_url = re.sub(r"\?.*$", "", linkedin_url)

        return {
            "name":          name,
            "title":         person.get("title") or "",
            "company":       person.get("organization_name") or "",
            "university":    "",        # populated from education[] in find_leads()
            "linkedin_url":  linkedin_url,
            "tenure_months": 0,         # Apollo doesn't expose tenure reliably
            "is_alumni":     False,     # set in find_leads() using education[]
            "snippet":       "",
            "email":         person.get("email") or "",
            "education":     person.get("education") or [],
        }

    # ── PDL (People Data Labs) ────────────────────────────────────────────────

    PDL_ENDPOINT = "https://api.peopledatalabs.com/v5/person/search"

    def _pdl_search(
        self,
        company:            str,
        job_title:          str,
        student_university: str = "",
        n:                  int = 10,
    ) -> list[dict]:
        """
        Search PDL Person Search API for employees at `company` relevant to `job_title`.
        Results are cached per company for the lifetime of this matcher instance to
        avoid burning quota when multiple students match the same company.
        Returns leads including education[] for reliable alumni detection.
        """
        import urllib.request as _urlreq

        cache_key = company.lower().strip()
        if cache_key in self._pdl_cache:
            logger.info(f"PDL cache hit for '{company}'")
            return self._pdl_cache[cache_key][:n]

        team_kw  = _extract_team_keywords(job_title)
        # Use title role keywords + seniority levels that are 1-3 levels above student
        title_sql_parts = []
        for kw in team_kw[:2]:
            title_sql_parts.append(f"job_title LIKE '%{kw}%'")
        seniority_levels = ["'analyst'", "'associate'", "'manager'", "'director'", "'vp'", "'vice president'"]
        company_clean = company.replace("'", "\\'")

        sql = (
            f"SELECT * FROM person "
            f"WHERE job_company_name='{company_clean}' "
            f"AND job_title_levels IN ('senior', 'manager', 'director', 'vp', 'cxo')"
        )

        payload = json.dumps({"sql": sql, "size": min(n, 25)}).encode()
        req = _urlreq.Request(
            self.PDL_ENDPOINT,
            data=payload,
            headers={
                "X-Api-Key":     self.pdl_key,
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
            method="POST",
        )
        try:
            with _urlreq.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            logger.error(f"PDL search failed for '{company}': {e}")
            return []

        records = data.get("data", [])
        if not records:
            # Retry with looser seniority (include mid-level)
            sql2 = (
                f"SELECT * FROM person "
                f"WHERE job_company_name='{company_clean}'"
            )
            try:
                payload2 = json.dumps({"sql": sql2, "size": min(n, 25)}).encode()
                req2 = _urlreq.Request(
                    self.PDL_ENDPOINT, data=payload2,
                    headers={"X-Api-Key": self.pdl_key, "Content-Type": "application/json", "Accept": "application/json"},
                    method="POST",
                )
                with _urlreq.urlopen(req2, timeout=15) as resp2:
                    data2 = json.loads(resp2.read())
                records = data2.get("data", [])
            except Exception as e:
                logger.error(f"PDL retry failed for '{company}': {e}")

        leads = []
        for rec in records:
            name = (rec.get("full_name") or "").strip()
            if not name:
                continue

            linkedin_url = rec.get("linkedin_url") or ""
            if linkedin_url and not linkedin_url.startswith("http"):
                linkedin_url = "https://" + linkedin_url
            if linkedin_url:
                linkedin_url = re.sub(r"\?.*$", "", linkedin_url)

            # Education — PDL education[].school.name + degrees[] + end_date
            raw_edu = rec.get("education") or []
            education = [
                {
                    "school_name": (e.get("school") or {}).get("name", ""),
                    "degree":      (e.get("degrees") or [""])[0],
                    "end_date":    e.get("end_date", ""),
                }
                for e in raw_edu
                if (e.get("school") or {}).get("name")
            ]

            # Best email if present (PDL can return bool or list)
            emails = rec.get("emails")
            email = ""
            if isinstance(emails, list) and emails:
                first_email = emails[0]
                if isinstance(first_email, dict):
                    email = first_email.get("address", "")
                elif isinstance(first_email, str):
                    email = first_email

            # Tenure — calculate from job_start_date if present
            tenure_months = 0
            job_start = rec.get("job_start_date") or ""
            if job_start:
                try:
                    parts = job_start.split("-")
                    start_year  = int(parts[0])
                    start_month = int(parts[1]) if len(parts) > 1 else 1
                    today = date.today()
                    tenure_months = (today.year - start_year) * 12 + (today.month - start_month)
                    tenure_months = max(0, tenure_months)
                except Exception:
                    tenure_months = 0

            lead = {
                "name":             name,
                "title":            rec.get("job_title") or "",
                "company":          rec.get("job_company_name") or company,
                "university":       education[0]["school_name"] if education else "",
                "linkedin_url":     linkedin_url,
                "tenure_months":    tenure_months,
                "location_country": (rec.get("location_country") or "").lower().strip(),
                "location_city":    (rec.get("location_locality") or "").lower().strip(),
                "is_alumni":        any(
                    is_alumni(e["school_name"], student_university)
                    for e in education
                ),
                "snippet":          "",
                "email":            email,
                "education":        education,
            }
            leads.append(lead)

        logger.info(f"PDL: {len(leads)} leads for '{company}' (used 1 API call)")
        self._pdl_cache[cache_key] = leads   # cache to protect quota
        return leads[:n]

    # ── Hunter.io domain people search ───────────────────────────────────────

    def _get_company_domain(self, company: str) -> str:
        """
        Resolve company name → email domain.
        1. Try Apollo organizations/search (already works on free plan).
        2. Fall back to naive slug (goldmansachs.com).
        """
        import urllib.request as _urlreq
        UA = "Mozilla/5.0 (compatible; inroad/1.0)"
        if self.apollo_key:
            try:
                payload = json.dumps({"q_organization_name": company, "per_page": 1}).encode()
                req = _urlreq.Request(
                    "https://api.apollo.io/api/v1/organizations/search",
                    data=payload,
                    headers={
                        "x-api-key": self.apollo_key,
                        "Content-Type": "application/json",
                        "accept": "application/json",
                        "User-Agent": UA,
                    },
                    method="POST",
                )
                with _urlreq.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                orgs = data.get("organizations", [])
                if orgs:
                    domain = orgs[0].get("primary_domain") or ""
                    if domain:
                        logger.info(f"Domain for '{company}': {domain}")
                        return domain
            except Exception as e:
                logger.debug(f"Apollo org search for domain failed: {e}")

        # Naive fallback
        slug = re.sub(r"[^a-z0-9]", "", company.lower().strip())
        return f"{slug}.com" if slug else ""

    def _hunter_search(
        self,
        company:            str,
        job_title:          str,
        student_university: str = "",
        n:                  int = 10,
    ) -> list[dict]:
        """
        Use Hunter.io domain search to find people at a company.
        Returns leads in the standard internal dict format.
        Hunter returns name, title, email, and sometimes LinkedIn URL per person.
        """
        domain = self._get_company_domain(company)
        if not domain:
            return []

        import urllib.request as _urlreq, urllib.parse as _uparse
        url = (
            f"https://api.hunter.io/v2/domain-search"
            f"?domain={_uparse.quote(domain)}"
            f"&limit={min(n, 100)}"
            f"&api_key={self.hunter_key}"
        )
        try:
            req = _urlreq.Request(url, headers={"Accept": "application/json"})
            with _urlreq.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except _urlreq.HTTPError as e:
            if e.code in (401, 402):
                logger.warning("Hunter.io credits exhausted or unauthorised — disabling for this session")
                self.hunter_key = ""
            else:
                logger.error(f"Hunter domain search failed for {domain}: {e}")
            return []
        except Exception as e:
            logger.error(f"Hunter domain search failed for {domain}: {e}")
            return []

        # Detect out-of-credits in JSON response body
        errors = data.get("errors") or []
        if any(e.get("id") in ("no_credits_left", "payment_required") for e in errors):
            logger.warning("Hunter.io credits exhausted — disabling for this session")
            self.hunter_key = ""
            return []

        emails = data.get("data", {}).get("emails", [])
        if not emails:
            logger.info(f"Hunter: 0 results for {domain}")
            return []

        # Extract job-relevant keywords to filter by title
        team_kw = _extract_team_keywords(job_title)

        leads = []
        for entry in emails:
            first = (entry.get("first_name") or "").strip()
            last  = (entry.get("last_name") or "").strip()
            if not first or not last:
                continue

            title   = (entry.get("position") or "").strip()
            email   = (entry.get("value") or "").strip()
            linkedin = (entry.get("linkedin") or "").strip()
            if linkedin:
                linkedin = re.sub(r"\?.*$", "", linkedin)

            # Soft title filter — prefer relevant titles but don't discard all others
            title_lower = title.lower()
            relevant = any(kw in title_lower for kw in team_kw) or any(
                s in title_lower for s in ["analyst", "associate", "manager", "director", "vice president"]
            )
            if not relevant and len(leads) >= 3:
                continue   # enough good leads already; skip irrelevant extras

            lead = {
                "name":           f"{first} {last}",
                "title":          title,
                "company":        company,
                "university":     "",
                "linkedin_url":   linkedin,
                "tenure_months":  0,
                "is_alumni":      False,  # Hunter has no education data
                "snippet":        "",
                "email":          email,
            }
            leads.append(lead)

        logger.info(f"Hunter: {len(leads)} leads for {domain}")
        return leads

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
    def _serper_search(self, query: str, count: int = 10, page: int = 1) -> list[dict]:
        """
        Serper.dev — Google search API. $0.30/1k queries, free trial.
        Returns results in a format compatible with _parse_snippet().
        page=1 returns results 1-10, page=2 returns 11-20.
        Falls back to SERPER_API_KEY_2 if primary key is exhausted.
        """
        import urllib.request
        keys_to_try = [k for k in [self.serper_key, self.serper_key_2, self.serper_key_3] if k]
        for i, key in enumerate(keys_to_try):
            self._throttle()
            payload = json.dumps({
                "q": query, "num": min(count, 10),
                "page": page, "gl": "gb", "hl": "en",
            }).encode()
            req = urllib.request.Request(
                SERPER_ENDPOINT,
                data=payload,
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                if data.get("credits") == 0 or "insufficient credits" in str(data).lower():
                    label = "primary" if i == 0 else "backup"
                    logger.critical(f"🚨 SERPER CREDITS EXHAUSTED on {label} key")
                    if i + 1 < len(keys_to_try):
                        logger.info("Switching to backup Serper key...")
                        continue
                    raise RuntimeError("SERPER_CREDITS_EXHAUSTED")
                organic = data.get("organic", [])
                return [{"name": it.get("title", ""), "url": it.get("link", ""), "snippet": it.get("snippet", "")} for it in organic]
            except RuntimeError:
                if i + 1 < len(keys_to_try):
                    logger.info("Switching to backup Serper key...")
                    continue
                raise
            except urllib.request.HTTPError as e:
                if e.code == 429:
                    # Rate limited — back off and retry same key, don't treat as exhausted
                    logger.warning(f"Serper rate limited (429) — backing off 10s")
                    import time as _time; _time.sleep(10)
                    try:
                        with urllib.request.urlopen(req, timeout=15) as r2:
                            data2 = json.loads(r2.read().decode("utf-8", errors="replace"))
                        organic = data2.get("organic", [])
                        return [{"name": it.get("title", ""), "url": it.get("link", ""), "snippet": it.get("snippet", "")} for it in organic]
                    except Exception:
                        return []
                if e.code in (400, 402):
                    label = "primary" if i == 0 else "backup"
                    try:
                        body = e.read().decode("utf-8", errors="replace")
                    except Exception:
                        body = ""
                    if e.code == 402 or "not enough credits" in body.lower() or "insufficient" in body.lower():
                        logger.critical(f"🚨 SERPER CREDITS EXHAUSTED (HTTP {e.code}) on {label} key: {body}")
                        if i + 1 < len(keys_to_try):
                            logger.info("Switching to backup Serper key...")
                            continue
                        raise RuntimeError("SERPER_CREDITS_EXHAUSTED")
                    logger.error(f"Serper search failed (HTTP {e.code}): {body or e}")
                return []
            except Exception as e:
                logger.error(f"Serper search failed: {e}")
                return []
        return []

    def _serper_search_two_pages(self, query: str) -> list[dict]:
        """Fetch pages 1 and 2 from Serper (up to 20 results). Deduplicates by URL."""
        results_p1 = self._serper_search(query, count=10, page=1)
        results_p2 = self._serper_search(query, count=10, page=2)
        seen_urls = set()
        combined = []
        for r in results_p1 + results_p2:
            u = r.get("url", "")
            if u and u not in seen_urls:
                seen_urls.add(u)
                combined.append(r)
        return combined

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

        Google/Serper result format:
        {
          "name": "John Smith - Risk Analyst - Goldman Sachs | LinkedIn",
          "url":  "https://uk.linkedin.com/in/john-smith-...",
          "snippet": "John Smith. Risk Analyst at Goldman Sachs. UCL Economics 2019. London."
        }
        Also handles middle-dot separator:
          "Jane Smith · Senior PM · Stripe | LinkedIn"
        """
        raw_name    = result.get("name", "")
        url         = result.get("url", result.get("link", ""))
        snippet     = result.get("snippet", "")

        if "linkedin.com/in/" not in url:
            return None

        # Parse title — handle both "-" and "·" as separators
        name, title, company = "", "", ""
        title_parts = re.split(r"\s*[|·–\-]\s*", raw_name)
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

        # Validate — must have at least a name with one word
        if not name or len(name.split()) < 1:
            return None

        # Extract university from snippet
        university = _extract_university(snippet)

        # Extract tenure hint ("X years at Y", "since YYYY", "· N yrs")
        tenure_months = _extract_tenure(snippet)

        # Extract city/country from snippet
        location_city, location_country = _extract_location(snippet)

        # Clean LinkedIn URL
        linkedin_url = re.sub(r"\?.*$", "", url)  # strip query params

        return {
            "name":             name,
            "title":            title,
            "company":          company,
            "university":       university,
            "linkedin_url":     linkedin_url,
            "tenure_months":    tenure_months,
            "location_city":    location_city,
            "location_country": location_country,
            "snippet":          snippet[:300],
        }


def _extract_university(text: str) -> str:
    """Try to find a university name in a snippet."""
    text_lower = text.lower()
    for canonical, aliases in UNI_ALIASES.items():
        for alias in aliases:
            pattern = r'\b' + re.escape(alias) + r'\b'
            if re.search(pattern, text_lower):
                return alias.title()
    # Generic patterns: "University of X", "X University", "X College"
    # Restrict word counts to avoid greedily absorbing preceding company names.
    m = re.search(
        r"\b(university of (?:[a-z]+ ){1,4}[a-z]+|(?:[a-z]+ ){1,3}university|(?:[a-z]+ ){1,2}college)\b",
        text_lower,
    )
    if m:
        result = m.group(0).strip().title()
        _generic = {"The University", "A University", "The College", "A College", "The School"}
        if result in _generic:
            return ""
        return result
    return ""


def _extract_tenure(text: str) -> int:
    """Extract approximate tenure in months from snippet text."""
    # "· 3 yrs" or "· 2 yr" (LinkedIn compact format)
    m = re.search(r"[·•]\s*(\d+)\s*yr", text, re.I)
    if m:
        return int(m.group(1)) * 12

    # "N years at" or "N year at"
    m = re.search(r"(\d+)\s+years?\s+at\b", text, re.I)
    if m:
        return int(m.group(1)) * 12

    # "N months at"
    m = re.search(r"(\d+)\s+months?\s+at\b", text, re.I)
    if m:
        return int(m.group(1))

    # Generic "3 years" / "18 months" in snippet
    m = re.search(r"(\d+)\s+years?", text, re.I)
    if m:
        return int(m.group(1)) * 12

    m = re.search(r"(\d+)\s+months?", text, re.I)
    if m:
        return int(m.group(1))

    # "since 2022" or "since 2019"
    m = re.search(r"since\s+(20\d{2})", text, re.I)
    if m:
        years = datetime.utcnow().year - int(m.group(1))
        return max(0, years * 12)

    return 0


# Hub cities used for location extraction
_HUB_CITIES = [
    "london", "new york", "nyc", "paris", "frankfurt", "amsterdam",
    "zurich", "dublin", "singapore", "hong kong", "tokyo", "chicago",
    "san francisco", "boston", "los angeles", "seattle", "manchester",
    "edinburgh", "berlin", "milan", "madrid", "stockholm", "brussels",
]

# Country keyword patterns → canonical country name
_COUNTRY_PATTERNS = [
    (["united kingdom", "uk", "england", "scotland", "wales", "london",
      "manchester", "edinburgh"], "united kingdom"),
    (["united states", "usa", "u.s.", "new york", "nyc", "chicago",
      "san francisco", "boston", "los angeles", "seattle"], "united states"),
    (["france", "paris", "lyon"], "france"),
    (["germany", "frankfurt", "berlin", "munich"], "germany"),
    (["netherlands", "amsterdam", "holland"], "netherlands"),
    (["switzerland", "zurich", "geneva"], "switzerland"),
    (["ireland", "dublin"], "ireland"),
    (["singapore"], "singapore"),
    (["hong kong"], "hong kong"),
]


def _extract_location(text: str) -> tuple[str, str]:
    """Extract (location_city, location_country) from snippet text."""
    text_lower = text.lower()

    city = ""
    for hub in _HUB_CITIES:
        if hub in text_lower:
            city = hub.title()
            # "NYC" stays as-is
            if hub == "nyc":
                city = "New York"
            break

    country = ""
    for keywords, canon in _COUNTRY_PATTERNS:
        if any(kw in text_lower for kw in keywords):
            country = canon
            break

    return city, country


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
    Lead scorer (total 92 pts). Priority order:

      location        25 pts  — MUST be hub city of job region (known wrong city = 0)
      alumni          15 pts  — shared university
      industry_match  12 pts  — student industry prefs vs job industry
      department      12 pts  — same functional area as the role
      title_relevance 10 pts  — job title keywords found in lead title
      seniority_fit   10 pts  — exec level OR 1-3 levels above intern
      company_size     8 pts  — student size preference vs job company size
    """
    breakdown: dict[str, float] = {
        "location":        0.0,
        "industry_match":  0.0,
        "alumni":          0.0,
        "department":      0.0,
        "title_relevance": 0.0,
        "seniority_fit":   0.0,
        "company_size":    0.0,
    }

    # ── 1. Location (25 pts — top priority, wrong country = 0) ───────
    REGION_COUNTRY = {"UK": "united kingdom", "US": "united states", "EU": None}
    REGION_HUBS = {
        "UK": ["london", "manchester", "edinburgh", "bristol", "birmingham",
               "leeds", "glasgow", "oxford", "cambridge"],
        "US": ["new york", "nyc", "chicago", "san francisco", "boston",
               "los angeles", "houston", "miami", "seattle"],
        "EU": ["paris", "frankfurt", "amsterdam", "zurich", "dublin",
               "luxembourg", "milan", "madrid", "stockholm"],
    }
    job_region       = (job.get("region") or "UK").upper()
    expected_country = REGION_COUNTRY.get(job_region)
    lead_country     = (lead.get("location_country") or "").lower().strip()
    lead_city        = (lead.get("location_city") or "").lower().strip()

    hubs = REGION_HUBS.get(job_region, [])
    in_hub_city = bool(lead_city and any(h in lead_city for h in hubs))

    if not lead_country and not lead_city:
        loc_pts = 10.0   # no data — benefit of the doubt
    elif expected_country and lead_country == expected_country:
        # Same country: city MUST be a known hub (or unknown)
        if not lead_city:
            loc_pts = 18.0   # right country, city unconfirmed
        elif in_hub_city:
            loc_pts = 25.0   # confirmed hub city — full score
        else:
            loc_pts = 0.0    # right country but wrong city — hard miss
    elif job_region == "EU" and lead_country not in ("united states", "united kingdom"):
        loc_pts = 15.0 if in_hub_city else 0.0
    else:
        loc_pts = 0.0    # wrong country
    breakdown["location"] = loc_pts

    # ── 2. Alumni (15 pts) ───────────────────────────────────────────
    alumni_pts = 15.0 if lead.get("is_alumni") else 0.0
    breakdown["alumni"] = alumni_pts

    # ── 3. Industry match (12 pts) ───────────────────────────────────
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
        jil = job_industry.lower()
        sil = [s.lower() for s in student_industries if s]
        if jil in sil:
            ind_pts = 12.0
        elif any(jil in s or s in jil for s in sil):
            ind_pts = 6.0
    breakdown["industry_match"] = ind_pts

    # ── 4. Department match (12 pts) ─────────────────────────────────
    lead_title      = (lead.get("title") or "").lower()
    job_title_lower = (job.get("title") or "").lower()
    job_industry_l  = job_industry.lower()
    job_industries  = [i.lower() for i in (job.get("industries") or []) if i]
    combined_job    = " ".join([job_title_lower, job_industry_l] + job_industries)

    DEPT_GROUPS: list[tuple[str, list[str]]] = [
        ("finance",    ["finance", "financial", "investment", "portfolio", "trading",
                        "banking", "capital", "fund", "asset", "equity", "quant",
                        "risk", "treasury", "insurance", "underwriting", "credit",
                        "markets", "securities", "wealth", "hedge"]),
        ("technology", ["engineer", "software", "developer", "data", "product",
                        "architect", "devops", "infrastructure", "machine learning",
                        "ai", "analytics", "platform", "technical"]),
        ("consulting", ["consulting", "strategy", "advisory", "transformation"]),
        ("law",        ["legal", "counsel", "compliance", "regulatory", "litigation"]),
        ("operations", ["operations", "ops", "supply chain", "logistics", "programme"]),
        ("hr",         ["human resources", "people", "talent", "recruiting",
                        "diversity", "inclusion"]),
        ("marketing",  ["marketing", "brand", "communications", "content", "growth"]),
        ("sales",      ["sales", "business development", "bd", "commercial",
                        "partnerships", "account"]),
    ]
    job_dept_kws: list[str] = []
    for _dept, kws in DEPT_GROUPS:
        if any(kw in combined_job for kw in kws):
            job_dept_kws = kws
            break
    if job_dept_kws and any(kw in lead_title for kw in job_dept_kws):
        dept_pts = 12.0
    elif not job_dept_kws:
        dept_pts = 5.0
    else:
        dept_pts = 0.0
    breakdown["department"] = dept_pts

    # ── 5. Title relevance (10 pts) ──────────────────────────────────
    # _title_relevance_pts returns 0-20; rescale to 0-10
    trel_pts = round(_title_relevance_pts(job.get("title", "") or "", lead.get("title") or "") * 0.5, 1)
    breakdown["title_relevance"] = trel_pts

    # ── 6. Seniority fit (10 pts) — exec OR 1-3 levels above intern ──
    EXEC_KW = ("chief ", "ceo", "cfo", "cto", "coo", "president",
               "managing director", "head of", " partner", "founding")
    is_exec = any(kw in lead_title for kw in EXEC_KW)
    if is_exec:
        sen_pts = 10.0
    else:
        diff = SENIORITY_RANK.get(_infer_seniority_from_title(lead.get("title") or ""), 2) - SENIORITY_RANK.get("intern", 0)
        sen_pts = 10.0 if 1 <= diff <= 3 else (4.0 if diff == 4 else 0.0)
    breakdown["seniority_fit"] = sen_pts

    # ── 7. Company size (8 pts) ──────────────────────────────────────
    raw_student_size = student.get("company_size")
    if isinstance(raw_student_size, list):
        student_sizes = [_normalise_company_size(s) for s in raw_student_size]
    else:
        try:
            parsed = json.loads(raw_student_size) if raw_student_size else []
            student_sizes = [_normalise_company_size(s) for s in (parsed if isinstance(parsed, list) else [parsed])]
        except (json.JSONDecodeError, TypeError):
            student_sizes = [_normalise_company_size(raw_student_size)]
    student_sizes = [s for s in student_sizes if s]
    job_size = _normalise_company_size(job.get("company_size"))
    size_pts = 8.0 if (student_sizes and job_size and job_size in student_sizes) else 0.0
    breakdown["company_size"] = size_pts

    total = round(min(
        loc_pts + ind_pts + alumni_pts + dept_pts + trel_pts + sen_pts + size_pts,
        100
    ), 1)
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
