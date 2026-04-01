# Matching Endpoint Performance Benchmark

Load test for `GET /api/matches/today/<student_id>` using [Locust](https://locust.io).

---

## Prerequisites

```bash
pip install locust==2.29.1
```

The server must be running in **DEV_MODE** (`DEV_MODE=true` in your `.env` or environment).
DEV_MODE causes `/auth/magic-link` to return a `dev_token` in its JSON response so the
benchmark can authenticate without sending real emails.

---

## How to run

```bash
# Start the server (separate terminal)
python api/server.py          # or: gunicorn --bind 0.0.0.0:5001 api.server:app

# Run the benchmark (headless, 60-second ramp + sustain)
locust -f tests/perf/locustfile.py \
       --host=http://localhost:5001 \
       --users=1000 \
       --spawn-rate=50 \
       --run-time=60s \
       --headless
```

### Interactive (web UI)

```bash
locust -f tests/perf/locustfile.py --host=http://localhost:5001
# then open http://localhost:8089
```

---

## What the p99 < 300 ms target means

**p99** is the 99th-percentile response time: 99 % of all requests complete within that
duration. A p99 of 300 ms means that only 1 in 100 requests may take longer than 300 ms.

This is the SLA target for `GET /api/matches/today/<student_id>` at 1,000 concurrent
virtual users. The benchmark exits with code 1 if the target is missed, making it
suitable for use in CI.

---

## How to interpret results

| Field | What it tells you |
|-------|-------------------|
| **Requests** | Total HTTP requests made during the run |
| **Failures** | Non-2xx responses (auth errors, 500s, timeouts) |
| **Median** | Typical latency under load |
| **p95** | 95 % of requests are faster than this |
| **p99** | The SLA gate — must be <= 300 ms |
| **Max** | Worst single request; outliers are expected |

A high failure count almost always means the server ran out of DB connections or
crashed under load — check server logs first.

---

## Common bottlenecks if the target is missed

### 1. Missing database index on `matches`

The matches query filters on `student_id` and `DATE(created_at)`.  Without an index
the DB does a full-table scan on every request.

```sql
-- SQLite
CREATE INDEX IF NOT EXISTS idx_matches_student_date
    ON matches (student_id, created_at);

-- PostgreSQL
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_matches_student_date
    ON matches (student_id, created_at);
```

If a card-queue table is used upstream (e.g. `card_queue`), add:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_card_queue_student_date
    ON card_queue (student_id, queued_date);
```

### 2. N+1 queries on the matches endpoint

`GET /api/matches/today/<student_id>` currently does a single JOIN query, which is
correct.  If additional per-match fetches are added later (e.g. fetching contact
details one-by-one inside a loop), the query count will scale with the number of
matches returned.  Use `EXPLAIN ANALYZE` / `EXPLAIN QUERY PLAN` to confirm only one
query runs per request.

### 3. Connection pool exhaustion

At 1,000 concurrent users each making requests simultaneously, the DB connection pool
can become the bottleneck.  With `psycopg2` (PostgreSQL) or `sqlite3` (SQLite):

- **PostgreSQL**: set `pool_size` and `max_overflow` in SQLAlchemy, or use PgBouncer
  in transaction-pooling mode.
- **SQLite**: SQLite serialises writes; consider switching to PostgreSQL for load
  above ~100 concurrent users.

### 4. Flask single-process mode

`python api/server.py` runs a single-threaded dev server.  Use Gunicorn with multiple
workers for realistic load testing:

```bash
gunicorn --bind 0.0.0.0:5001 --workers=4 --threads=2 api.server:app
```

### 5. Magic-link rate limiting

The benchmark creates unique email addresses (`perftest_<random>@example.com`) to
avoid hitting the 3-requests-per-10-minute rate limit per email.  If you lower the
random range, virtual users may collide and receive 429 responses, inflating the
failure count.
