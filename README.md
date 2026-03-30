# Coffee Chat Connect — Backend

Scraping, matching, and daily card generation for Coffee Chat Connect.
Helps university students get jobs via targeted cold-email coffee chats.

## Quick start

```bash
cd ccc-backend
python3 setup.py          # init DB, seed ATS targets, create .env template
source .env.template      # fill in your keys first
python3 cli.py scrape --source greenhouse_feed   # pull live jobs (no keys needed)
python3 cli.py stats                             # check DB health
python3 api/server.py                            # start API on :5001
```

Then open `ccc-admin.html` in your browser.

## Architecture

```
ccc-backend/
├── setup.py                 ← Run this first
├── cli.py                   ← Admin CLI
├── .env.template            ← Copy and fill in your API keys
│
├── config/settings.py       ← Constants, industry list, source registry
├── db/database.py           ← SQLite schema, helpers, FTS5 search, stats
│
├── scrapers/                ← 8 job sources
│   ├── greenhouse.py        ← 60+ companies, no key needed
│   ├── lever.py             ← 30+ companies, no key needed
│   ├── workday.py           ← 25 major employers
│   ├── handshake.py         ← UK + US
│   ├── wttj.py              ← Welcome to the Jungle
│   ├── reed.py              ← [REED_API_KEY]
│   ├── adzuna.py            ← [ADZUNA_APP_ID + ADZUNA_APP_KEY]
│   └── trackr.py            ← [TRACKR_SESSION_COOKIE]
│
├── pipeline/
│   ├── ingest.py            ← Scraper orchestration
│   ├── matcher.py           ← LinkedIn search, scoring, company variants
│   ├── profile_cache.py     ← 7-day SQLite profile cache
│   ├── email_infer.py       ← Email pattern inference
│   ├── email_templates.py   ← 5 templates + quality scorer
│   ├── daily_cards.py       ← Daily 3-card generation
│   └── reply_tracker.py     ← Sentiment, digest, A/B cohorts
│
├── utils/
│   ├── university_detector.py   ← 130+ UK/US domain mappings
│   ├── notifications.py         ← SMTP magic link + match emails
│   └── company_enrichment.py   ← Domain, size, sector data
│
├── api/
│   ├── server.py            ← Flask API (26 endpoints)
│   └── rate_limit.py        ← Sliding window rate limiter
│
└── scheduler/run.py         ← 06:00 scrape + 07:00 cards
```

## Keys needed

| Key | Purpose | Free tier |
|---|---|---|
| `ANTHROPIC_API_KEY` | Email drafts | pay per use |
| `BRAVE_SEARCH_API_KEY` | LinkedIn matching | ~1k queries/$5 credit |
| `SMTP_USER` + `SMTP_PASS` | Notification emails | Gmail App Password |
| `REED_API_KEY` | More UK jobs | free |
| `ADZUNA_APP_ID/KEY` | More jobs | 250 calls/mo free |

## CLI

```bash
python3 cli.py stats                    # DB health
python3 cli.py scrape                   # run all scrapers
python3 cli.py students                 # list students + stats
python3 cli.py cards --student-id 1     # generate today's cards
python3 cli.py enrich                   # enrich companies
python3 cli.py notify 1 --type matches  # send notification
python3 cli.py search "goldman analyst" # FTS search
python3 cli.py jobs --industry Finance  # list jobs
python3 cli.py purge --days 45          # clean stale jobs
```

## Architecture

