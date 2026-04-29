"""
inroad — University Detector

Delegates to university_lookup.py which reads data/universities.csv dynamically.
This ensures the domain → name mapping is always in sync with the CSV.
"""

from utils.university_lookup import detect_university as _lookup


def detect_university(email: str) -> str:
    """
    Return human-readable university name from a student email address.
    Returns empty string if unrecognised.
    """
    result = _lookup(email)
    return result["name"] if result else ""


def university_from_email_response(email: str) -> dict:
    name = detect_university(email)
    return {
        "email":      email,
        "university": name,
        "detected":   bool(name),
    }
