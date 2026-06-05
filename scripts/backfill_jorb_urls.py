"""Backfill direct company apply URLs for all jorb jobs that currently store jorb.ai URLs."""
import sys, time, urllib.request, re
sys.path.insert(0, '.')

from db.database import fetchall, execute

_APPLY_LINK_RE = re.compile(
    r'href="(https?://(?!(?:www\.)?jorb\.ai(?:/|$))[^"]{15,})"[^>]*target="_blank"',
    re.IGNORECASE,
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html",
}

_ATS_DOMAINS = frozenset({
    "myworkdayjobs.com", "greenhouse.io", "lever.co",
    "fa.oraclecloud.com", "oraclecloud.com",
    "smartrecruiters.com", "icims.com", "taleo.net",
    "successfactors.com", "jobvite.com", "bamboohr.com",
    "brassring.com", "ultipro.com", "kenexa.com",
    "workable.com", "ashbyhq.com", "recruitee.com",
    "boards.greenhouse.io", "jobs.lever.co", "app.bamboohr.com",
})


def _careers_site_from_url(apply_url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(apply_url).netloc.lower()
        for ats in _ATS_DOMAINS:
            if host == ats or host.endswith("." + ats):
                return ""
        return f"https://{host}"
    except Exception:
        return ""


CLOSED_DATE = "2026-06-05"

rows = fetchall(
    "SELECT id, url, company, title, careers_site FROM jobs "
    "WHERE source = 'jorb' AND url LIKE ? "
    "AND (closing_date IS NULL OR closing_date = '')",
    ("%jorb.ai%",),
)
print(f"Found {len(rows)} jorb jobs to backfill")

updated = 0
closed = 0
for i, row in enumerate(rows):
    job_id   = row["id"]
    jorb_url = row["url"]
    try:
        time.sleep(0.2)
        req  = urllib.request.Request(jorb_url, headers=_HEADERS)
        html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="replace")
        m = _APPLY_LINK_RE.search(html)
        if m:
            direct_url = m.group(1)
            co_site    = _careers_site_from_url(direct_url) if not row.get("careers_site") else row["careers_site"]
            execute(
                "UPDATE jobs SET url = ?, careers_site = COALESCE(NULLIF(careers_site,''), ?) WHERE id = ?",
                (direct_url, co_site, job_id),
            )
            print(f"  [{i+1}/{len(rows)}] {row['company']}: {direct_url[:80]}")
            updated += 1
        else:
            execute("UPDATE jobs SET closing_date = ? WHERE id = ?", (CLOSED_DATE, job_id))
            print(f"  [{i+1}/{len(rows)}] {row['company']}: listing pulled — marked closed {CLOSED_DATE}")
            closed += 1
    except Exception as e:
        execute("UPDATE jobs SET closing_date = ? WHERE id = ?", (CLOSED_DATE, job_id))
        print(f"  [{i+1}/{len(rows)}] {row['company']}: error ({e}) — marked closed {CLOSED_DATE}")
        closed += 1

print(f"Done — {updated} updated, {closed} closed")
