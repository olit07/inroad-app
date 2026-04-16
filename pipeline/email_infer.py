"""
CCC Backend — Email Inference Engine (Phase 4)

Given a person's name + company domain, generate candidate emails
using the 8 most common corporate patterns.

Optional: Hunter.io API for domain discovery and email verification.
Set HUNTER_API_KEY env var (free tier: 25 verifications/month).
"""
import os
import re
import json
import logging
import urllib.parse
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.base import fetch_json, RequestError

logger = logging.getLogger(__name__)

HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search?domain={domain}&api_key={key}"
HUNTER_VERIFY_URL = "https://api.hunter.io/v2/email-verifier?email={email}&api_key={key}"
HUNTER_FIND_URL   = "https://api.hunter.io/v2/email-finder?domain={domain}&first_name={fn}&last_name={ln}&api_key={key}"

# 8 most common corporate email patterns
EMAIL_PATTERNS = [
    "{first}.{last}@{domain}",        # john.smith@company.com     (most common)
    "{first}{last}@{domain}",         # johnsmith@company.com
    "{f}{last}@{domain}",             # jsmith@company.com
    "{first}@{domain}",               # john@company.com
    "{last}@{domain}",                # smith@company.com
    "{first}_{last}@{domain}",        # john_smith@company.com
    "{f}.{last}@{domain}",            # j.smith@company.com
    "{last}.{first}@{domain}",        # smith.john@company.com
]

CONFIDENCE = {
    "verified":  "HIGH",
    "pattern":   "MEDIUM",
    "domain_only": "LOW",
}


def clean_name_part(s: str) -> str:
    """Lowercase, strip accents roughly, remove non-alpha."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z]", "", s)
    return s


def generate_candidates(first: str, last: str, domain: str) -> list[str]:
    """Generate all pattern-based email candidates for a person."""
    fn = clean_name_part(first)
    ln = clean_name_part(last)
    f  = fn[0] if fn else ""
    l  = ln[0] if ln else ""

    if not fn or not ln or not domain:
        return []

    candidates = []
    for pattern in EMAIL_PATTERNS:
        try:
            email = pattern.format(first=fn, last=ln, f=f, l=l, domain=domain)
            if email not in candidates:
                candidates.append(email)
        except KeyError:
            continue
    return candidates


def extract_domain_from_url(url: str) -> str:
    """Extract the email domain from a company website URL."""
    if not url:
        return ""
    url = url.lower().strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.netloc or parsed.path
        # Strip www.
        host = re.sub(r"^www\.", "", host)
        return host
    except Exception:
        return ""


class EmailInferrer:
    """
    Infer the most likely email address for a person given their name + company.
    """

    def __init__(self):
        self.hunter_key = os.environ.get("HUNTER_API_KEY", "")

    def infer(
        self,
        first_name:    str,
        last_name:     str,
        company_name:  str,
        company_domain: str = "",
        job_url:       str  = "",
    ) -> dict:
        """
        Returns:
        {
            email:       str,        # best guess email
            confidence:  str,        # HIGH / MEDIUM / LOW
            all_candidates: list,    # all pattern-based guesses
            domain:      str,        # domain used
        }
        """
        domain = company_domain or self._discover_domain(company_name, job_url)
        if not domain:
            return {"email": "", "confidence": "LOW", "all_candidates": [], "domain": ""}

        # Try Hunter.io email finder first (uses their verified pattern database)
        if self.hunter_key:
            hunter_result = self._hunter_find(first_name, last_name, domain)
            if hunter_result:
                return {
                    "email":          hunter_result["email"],
                    "confidence":     "HIGH",
                    "all_candidates": [hunter_result["email"]],
                    "domain":         domain,
                }

        # Fall back to pattern generation
        candidates = generate_candidates(first_name, last_name, domain)
        best = candidates[0] if candidates else ""

        return {
            "email":          best,
            "confidence":     "MEDIUM" if candidates else "LOW",
            "all_candidates": candidates,
            "domain":         domain,
        }

    # ATS / job-portal domains that are never real employee email domains
    _ATS_SUFFIXES = (
        "workday.com", "myworkday.com", "myworkdaysite.com",
        "myworkdayjobs.com",
        "greenhouse.io", "lever.co", "ashbyhq.com",
        "tal.net", "taleo.net", "icims.com", "smartrecruiters.com",
        "jobvite.com", "successfactors.com", "bamboohr.com",
        "workable.com", "recruitee.com",
        "linkedin.com", "indeed.com", "glassdoor.com",
    )

    def _is_ats_domain(self, domain: str) -> bool:
        d = domain.lower().strip()
        return any(d == s or d.endswith("." + s) for s in self._ATS_SUFFIXES)

    def _discover_domain(self, company_name: str, job_url: str = "") -> str:
        """Try to find the company's email domain."""
        # 1. Extract from job URL (skip ATS/job-portal domains)
        if job_url:
            d = extract_domain_from_url(job_url)
            if d and not self._is_ats_domain(d):
                return d

        # 2. Groq LLM lookup (via company_email_formats cache then Groq)
        if company_name:
            try:
                from pipeline.lead_builder import _lookup_email_format_via_llm
                from db.database import get_email_format
                # Check DB cache first, then ask Groq — never use a naive guess here
                db_result = get_email_format(company_name)
                groq_result = db_result or _lookup_email_format_via_llm(company_name)
                if groq_result:
                    _, domain = groq_result
                    if domain and not self._is_ats_domain(domain):
                        return domain
            except Exception as e:
                logger.debug(f"Groq email format lookup failed for {company_name}: {e}")

        return ""

    def _hunter_find(self, first: str, last: str, domain: str) -> dict | None:
        try:
            url = HUNTER_FIND_URL.format(
                domain=domain,
                fn=urllib.parse.quote(first),
                ln=urllib.parse.quote(last),
                key=self.hunter_key,
            )
            data = fetch_json(url)
            email = data.get("data", {}).get("email", "")
            if email:
                return {"email": email}
        except Exception as e:
            logger.debug(f"Hunter find failed: {e}")
        return None
