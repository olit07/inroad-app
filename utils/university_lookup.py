"""
university_lookup.py — map email domains to university names.

Used to auto-detect a student's university from their email address at sign-up.
Data source: data/universities.csv (110 UK + 120 US universities).
"""

import csv
import os
from functools import lru_cache

_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "universities.csv")


@lru_cache(maxsize=1)
def _load_domain_map() -> dict:
    """Load the CSV once and return a dict keyed by lowercase email domain."""
    domain_map = {}
    try:
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                domain_map[row["domain"].strip().lower()] = {
                    "name": row["name"].strip(),
                    "country": row["country"].strip(),
                }
    except FileNotFoundError:
        pass
    return domain_map


def detect_university(email: str) -> dict | None:
    """
    Given an email address, return the matching university or None.

    Example:
        detect_university("alice@lse.ac.uk")
        → {"name": "London School of Economics", "country": "UK"}

        detect_university("bob@gmail.com")
        → None
    """
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower().strip()
    return _load_domain_map().get(domain)


def get_all_universities() -> list:
    """Return all universities sorted alphabetically by name."""
    domain_map = _load_domain_map()
    unis = [
        {"name": v["name"], "domain": k, "country": v["country"]}
        for k, v in domain_map.items()
    ]
    return sorted(unis, key=lambda u: u["name"])
