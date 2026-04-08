# inroad — Handoff Document

**Project:** inroad — career platform matching students to real professionals at companies with open roles, generating cold outreach email drafts.  
**Stack:** Flask API on Railway (Postgres in prod, SQLite locally), JWT auth, Resend for email, Serper for Google/LinkedIn search, Claude for email drafts.  
**Last updated:** 2026-04-08

---

## Current State

The pipeline code is deployed and functional. The **lead pool is empty** — the Serper-based lead builder was written but immediately disabled due to API cost concerns before any crawl was run. Card generation currently produces 0 cards because it queries the `leads` table (empty) and has no fallback. This is the main thing that needs to be resolved.

---

## What Was Built (and Works)

### Infrastructure — all deployed and live

| File | What it does |
|---|---|
| `pipeline/lead_builder.py` | Systematic Google/Serper crawler. Iterates every (company, department) from active jobs, runs Query A (alumni) + Query B (broad location), fetches 2 pages (20 results), parses snippets, stores in `leads` table, writes training JSONL. **Written, not yet run.** |
| `db/database.py` | `leads` table exists in Postgres (via migration). `upsert_lead()`, `get_leads_for_company()`, `get_seen_linkedin_urls()`, `get_leads_stats()` all implemented. |
| `pipeline/matcher.py` | `_parse_snippet` improved: handles `·` separator, extracts city/country via `_extract_location()`, improved tenure patterns. `_serper_search` now takes `page=` param. `_serper_search_two_pages()` added. |
| `pipeline/daily_cards.py` | Uses `leads` table first (no Serper fallback). 30-day freshness window. Min score threshold 50. All-time person deduplication (`get_seen_linkedin_urls`). Tenure hint in Claude prompt. Company-per-day diversity cap removed (same company OK, same person never). |
| `config/settings.py` | `COMPANY_SIZE_LOOKUP` (~40 companies), `DEPT_MAP` (14 depts with keyword lists), `INDUSTRY_DEPT_MAP` (Finance/Tech/Law → dept keys), `REGION_LOCATION_FALLBACK`. |
| `scrapers/trackr.py` | `posted_at` now uses `opening_date` from Trackr (NULL if not found). `company_size` populated from `COMPANY_SIZE_LOOKUP`. |
| `api/server.py` | All `/api/admin/*` routes + `/admin` page gated behind `@require_admin` decorator (checks `?key=` param or `X-Admin-Key` header against `ADMIN_SECRET` env var). `/api/admin/build-leads` endpoint exists but returns 503 (disabled). `/api/admin/leads/stats` returns live stats. |
| `html/ccc-admin.html` | Lead pool stats panel. "Build lead pool" button (currently shows disabled). Admin fetches forward `?key=` automatically. |
| `scheduler/run.py` | Scraping gated behind `SCRAPE_ENABLED` env var (currently `false` — scraper is paused). Cards job still runs at 07:00 UTC. |

### Scoring system (`pipeline/matcher.py: score_lead_v2`)
100-point system: location (25), alumni (15), industry match (12), department (12), title relevance (10), seniority (10), tenure (8), company size (8). Correct in design — problem is the `leads` table is empty so it never fires.

---

## What Doesn't Work Yet

### 1. Lead pool is empty → 0 cards generated
`daily_cards.py` queries `get_leads_for_company(company)` first. Table is empty. The old live Serper fallback was removed. **Cards will not be generated until the lead pool is populated.**

### 2. Lead builder has never been run
`pipeline/lead_builder.py` is complete but has never executed. It was written, disabled at the API endpoint level (503), and scraping was paused before any crawl happened.

### 3. Scoring hasn't been validated
Because no leads are in the DB, the score improvements (city extraction, tenure parsing, company_size lookup) have never been exercised end-to-end.

---

## The Plan: How to Fix This

The cost concern is Serper API quota. The right approach is **controlled, batched crawling** — not running `build_leads` for all companies at once. Here's the intended flow:

### Step 1: Run a small test crawl manually
```bash
# From project root, on a machine with SERPER_API_KEY set:
python pipeline/lead_builder.py --company "Goldman Sachs" --university "UCL" --dry-run
# Review output — check data/leads_training.jsonl for quality
python pipeline/lead_builder.py --company "Goldman Sachs" --university "UCL"
# Check: SELECT COUNT(*), company FROM leads GROUP BY company;
```

### Step 2: Verify card generation uses the leads
```bash
python pipeline/daily_cards.py --student-id 1
# Should produce cards using leads from table, score ≥ 50
```

### Step 3: Re-enable the admin build-leads endpoint (with a company filter)
In `api/server.py`, the `/api/admin/build-leads` endpoint currently returns 503. To re-enable, restore the body:

```python
@app.route("/api/admin/build-leads", methods=["POST"])
@require_admin
def admin_build_leads():
    data    = request.get_json(silent=True) or {}
    company = data.get("company", "")   # pass a specific company to limit scope
    uni     = data.get("university", "")

    def _run():
        from pipeline.lead_builder import build_leads
        n = build_leads(company_filter=company, university=uni)
        print(f"[admin/build-leads] done — {n} leads upserted")

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered", "company": company or "all"})
```

