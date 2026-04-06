# inroad V0 Roadmap
_Last updated: 2026-04-06_

---

## Vision
A laser-focused tool for students seeking **internships, graduate programmes, and entry-level roles** in **Finance, Technology, and Law**. Gold-standard job data → matched employee contacts → AI-drafted cold outreach. Dead simple. No noise.

---

## Phase 1 — Gold Standard Job Scrapes

Get real, entry-level jobs from Trackr and Greenhouse into the database.

### Ticket 1.1 — Trackr: trigger scrape `[READY]`
Trackr uses a public JSON API (`api.the-trackr.com/programmes`) — no auth required. The scraper pulls 913+ open programmes across Finance, Tech, and Law for UK/US/EU.

```bash
curl -X POST https://inroad-production.up.railway.app/api/admin/scrape \
  -H "Content-Type: application/json" -d '{"source_id": "trackr"}'
```

Check Railway logs for per-bucket counts, e.g. `"Trackr [UK/Finance/2026/summer-internships]: yielded 203 open programmes"`.

### Ticket 1.2 — Greenhouse: trigger scrape `[READY]`
Greenhouse uses the free public ATS API. All jobs are filtered through `_is_entry_level()` before being stored.

```bash
curl -X POST https://inroad-production.up.railway.app/api/admin/scrape \
  -H "Content-Type: application/json" -d '{"source_id": "greenhouse_feed"}'
```

### Ticket 1.3 — Verify jobs in DB `[READY after 1.1–1.2]`
```bash
curl https://inroad-production.up.railway.app/api/admin/stats
```
Expected: `active_jobs > 0`, `by_source` shows non-zero for `trackr` and `greenhouse_feed`.

**Done when:** 50+ active jobs across Finance / Tech / Law in the DB.

---

## Phase 2 — Employee Scrapes

Wire up PDL so the matcher can find real employees at the companies in the jobs DB.

The matcher runs on-demand per job during card generation — it queries PDL in real time rather than pre-scraping. One API key is all that's needed.

### Ticket 2.1 — Set PDL API key `[BLOCKED — user action]`
PDL (People Data Labs) returns name, title, company, education history, and LinkedIn URL in a single query. Education history is what powers the alumni detection.

**Steps:**
1. Sign up at `https://www.peopledatalabs.com`
2. Go to API Keys → copy your key
3. Railway → Variables: `PDL_API_KEY=<key>`

**Test the key** (after setting):
```bash
railway run python -c "
from pipeline.matcher import LinkedInMatcher
m = LinkedInMatcher()
leads = m.find_leads('Goldman Sachs', 'Summer Analyst', 'UCL', n=3)
for l in leads:
    print(l.get('name'), '|', l.get('title'), '|', l.get('linkedin_url'))
"
```
Expected: list of 3 dicts with `name`, `title`, `linkedin_url` populated.

### Ticket 2.2 — Verify matcher returns leads `[READY after 2.1]`
```bash
railway run python -c "
from pipeline.matcher import LinkedInMatcher
m = LinkedInMatcher()
for company in ['Stripe', 'Monzo', 'Goldman Sachs']:
    leads = m.find_leads(company, 'Software Engineer Intern', 'UCL', n=3)
    print(company, '->', len(leads), 'leads')
"
```
**Done when:** Each company returns ≥ 2 leads.

---

## Phase 3 — Employee Match Function + Update Cards

Run the full pipeline end-to-end: jobs → matched contacts → AI email drafts → dashboard cards.

### Ticket 3.1 — Confirm ANTHROPIC_API_KEY is set `[TODO — check Railway Variables]`
```bash
railway run python -c "import os; print('OK' if os.environ.get('ANTHROPIC_API_KEY') else 'MISSING')"
```

### Ticket 3.2 — Get your student ID `[READY]`
```bash
curl https://inroad-production.up.railway.app/api/admin/students
```

### Ticket 3.3 — Run card generation for your account `[READY after Phase 1 + Phase 2]`
```bash
railway run python pipeline/daily_cards.py --student-id <your_id>
```

**What this does:**
1. Fetches active jobs matching your industries (Finance / Tech / Law), region (UK), last 21 days
2. Scores and deduplicates jobs — picks up to 3 best candidates
3. For each job, calls `find_leads()` to find real employees at that company
4. Scores leads — prioritises alumni, title relevance, seniority gap
5. Infers or retrieves contact email
6. Calls Claude to draft a personalised cold email
7. Writes up to 3 match records to the `matches` table

### Ticket 3.4 — Verify cards appear on dashboard `[READY after 3.3]`
Reload `https://inroad-production.up.railway.app/dashboard`.

If cards don't appear, debug via API:
```bash
curl -H "Authorization: Bearer <your_token>" \
  https://inroad-production.up.railway.app/api/matches/today/<your_id>
```

### Ticket 3.5 — Confirm scheduler is running `[READY]`
```bash
curl https://inroad-production.up.railway.app/api/admin/runs
```
Should show recent `scrape_runs` entries. Scrapes run at 06:00 UTC daily.

---

## Dependency Graph

```
Phase 1 (Jobs in DB)
  ├── Ticket 1.1: Trackr scrape          [one curl command]
  └── Ticket 1.2: Greenhouse scrape      [one curl command]
        │
        ▼ (≥ 50 active jobs in DB)
Phase 2 (Matcher working)
  └── Ticket 2.1: PDL API key            [user → Railway Variables]
        │
        ▼ (find_leads() returns ≥ 2 results per company)
Phase 3 (Cards on dashboard)
  ├── Ticket 3.1: ANTHROPIC_API_KEY      [check Railway]
  ├── Ticket 3.3: Run daily_cards.py     [one command]
  └── Ticket 3.4: Verify dashboard       [visual check]
```

---

## Immediate Next Steps

| Priority | Ticket | Owner | Action |
|---|---|---|---|
| 🔴 P0 | 2.1 | Oliver | Set `PDL_API_KEY` in Railway Variables |
| 🟡 P1 | 1.1 | Oliver | Trigger Trackr scrape |
| 🟡 P1 | 1.2 | Oliver | Trigger Greenhouse scrape |
| 🟢 P2 | 3.3 | Oliver | Run `python pipeline/daily_cards.py --student-id <id>` once Phase 1+2 done |

---

## Architecture (V0)

```
Trackr     (Gold) ──┐
Greenhouse (Gold) ──┴──► ingest pipeline ──► jobs table (entry-level filter)

jobs table ──► daily_cards.py ──► PDL ──► find_leads()
                                       └──► email_infer.py
                                       └──► Claude email draft
                                       └──► matches table

matches table ──► GET /api/matches/today ──► dashboard cards ──► user sends email
```

---

## Dropped from V0

| Source | Reason |
|---|---|
| Welcome to the Jungle | French job board — only 5 UK internships in their English index. Not suitable for UK-focused V0. |
