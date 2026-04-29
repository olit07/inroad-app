"""
Test Workday tenant URL configurations.
Tries POST to each candidate URL and reports HTTP status.

Usage: python scripts/test_workday.py
"""
import json
import gzip
import urllib.request
import urllib.error
import sys

CANDIDATES = [
    # (label, subdomain, board, wd_number)

    # ── Consulting — Big 4 ──────────────────────────────────────────────────
    # Deloitte
    ("Deloitte [wd1/Deloitte]",         "deloitte",  "Deloitte",         1),
    ("Deloitte [wd1/deloitte]",         "deloitte",  "deloitte",         1),
    ("Deloitte [wd2/DeloitteCareers]",  "deloitte",  "DeloitteCareers",  2),  # current (failing)
    # KPMG
    ("KPMG [kpmg1.wd3/campus]",         "kpmg1",     "campus",           3),
    ("KPMG [kpmg.wd5/campus]",          "kpmg",      "campus",           5),  # current
    ("KPMG [kpmg.wd3/campus]",          "kpmg",      "campus",           3),
    # EY
    ("EY [ey.wd5/EY]",                  "ey",        "EY",               5),
    ("EY [ey.wd1/EY]",                  "ey",        "EY",               1),  # current
    # PwC
    ("PwC [pwc.wd3/Global]",            "pwc",       "Global",           3),
    ("PwC [pwc.wd1/Global]",            "pwc",       "Global",           1),  # current
    # Accenture
    ("Accenture [wd3/AccentureCareers]","accenture", "AccentureCareers", 3),
    ("Accenture [wd1/AccentureCareers]","accenture", "AccentureCareers", 1),  # current
    # Capgemini
    ("Capgemini [wd3/Capgemini]",       "capgemini", "Capgemini",        3),  # current — may be ok

    # ── Consulting — MBB ───────────────────────────────────────────────────
    ("BCG [bcg.wd3/BCGCareers]",        "bcg",       "BCGCareers",       3),
    ("BCG [bcg.wd1/BCGCareers]",        "bcg",       "BCGCareers",       1),
    ("Bain [bain.wd1/BainCareers]",     "bain",      "BainCareers",      1),
    ("Bain [bain.wd3/BainCareers]",     "bain",      "BainCareers",      3),

    # ── Consulting — IQVIA ─────────────────────────────────────────────────
    ("IQVIA [wd3/IQVIA]",               "iqvia",     "IQVIA",            3),
    ("IQVIA [wd5/IQVIA]",               "iqvia",     "IQVIA",            5),

    # ── Healthcare — Pharma ────────────────────────────────────────────────
    # AstraZeneca
    ("AstraZeneca [wd3/AstraZenecaGlobal]", "astrazeneca", "AstraZenecaGlobal", 3),
    ("AstraZeneca [wd4/AstraZenecaGlobal]", "astrazeneca", "AstraZenecaGlobal", 4),  # current
    ("AstraZeneca [wd5/AstraZenecaGlobal]", "astrazeneca", "AstraZenecaGlobal", 5),
    # GSK
    ("GSK [wd5/gsk]",                   "gsk",       "gsk",              5),
    ("GSK [wd1/gsk]",                   "gsk",       "gsk",              1),  # current
    ("GSK [wd3/gsk]",                   "gsk",       "gsk",              3),
    # J&J
    ("J&J [jnj.wd5/JNJCareers]",        "jnj",       "JNJCareers",       5),
    ("J&J [jnj.wd1/JNJCareers]",        "jnj",       "JNJCareers",       1),  # current
    ("J&J [jnj.wd3/JNJCareers]",        "jnj",       "JNJCareers",       3),
    # Roche
    ("Roche [wd3/Roche]",               "roche",     "Roche",            3),
    ("Roche [wd5/Roche]",               "roche",     "Roche",            5),
    # Novartis
    ("Novartis [wd3/Novartis]",         "novartis",  "Novartis",         3),
    ("Novartis [wd5/Novartis]",         "novartis",  "Novartis",         5),
    # Sanofi
    ("Sanofi [wd5/Sanofi]",             "sanofi",    "Sanofi",           5),
    ("Sanofi [wd3/Sanofi]",             "sanofi",    "Sanofi",           3),
    # AbbVie
    ("AbbVie [wd1/AbbVie]",             "abbvie",    "AbbVie",           1),
    ("AbbVie [wd3/AbbVie]",             "abbvie",    "AbbVie",           3),
    # Bayer
    ("Bayer [wd3/Bayer]",               "bayer",     "Bayer",            3),
    ("Bayer [wd5/Bayer]",               "bayer",     "Bayer",            5),
]

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Content-Encoding": "gzip",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

BODY = gzip.compress(json.dumps({
    "limit": 1, "offset": 0,
    "searchText": "graduate",
    "appliedFacets": {}
}).encode())


def test_url(label, sub, board, wd_n):
    url = f"https://{sub}.wd{wd_n}.myworkdayjobs.com/wday/cxs/{sub}/{board}/jobs"
    req = urllib.request.Request(url, data=BODY, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
            count = len(data.get("jobPostings", []))
            return 200, f"{count} postings"
    except urllib.error.HTTPError as e:
        return e.code, str(e.reason)
    except urllib.error.URLError as e:
        return 0, str(e.reason)
    except Exception as e:
        return -1, str(e)


print(f"{'Status':<8} {'Label':<48} {'Detail'}")
print("-" * 80)

ok_count = 0
for label, sub, board, wd_n in CANDIDATES:
    status, detail = test_url(label, sub, board, wd_n)
    marker = "✓" if status == 200 else " "
    if status == 200:
        ok_count += 1
    print(f"{marker} {status:<6} {label:<48} {detail}")
    sys.stdout.flush()

print(f"\n{ok_count}/{len(CANDIDATES)} candidates returned HTTP 200")
