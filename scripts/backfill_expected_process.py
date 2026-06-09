"""
Backfill 'process' (Expected Interview Process) into jobs.raw JSON.

Phase A — free, instant:
    For companies already on Trackr with a process field, copy that value to
    all other listings for the same company (jorb, greenhouse, etc.).

Phase B — web search + AI:
    For companies with no process data anywhere, run 2 Serper queries and ask
    Claude Haiku to normalise the results into the standard abbreviation format.

Usage:
    python scripts/backfill_expected_process.py [--limit 150] [--dry-run] [--force]

Options:
    --limit N   Max Serper queries to spend in Phase B (2 queries per company, default 150)
    --dry-run   Print what would be written without touching the DB
    --force     Re-process companies that already have expected_process_source set
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
import psycopg2.extras

DSN = "postgresql://postgres:DzxzVniwSoXWPNIrpIkaXwuWhCUBbDvH@junction.proxy.rlwy.net:14476/railway?sslmode=require"

SERPER_ENDPOINT = "https://google.serper.dev/search"

# Key fallback chain: env var first, then the fresh keys provided 2026-06-09
SERPER_KEYS = list(filter(None, [
    os.environ.get("SERPER_API_KEY"),
    "589ec432e7bba5304bbd2fc0681e08e0de5f66bb",
    "f15aaa8fdc0e0bed6dcd1d71de5bb1f4237ddcbe",
]))

# Valid step abbreviations the frontend knows about
VALID_STEPS = {"OA", "HV", "VI", "AC", "TI", "PI", "SB"}


# ---------------------------------------------------------------------------
# Serper helpers
# ---------------------------------------------------------------------------

_key_index = 0


def _active_key() -> str | None:
    global _key_index
    if _key_index < len(SERPER_KEYS):
        return SERPER_KEYS[_key_index]
    return None


def _next_key() -> bool:
    global _key_index
    _key_index += 1
    return _key_index < len(SERPER_KEYS)


def serper_search(query: str) -> list[dict]:
    """Return a list of {title, snippet, link} from Serper organic results."""
    key = _active_key()
    if not key:
        return []

    payload = json.dumps({"q": query, "num": 8, "gl": "gb", "hl": "en"}).encode()
    req = urllib.request.Request(
        SERPER_ENDPOINT,
        data=payload,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.request.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if e.code == 400 and ("not enough credits" in body.lower() or "insufficient" in body.lower()):
            print(f"  Key {key[:8]}... exhausted, trying next key")
            if _next_key():
                return serper_search(query)
            print("  All Serper keys exhausted.")
            return []
        print(f"  Serper HTTP {e.code} for query {query!r}: {body[:100]}")
        return []
    except Exception as e:
        print(f"  Serper error: {e}")
        return []

    if "insufficient" in str(data).lower() or data.get("credits") == 0:
        print(f"  Key {key[:8]}... exhausted, trying next key")
        if _next_key():
            return serper_search(query)
        print("  All Serper keys exhausted.")
        return []

    return [
        {"title": r.get("title", ""), "snippet": r.get("snippet", ""), "link": r.get("link", "")}
        for r in data.get("organic", [])
    ]


# ---------------------------------------------------------------------------
# Claude normalisation
# ---------------------------------------------------------------------------

def normalise_with_groq(company: str, title: str, snippets: list[dict]) -> dict:
    """
    Ask Groq (Llama 3) to extract likely interview stages from search snippets.
    Returns {"process": "OA > HV > AC", "confidence": "high|medium|low|none"}.
    """
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_EMAILLLM_API_KEY")
    if not api_key:
        print("  GROQ_API_KEY not set — skipping normalisation")
        return {"process": "", "confidence": "none"}

    snippet_text = "\n".join(
        f"[{i+1}] {s['title']}\n{s['snippet']}"
        for i, s in enumerate(snippets[:8])
        if s.get("snippet")
    )
    if not snippet_text.strip():
        return {"process": "", "confidence": "none"}

    prompt = f"""You are helping build a UK graduate job platform. Given search snippets about a company's interview process, extract the expected interview stages for internship/graduate roles.