Then call it per-company to control cost:
```bash
curl -X POST "https://the-inroad.com/api/admin/build-leads?key=ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"company": "Goldman Sachs", "university": "UCL"}'
```

### Step 4: Re-enable scraping when ready
Set `SCRAPE_ENABLED=true` on Railway. Currently false — jobs table may be stale.

---

## Key Design Decisions to Know

**Why pre-fetch instead of live search:**
Serper costs $0.30/1k queries. Live search at card-gen time = 2 queries per student per company per day. With 100+ students × 3 cards = 600+ queries/day. Pre-fetching once per (company, dept) batch costs ~200 queries to cover all Trackr companies and is reused for 30 days.

**Query strategy (in `lead_builder.py`):**
- Query A: `site:linkedin.com/in "Goldman Sachs" "UCL" ("analyst" OR "associate")` — alumni first
- Query B: `site:linkedin.com/in "Goldman Sachs" "London" ("analyst" OR "associate")` — broad
- 2 pages per query (20 results total via Serper `page` param)
- Results merged, deduplicated by `linkedin_url`, upserted to `leads` table

**Training dataset:**
Every crawl run appends to `data/leads_training.jsonl`. Each line is one raw search result + parsed output. To build a validation set: copy ~50 lines, manually set `"verified": {...}` fields to ground truth. This lets you measure extraction accuracy and improve the regex/parsing over time.

**Scoring floor:**
Cards are skipped if `relevance_score < 50`. This means a thin lead pool can result in 0 cards rather than low-quality ones. Trade-off: better quality vs. fewer cards. Threshold is `MIN_LEAD_SCORE = 50` in `daily_cards.py:499`.

**Person deduplication:**
`get_seen_linkedin_urls(student_id)` queries the entire `matches` history, not just today. A person is never shown to the same student twice across any day.

**Company size scoring:**
Was always 0/8 before because Trackr never returned company sizes. Now populated via `COMPANY_SIZE_LOOKUP` in `config/settings.py` during Trackr scrape. ~40 companies covered. Extend this dict as needed.

---

## Environment Variables on Railway

| Var | Purpose | Current value |
|---|---|---|
| `SCRAPE_ENABLED` | Enable/disable daily scraper | `false` (paused) |
| `ADMIN_SECRET` | Key for `/admin` and `/api/admin/*` | Set — ask Oliver |
| `SERPER_API_KEY` | Google search via Serper | Set |
| `ANTHROPIC_API_KEY` | Claude for email drafts | Set |
| `RESEND_API_KEY` | Transactional email | Set |
| `FROM_EMAIL` | Sender address | `hello@contact.the-inroad.com` |
| `DATABASE_URL` | Postgres connection | Set (Railway internal) |
| `APP_BASE_URL` | Public URL for magic links | Set |

---

## File Map (relevant files only)

```
inroad/
├── api/server.py              # Flask app — all routes
├── config/settings.py         # All constants: COMPANY_SIZE_LOOKUP, DEPT_MAP, etc.
├── db/database.py             # DB layer — schema, migrations, all query helpers
├── pipeline/
│   ├── daily_cards.py         # Card generation — main loop, scoring, Claude drafts
│   ├── lead_builder.py        # NEW — Serper crawler, builds leads table
│   ├── matcher.py             # LinkedIn search + lead scoring (score_lead_v2)
│   ├── email_templates.py     # Template fallback for email drafts
│   └── ingest.py              # Runs scrapers, calls upsert_job
├── scrapers/
│   ├── trackr.py              # Trackr API scraper (primary job source)
│   └── base.py                # BaseScraper, make_job, clean_date, etc.
├── scheduler/
│   └── run.py                 # Daily cron: scrape@06:00, cards@07:00 UTC
├── html/
│   ├── ccc-admin.html         # Admin panel (requires ?key=ADMIN_SECRET)
│   ├── ccc-dashboard-live.html# Student dashboard
│   ├── ccc-onboarding.html    # Onboarding flow
│   └── login.html             # Magic link login
└── static/
    ├── auth.js                # Client-side JWT helpers (setAccessToken, apiFetch)
    └── favicon.svg            # Brand icon served as file
```

---

## Immediate Next Action

1. Run a dry-run crawl for one company to validate parsing quality:
   ```bash
   SERPER_API_KEY=xxx python pipeline/lead_builder.py --company "Goldman Sachs" --university "UCL" --dry-run
   ```
2. Check `data/leads_training.jsonl` — are name/title/company/city/university being extracted correctly?
3. If quality is good, run for real (remove `--dry-run`) and test card generation.
4. Re-enable the `/api/admin/build-leads` endpoint in `server.py` (see Step 3 above) so crawls can be triggered from the admin panel per-company.
5. Once lead pool has 50+ leads for a company, re-enable scraping (`SCRAPE_ENABLED=true`) and let the full daily pipeline run.
