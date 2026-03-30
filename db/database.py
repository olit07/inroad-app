"""
db/database.py
Supports both Postgres (Railway production) and SQLite (local dev).
Set DATABASE_URL env var to a postgres:// connection string for Postgres.
Falls back to SQLite at ccc.db if DATABASE_URL is not set.
"""

import os
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
    id          SERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    name        TEXT,
    age         INTEGER,
    status      TEXT,
    industries  TEXT,
    company_size TEXT,
    bio         TEXT,
    university  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    last_seen   TIMESTAMPTZ
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
    contact_name    TEXT,
    contact_email   TEXT,
    contact_linkedin TEXT,
    is_alumni       BOOLEAN DEFAULT FALSE,
    email_subject   TEXT,
    email_body      TEXT,
    status          TEXT DEFAULT 'pending',
    sent_at         TIMESTAMPTZ,
    replied_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
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

CREATE INDEX IF NOT EXISTS idx_magic_tokens_token   ON magic_tokens(token);
CREATE INDEX IF NOT EXISTS idx_magic_tokens_email   ON magic_tokens(email);
CREATE INDEX IF NOT EXISTS idx_matches_student      ON matches(student_id);
CREATE INDEX IF NOT EXISTS idx_signals_match        ON signals(match_id);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS students (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT UNIQUE NOT NULL,
    name         TEXT,
    age          INTEGER,
    status       TEXT,
    industries   TEXT,
    company_size TEXT,
    bio          TEXT,
    university   TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    last_seen    TEXT
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
    contact_name     TEXT,
    contact_email    TEXT,
    contact_linkedin TEXT,
    is_alumni        INTEGER DEFAULT 0,
    email_subject    TEXT,
    email_body       TEXT,
    status           TEXT DEFAULT 'pending',
    sent_at          TEXT,
    replied_at       TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
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

CREATE INDEX IF NOT EXISTS idx_magic_tokens_token ON magic_tokens(token);
CREATE INDEX IF NOT EXISTS idx_magic_tokens_email ON magic_tokens(email);
CREATE INDEX IF NOT EXISTS idx_matches_student    ON matches(student_id);
CREATE INDEX IF NOT EXISTS idx_signals_match      ON signals(match_id);
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