Company: {company}
Sample role: {title}

Search snippets:
{snippet_text}

Return a JSON object with:
- "process": a string of stage abbreviations separated by " > " (e.g. "OA > HV > AC")
- "confidence": "high" if stages are clearly described, "medium" if implied, "low" if very uncertain, "none" if no relevant info

Preferred abbreviations:
  OA = Online Assessment / aptitude test
  HV = HireVue / one-way video interview
  VI = Video Interview / phone screen / recruiter call
  AC = Assessment Centre / superday / final round in-person
  TI = Technical Interview / case study interview
  PI = Partner Interview / managing director interview
  SB = Superday (US investment banking final round)

If the process uses stages not in this list, write the stage name in plain text (e.g. "OA > Group Exercise > AC").
If there is no clear evidence of an interview process, return {{"process": "", "confidence": "none"}}.

Respond with valid JSON only, no other text."""

    try:
        import requests as _req
        import re as _re
        resp = _req.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0,
            },
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        m = _re.search(r'\{.*?\}', raw, _re.DOTALL)
        if not m:
            return {"process": "", "confidence": "none"}
        json_str = m.group()
        # Repair trailing commas before } or ]
        json_str = _re.sub(r',\s*([}\]])', r'\1', json_str)
        # Repair missing commas between "value"\n"key" pairs
        json_str = _re.sub(r'("|\d)\s*\n(\s*")', r'\1,\n\2', json_str)
        try:
            data = json.loads(json_str)
        except Exception:
            # Last resort: extract values with regex
            proc_m = _re.search(r'"process"\s*:\s*"([^"]*)"', json_str)
            conf_m = _re.search(r'"confidence"\s*:\s*"([^"]*)"', json_str)
            data = {
                "process": proc_m.group(1) if proc_m else "",
                "confidence": conf_m.group(1) if conf_m else "none",
            }
        return {
            "process": (data.get("process") or "").strip(),
            "confidence": (data.get("confidence") or "none").strip(),
        }
    except Exception as e:
        print(f"  Groq error: {e}")
        return {"process": "", "confidence": "none"}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def update_jobs_process(conn, job_ids: list[int], process: str, source: str, dry_run: bool):
    if dry_run:
        print(f"    [dry-run] would update {len(job_ids)} jobs → process={process!r} source={source!r}")
        return

    cur = conn.cursor()
    for job_id in job_ids:
        cur.execute("SELECT raw FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            continue
        try:
            raw_dict = json.loads(row["raw"]) if row["raw"] else {}
        except Exception:
            raw_dict = {}
        raw_dict["process"] = process
        raw_dict["expected_process_source"] = source
        cur.execute(
            "UPDATE jobs SET raw = %s WHERE id = %s",
            (json.dumps(raw_dict), job_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Phase A: cross-populate from Trackr data
# ---------------------------------------------------------------------------

def phase_a(conn, dry_run: bool, force: bool) -> int:
    print("\n=== Phase A: cross-populate from existing Trackr process data ===")
    cur = conn.cursor()

    # Build company → canonical process map from Trackr jobs that have it
    cur.execute("""
        SELECT LOWER(TRIM(company)) AS co, raw::json->>'process' AS process
        FROM jobs
        WHERE source = 'trackr'
          AND raw::json->>'process' IS NOT NULL
          AND raw::json->>'process' != ''
    """)
    trackr_rows = cur.fetchall()

    # Use the most common process per company (mode)
    from collections import Counter
    company_processes: dict[str, str] = {}
    counts: dict[str, Counter] = {}
    for row in trackr_rows:
        co = row["co"]
        proc = row["process"]
        counts.setdefault(co, Counter())[proc] += 1
    for co, ctr in counts.items():
        company_processes[co] = ctr.most_common(1)[0][0]

    print(f"  Found process data for {len(company_processes)} companies from Trackr")

    # Find non-Trackr jobs at these companies that lack a process
    already_condition = "" if force else "AND (raw::json->>'expected_process_source' IS NULL OR raw::json->>'expected_process_source' = '')"
    cur.execute(f"""
        SELECT id, LOWER(TRIM(company)) AS co, raw
        FROM jobs
        WHERE source != 'trackr'
          AND (raw::json->>'process' IS NULL OR raw::json->>'process' = '')
          {already_condition}
    """)
    candidates = cur.fetchall()

    updated_count = 0
    company_job_map: dict[str, list[int]] = {}
    for row in candidates:
        co = row["co"]
        if co in company_processes:
            company_job_map.setdefault(co, []).append(row["id"])

    for co, job_ids in company_job_map.items():
        proc = company_processes[co]
        print(f"  {co!r}: {len(job_ids)} jobs → {proc!r}")
        update_jobs_process(conn, job_ids, proc, "trackr_crossref", dry_run)
        updated_count += len(job_ids)

    print(f"  Phase A updated {updated_count} jobs across {len(company_job_map)} companies")
    return updated_count


# ---------------------------------------------------------------------------
# Phase B: Serper + Claude for remaining companies
# ---------------------------------------------------------------------------

def phase_b(conn, limit: int, dry_run: bool, force: bool) -> int:
    print(f"\n=== Phase B: Serper + Claude enrichment (up to {limit} Serper queries) ===")

    if not _active_key():
        print("  No Serper keys available — skipping Phase B")
        return 0

    cur = conn.cursor()

    # Find companies where ALL jobs lack process — prioritise those with most listings
    already_condition = "" if force else "AND (raw::json->>'expected_process_source' IS NULL OR raw::json->>'expected_process_source' = '')"
    cur.execute(f"""
        SELECT LOWER(TRIM(company)) AS co,
               MIN(title) AS sample_title,
               COUNT(*) AS n,
               ARRAY_AGG(id) AS ids,
               ARRAY_AGG(raw) AS raws
        FROM jobs
        WHERE (raw::json->>'process' IS NULL OR raw::json->>'process' = '')
          {already_condition}
        GROUP BY LOWER(TRIM(company))
        ORDER BY COUNT(*) DESC
    """)
    companies = cur.fetchall()

    print(f"  {len(companies)} companies need enrichment")

    max_companies = limit // 2  # 2 Serper queries per company
    updated = 0
    serper_calls = 0

    for row in companies[:max_companies]:
        if not _active_key():
            print("  Serper keys exhausted — stopping Phase B")
            break

        co = row["co"]
        title = row["sample_title"] or "internship"
        job_ids = list(row["ids"])

        print(f"  [{co!r}] {len(job_ids)} job(s)…")

        if dry_run:
            print(f"    [dry-run] would search Serper + Claude for {co!r}")
            continue

        # Two targeted Serper queries
        q1 = f'"{co}" internship interview process'
        q2 = f'"{co}" graduate scheme spring week stages assessment'

        results1 = serper_search(q1)
        serper_calls += 1
        time.sleep(0.5)
        results2 = serper_search(q2)
        serper_calls += 1
        time.sleep(0.5)

        snippets = results1 + results2

        result = normalise_with_groq(co, title, snippets)
        time.sleep(2.5)  # stay under Groq free-tier 30 RPM
        process = result["process"]
        confidence = result["confidence"]

        print(f"    → process={process!r} confidence={confidence!r}")

        if confidence in ("high", "medium") and process:
            update_jobs_process(conn, job_ids, process, "serper+claude", dry_run)
            updated += len(job_ids)
        else:
            print(f"    skipped (confidence too low or no result)")

    print(f"  Phase B: {serper_calls} Serper queries, {updated} jobs updated")
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=150, help="Max Serper queries for Phase B")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-process already-enriched companies")
    args = parser.parse_args()

    conn = get_connection()

    a = phase_a(conn, dry_run=args.dry_run, force=args.force)
    b = phase_b(conn, limit=args.limit, dry_run=args.dry_run, force=args.force)

    conn.close()
    print(f"\nDone. Total jobs updated: {a + b}")


if __name__ == "__main__":
    main()