```
ccc-backend/
├── config/
│   └── settings.py          # constants, source registry, industry list
├── db/
│   └── database.py          # SQLite schema, helpers, stats
├── scrapers/
│   ├── base.py              # BaseScraper, HTTP helpers, normalisation
│   ├── greenhouse.py        # Greenhouse ATS (60+ companies, no key needed)
│   ├── lever.py             # Lever ATS (30+ companies, no key needed)
│   ├── reed.py              # Reed.co.uk API  [REED_API_KEY]
│   ├── trackr.py            # Bristol Trackr  [TRACKR_SESSION_COOKIE]
│   ├── wttj.py              # Welcome to the Jungle (public API)
│   ├── adzuna.py            # Adzuna API      [ADZUNA_APP_ID + ADZUNA_APP_KEY]
│   └── __init__.py          # scraper registry
├── pipeline/
│   ├── ingest.py            # orchestrates scrapers → DB
│   ├── matcher.py           # LinkedIn lead matching + scoring
│   ├── email_infer.py       # email address inference
│   └── daily_cards.py       # daily 3-card generation per student
├── scheduler/
│   └── run.py               # cron daemon: scrape@06:00, cards@07:00 UTC
├── cli.py                   # admin CLI
└── data/
    └── ccc.db               # SQLite database (auto-created)
```

## Setup

```bash
cd ccc-backend
python3 cli.py init          # create DB schema
```

## Environment variables

| Variable | Required | Source | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | ✓ | anthropic.com | Email draft generation (Claude) |
| `BRAVE_SEARCH_API_KEY` | ✓* | portal.azure.com | LinkedIn profile search |
| `SERPAPI_KEY` | ✓* | serpapi.com | Alternative to Bing |
| `REED_API_KEY` | optional | reed.co.uk/developers | Reed job listings |
| `ADZUNA_APP_ID` | optional | developer.adzuna.com | Adzuna job listings |
| `ADZUNA_APP_KEY` | optional | developer.adzuna.com | Adzuna job listings |
| `TRACKR_SESSION_COOKIE` | optional | Browser DevTools | Bristol Trackr (requires login) |
| `HUNTER_API_KEY` | optional | hunter.io | Email verification |

*One of Bing or SerpAPI is required for LinkedIn matching.

Set in a `.env` file or export before running:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export BRAVE_SEARCH_API_KEY=...
```

## CLI usage

```bash
# Initialise DB
python3 cli.py init

# Run all scrapers (works without any API keys via Greenhouse + Lever)
python3 cli.py scrape

# Run a specific scraper
python3 cli.py scrape --source greenhouse_feed
python3 cli.py scrape --source lever_feed

# DB stats
python3 cli.py stats

# Search jobs
python3 cli.py search "goldman analyst"
python3 cli.py jobs --industry Finance --region UK --days 14

# Purge old inactive jobs
python3 cli.py purge --days 45
```

## Running the scheduler

```bash
# Run daemon (blocks, runs daily at 06:00 + 07:00 UTC)
python3 scheduler/run.py

# One-off full run (scrape + cards)
python3 scheduler/run.py --once

# Scrape only
python3 scheduler/run.py --scrape

# Generate cards only  
python3 scheduler/run.py --cards
```

## Generating daily cards for a student

```bash
# After inserting a student into the DB:
python3 pipeline/daily_cards.py --student-id 1
```

## Data flow

```
Daily at 06:00 UTC:
  Scrapers → normalise → deduplicate → jobs table

Daily at 07:00 UTC:
  For each student:
    jobs table (filtered by preferences)
      → LinkedIn search (Bing API)
      → parse profiles → score leads
      → infer email (patterns + Hunter.io)
      → Claude drafts email
      → write to matches table

Dashboard reads from:
  matches WHERE student_id=? AND match_date=today
```

## Source tiers

| Tier | Sources | Notes |
|---|---|---|
| 1 (API) | Greenhouse, Lever, Reed, Adzuna | Structured JSON, reliable |
| 2 (scrape) | Bristol Trackr, WTTJ | HTML parse, may break on DOM changes |
| 3 (manual) | Custom imports | CSV/JSON import via CLI (future) |

## Legal notes

- Greenhouse and Lever ATS feeds are publicly documented and intended for programmatic access.
- Reed and Adzuna have official developer APIs with ToS-compliant access.
- Bristol Trackr scraping requires a valid logged-in session — personal use only.
- LinkedIn profile data is sourced via Bing Search API from public search results. Profile data is treated as ephemeral (not stored permanently — anonymised after 90 days).
- Email inference uses pattern matching. Students send emails manually — CCC is the drafter, not the sender.
