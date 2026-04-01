"""
db/database.py
Supports both Postgres (Railway production) and SQLite (local dev).
Set DATABASE_URL env var to a postgres:// connection string for Postgres.
Falls back to SQLite at ccc.db if DATABASE_URL is not set.
"""

import os
import json
import sqlite3
import contextlib

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Railway gives postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

USE_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith("postgresql://"))

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    print("[db] Using Postgres:", DATABASE_URL[:40] + "...")
else:
    SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "ccc.db")
    print("[db] Using SQLite:", SQLITE_PATH)


# ── Connection helpers ──────────────────────────────────────────────────────

@contextlib.contextmanager
def get_conn():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def execute(sql, params=None):
    """Run a write query (INSERT/UPDATE/DELETE). Returns lastrowid / rowcount."""
    # Normalise ? placeholders to %s for Postgres
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if USE_POSTGRES:
            return cur.rowcount
        return cur.lastrowid


def fetchone(sql, params=None):
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return dict(row) if row else None


def fetchall(sql, params=None):
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        return [dict(r) for r in rows]


# ── Schema ──────────────────────────────────────────────────────────────────

# Use %s placeholders in Postgres, ? in SQLite
def _ph():
    return "%s" if USE_POSTGRES else "?"


SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS students (
    id             SERIAL PRIMARY KEY,
    email          TEXT UNIQUE NOT NULL,
    name           TEXT,
    age            INTEGER,
    status         TEXT,
    industries     TEXT,
    company_size   TEXT,
    bio            TEXT,
    university     TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    last_seen      TIMESTAMPTZ,
    deactivated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS magic_tokens (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    token       TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS jobs (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    url         TEXT,
    location    TEXT,
    industry    TEXT,
    company_size TEXT,
    posted_at   TIMESTAMPTZ,
    source      TEXT,
    raw         TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS matches (
    id              SERIAL PRIMARY KEY,
    student_id      INTEGER REFERENCES students(id),
    job_id          INTEGER REFERENCES jobs(id),
    match_date      DATE,
    contact_name    TEXT,
    contact_email   TEXT,
    contact_linkedin TEXT,
    is_alumni       BOOLEAN DEFAULT FALSE,
    email_subject   TEXT,
    email_body      TEXT,
    status          TEXT DEFAULT 'pending',
    sent_at         TIMESTAMPTZ,
    replied_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(student_id, job_id)
);

CREATE TABLE IF NOT EXISTS email_log (
    id          SERIAL PRIMARY KEY,
    to_email    TEXT NOT NULL,
    subject     TEXT,
    kind        TEXT,
    resend_id   TEXT,
    sent_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS signals (
    id          SERIAL PRIMARY KEY,
    match_id    INTEGER REFERENCES matches(id),
    student_id  INTEGER REFERENCES students(id),
    signal      TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS suppressions (
    id          SERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          SERIAL PRIMARY KEY,
    token       TEXT UNIQUE NOT NULL,
    student_id  INTEGER REFERENCES students(id),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS card_queue (
    id          SERIAL PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id),
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    score       REAL NOT NULL,
    queued_for  DATE NOT NULL,
    consumed    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(student_id, job_id, queued_for)
);

CREATE INDEX IF NOT EXISTS idx_magic_tokens_token   ON magic_tokens(token);
CREATE INDEX IF NOT EXISTS idx_magic_tokens_email   ON magic_tokens(email);
CREATE INDEX IF NOT EXISTS idx_matches_student      ON matches(student_id);
CREATE INDEX IF NOT EXISTS idx_signals_match        ON signals(match_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token   ON refresh_tokens(token);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_student ON refresh_tokens(student_id);
CREATE INDEX IF NOT EXISTS idx_card_queue_student_date ON card_queue(student_id, queued_for);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS students (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT UNIQUE NOT NULL,
    name           TEXT,
    age            INTEGER,
    status         TEXT,
    industries     TEXT,
    company_size   TEXT,
    bio            TEXT,
    university     TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    last_seen      TEXT,
    deactivated_at TEXT
);

CREATE TABLE IF NOT EXISTS magic_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    token      TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    url          TEXT,
    location     TEXT,
    industry     TEXT,
    company_size TEXT,
    posted_at    TEXT,
    source       TEXT,
    raw          TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id       INTEGER REFERENCES students(id),
    job_id           INTEGER REFERENCES jobs(id),
    match_date       TEXT,
    contact_name     TEXT,
    contact_email    TEXT,
    contact_linkedin TEXT,
    is_alumni        INTEGER DEFAULT 0,
    email_subject    TEXT,
    email_body       TEXT,
    status           TEXT DEFAULT 'pending',
    sent_at          TEXT,
    replied_at       TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(student_id, job_id)
);

CREATE TABLE IF NOT EXISTS email_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email  TEXT NOT NULL,
    subject   TEXT,
    kind      TEXT,
    resend_id TEXT,
    sent_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id   INTEGER REFERENCES matches(id),
    student_id INTEGER REFERENCES students(id),
    signal     TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suppressions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE NOT NULL,
    reason     TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT UNIQUE NOT NULL,
    student_id INTEGER REFERENCES students(id),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS card_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id),
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    score       REAL NOT NULL,
    queued_for  TEXT NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(student_id, job_id, queued_for)
);

CREATE INDEX IF NOT EXISTS idx_magic_tokens_token ON magic_tokens(token);
CREATE INDEX IF NOT EXISTS idx_magic_tokens_email ON magic_tokens(email);
CREATE INDEX IF NOT EXISTS idx_matches_student    ON matches(student_id);
CREATE INDEX IF NOT EXISTS idx_signals_match      ON signals(match_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token   ON refresh_tokens(token);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_student ON refresh_tokens(student_id);
CREATE INDEX IF NOT EXISTS idx_card_queue_student_date ON card_queue(student_id, queued_for);
"""


def init_db():
    """Create all tables. Safe to call on every startup (IF NOT EXISTS)."""
    schema = SCHEMA_POSTGRES if USE_POSTGRES else SCHEMA_SQLITE
    with get_conn() as conn:
        cur = conn.cursor()
        # Postgres needs statements run individually
        for statement in [s.strip() for s in schema.split(";") if s.strip()]:
            cur.execute(statement)
    print("[db] Schema initialised.")


# ── Convenience wrappers used by api/server.py ──────────────────────────────

def get_student_by_email(email):
    return fetchone("SELECT * FROM students WHERE email = ?", (email,))


def get_student_by_id(student_id):
    return fetchone("SELECT * FROM students WHERE id = ?", (student_id,))


def create_student(email, university=None):
    execute(
        "INSERT INTO students (email, university) VALUES (?, ?) ON CONFLICT (email) DO NOTHING",
        (email, university)
    )
    return get_student_by_email(email)


def upsert_student_profile(email, name, age, status, industries, company_size, bio, university):
    if USE_POSTGRES:
        execute("""
            INSERT INTO students (email, name, age, status, industries, company_size, bio, university)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (email) DO UPDATE SET
                name=EXCLUDED.name, age=EXCLUDED.age, status=EXCLUDED.status,
                industries=EXCLUDED.industries, company_size=EXCLUDED.company_size,
                bio=EXCLUDED.bio, university=EXCLUDED.university
        """, (email, name, age, status, industries, company_size, bio, university))
    else:
        execute("""
            INSERT INTO students (email, name, age, status, industries, company_size, bio, university)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name=excluded.name, age=excluded.age, status=excluded.status,
                industries=excluded.industries, company_size=excluded.company_size,
                bio=excluded.bio, university=excluded.university
        """, (email, name, age, status, industries, company_size, bio, university))
    return get_student_by_email(email)


def create_magic_token(email, token, expires_at):
    execute(
        "INSERT INTO magic_tokens (email, token, expires_at) VALUES (?, ?, ?)",
        (email, token, expires_at)
    )


def get_and_consume_token(token):
    """Atomically fetch and mark a token as used. Returns token row or None."""
    if USE_POSTGRES:
        row = fetchone("""
            UPDATE magic_tokens SET used_at = NOW()
            WHERE token = ? AND used_at IS NULL AND expires_at > NOW()
            RETURNING *
        """, (token,))
    else:
        row = fetchone("""
            SELECT * FROM magic_tokens
            WHERE token = ? AND used_at IS NULL
              AND expires_at > datetime('now')
        """, (token,))
        if row:
            execute(
                "UPDATE magic_tokens SET used_at = datetime('now') WHERE token = ?",
                (token,)
            )
    return row


def log_email(to_email, subject, kind, resend_id=None):
    execute(
        "INSERT INTO email_log (to_email, subject, kind, resend_id) VALUES (?, ?, ?, ?)",
        (to_email, subject, kind, resend_id)
    )


def count_recent_tokens(email, minutes=10):
    if USE_POSTGRES:
        row = fetchone("""
            SELECT COUNT(*) as cnt FROM magic_tokens
            WHERE email = ? AND created_at > NOW() - INTERVAL '%s minutes'
        """ % minutes, (email,))
    else:
        row = fetchone("""
            SELECT COUNT(*) as cnt FROM magic_tokens
            WHERE email = ?
              AND created_at > datetime('now', ? || ' minutes')
        """, (email, f"-{minutes}"))
    return row["cnt"] if row else 0


def update_student_fields(student_id: int, fields: dict) -> None:
    """Update only the provided fields on a student row (partial update)."""
    if not fields:
        return
    # Serialise list values (e.g. industries) to JSON strings
    normalised = {}
    for k, v in fields.items():
        normalised[k] = json.dumps(v) if isinstance(v, list) else v
    set_clause = ", ".join(f"{col} = ?" for col in normalised)
    values = list(normalised.values()) + [student_id]
    execute(
        f"UPDATE students SET {set_clause} WHERE id = ?",
        values,
    )


def create_refresh_token(student_id, token, expires_at):
    execute(
        "INSERT INTO refresh_tokens (student_id, token, expires_at) VALUES (?, ?, ?)",
        (student_id, token, expires_at)
    )


def get_refresh_token(token):
    """Fetch a refresh token row by token string. Returns dict or None."""
    return fetchone("SELECT * FROM refresh_tokens WHERE token = ?", (token,))


def revoke_refresh_token(token):
    """Set revoked_at = now on a single refresh token."""
    if USE_POSTGRES:
        execute(
            "UPDATE refresh_tokens SET revoked_at = NOW() WHERE token = ?",
            (token,)
        )
    else:
        execute(
            "UPDATE refresh_tokens SET revoked_at = datetime('now') WHERE token = ?",
            (token,)
        )


def revoke_all_tokens_for_student(student_id):
    """Revoke all refresh tokens belonging to a student."""
    if USE_POSTGRES:
        execute(
            "UPDATE refresh_tokens SET revoked_at = NOW() WHERE student_id = ? AND revoked_at IS NULL",
            (student_id,)
        )
    else:
        execute(
            "UPDATE refresh_tokens SET revoked_at = datetime('now') WHERE student_id = ? AND revoked_at IS NULL",
            (student_id,)
        )


def deactivate_student(student_id):
    """Set deactivated_at = now on the student row."""
    if USE_POSTGRES:
        execute(
            "UPDATE students SET deactivated_at = NOW() WHERE id = ?",
            (student_id,)
        )
    else:
        execute(
            "UPDATE students SET deactivated_at = datetime('now') WHERE id = ?",
            (student_id,)
        )


# ── Pipeline helpers ─────────────────────────────────────────────────────────

# Alias used by pipeline/daily_cards.py and cli.py
db_conn = get_conn


def get_active_jobs(conn, industries=None, region=None, seniority=None,
                    days_fresh=21, limit=100):
    """
    Return active jobs as a list of dicts, optionally filtered by industries,
    region, seniority, and recency.

    Designed to be called with an already-open connection so callers can batch
    it with other queries inside the same transaction.
    """
    ph = "%s" if USE_POSTGRES else "?"

    clauses = []
    params  = []

    if days_fresh:
        if USE_POSTGRES:
            clauses.append(f"posted_at > NOW() - INTERVAL '{days_fresh} days'")
        else:
            clauses.append(f"posted_at > datetime('now', '-{days_fresh} days')")

    if industries:
        placeholders = ", ".join([ph] * len(industries))
        clauses.append(f"industry IN ({placeholders})")
        params.extend(industries)

    if region:
        clauses.append(f"location LIKE {ph}")
        params.append(f"%{region}%")

    if seniority:
        clauses.append(f"raw LIKE {ph}")
        params.append(f"%{seniority}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql   = f"""
        SELECT id, title, company AS company_name, url, location, industry,
               company_size, posted_at AS posted_date, source, raw
        FROM   jobs
        {where}
        ORDER  BY posted_at DESC
        LIMIT  {ph}
    """
    params.append(limit)

    if USE_POSTGRES:
        sql = sql.replace("?", "%s")

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_card_count_today(student_id: int) -> int:
    """Return how many cards have been generated for this student today."""
    from datetime import date as _date
    today = _date.today().isoformat()
    if USE_POSTGRES:
        row = fetchone(
            "SELECT COUNT(*) AS cnt FROM matches WHERE student_id = ? AND match_date = CAST(? AS DATE)",
            (student_id, today),
        )
    else:
        row = fetchone(
            "SELECT COUNT(*) AS cnt FROM matches WHERE student_id = ? AND match_date = ?",
            (student_id, today),
        )
    return int(row["cnt"]) if row else 0


def enqueue_card(student_id, job_id, score, queued_for):
    """Insert a card into the queue; ignore if already queued for that date."""
    if USE_POSTGRES:
        execute(
            """INSERT INTO card_queue (student_id, job_id, score, queued_for)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (student_id, job_id, queued_for) DO NOTHING""",
            (student_id, job_id, score, queued_for),
        )
    else:
        execute(
            """INSERT OR IGNORE INTO card_queue (student_id, job_id, score, queued_for)
               VALUES (?, ?, ?, ?)""",
            (student_id, job_id, score, queued_for),
        )


def get_queued_cards(student_id, date_str):
    """Return unconsumed queued cards for a student on a given date, score DESC."""
    if USE_POSTGRES:
        return fetchall(
            """SELECT * FROM card_queue
               WHERE student_id = ? AND queued_for = ? AND consumed = FALSE
               ORDER BY score DESC""",
            (student_id, date_str),
        )
    else:
        return fetchall(
            """SELECT * FROM card_queue
               WHERE student_id = ? AND queued_for = ? AND consumed = 0
               ORDER BY score DESC""",
            (student_id, date_str),
        )


def mark_card_consumed(card_queue_id):
    """Mark a card_queue row as consumed."""
    if USE_POSTGRES:
        execute(
            "UPDATE card_queue SET consumed = TRUE WHERE id = ?",
            (card_queue_id,),
        )
    else:
        execute(
            "UPDATE card_queue SET consumed = 1 WHERE id = ?",
            (card_queue_id,),
        )
