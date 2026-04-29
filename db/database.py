"""
db/database.py
Supports both Postgres (Railway production) and SQLite (local dev).
Set DATABASE_URL env var to a postgres:// connection string for Postgres.
Falls back to SQLite at inroad.db if DATABASE_URL is not set.
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
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _ccc  = os.path.join(_root, "ccc.db")
    SQLITE_PATH = _ccc if os.path.exists(_ccc) else os.path.join(_root, "inroad.db")
    print("[db] Using SQLite:", SQLITE_PATH)


# ── Connection helpers ──────────────────────────────────────────────────────

@contextlib.contextmanager
def get_conn(db_path=None):
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
    university          TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ,
    deactivated_at      TIMESTAMPTZ,
    notify_matches      BOOLEAN NOT NULL DEFAULT FALSE,
    notify_frequency    TEXT    NOT NULL DEFAULT 'daily',
    referral_code       TEXT UNIQUE,
    referred_by         TEXT,
    daily_cards_override INTEGER DEFAULT NULL,
    region              TEXT NOT NULL DEFAULT 'UK'
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
    id           SERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    url          TEXT,
    location     TEXT,
    industry     TEXT,
    company_size TEXT,
    posted_at    TIMESTAMPTZ,
    opening_date TEXT,
    closing_date TEXT,
    source       TEXT,
    raw          TEXT,
    role_type    TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS matches (
    id                   SERIAL PRIMARY KEY,
    student_id           INTEGER REFERENCES students(id),
    job_id               INTEGER REFERENCES jobs(id),
    match_date           DATE,
    person_name          TEXT,
    person_title         TEXT,
    person_company       TEXT,
    person_linkedin_url  TEXT,
    person_university    TEXT,
    person_tenure_months INTEGER,
    is_alumni            BOOLEAN DEFAULT FALSE,
    relevance_score      REAL,
    score_breakdown      TEXT,
    expected_email       TEXT,
    email_confidence     REAL,
    email_subject        TEXT,
    email_body           TEXT,
    status               TEXT DEFAULT 'pending',
    sent_at              TIMESTAMPTZ,
    replied_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
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

CREATE TABLE IF NOT EXISTS suppression_list (
    id              SERIAL PRIMARY KEY,
    identifier      TEXT NOT NULL,
    identifier_type TEXT NOT NULL DEFAULT 'linkedin',
    reason          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
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
    id              SERIAL PRIMARY KEY,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    score           REAL NOT NULL,
    queued_for      DATE NOT NULL,
    consumed        BOOLEAN NOT NULL DEFAULT FALSE,
    score_breakdown TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(student_id, job_id, queued_for)
);

CREATE TABLE IF NOT EXISTS leads (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    title            TEXT,
    company          TEXT,
    university       TEXT,
    linkedin_url     TEXT UNIQUE NOT NULL,
    snippet          TEXT,
    location_city    TEXT,
    location_country TEXT,
    tenure_months    INTEGER DEFAULT 0,
    is_alumni        BOOLEAN DEFAULT FALSE,
    dept_tag         TEXT,
    lead_type        TEXT DEFAULT 'relevant',
    job_opening_date TEXT,
    fetched_at       TIMESTAMPTZ DEFAULT NOW(),
    stale_after      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS company_email_formats (
    company    TEXT PRIMARY KEY,
    fmt_code   TEXT NOT NULL,
    domain     TEXT NOT NULL,
    source     TEXT DEFAULT 'groq',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_magic_tokens_token   ON magic_tokens(token);
CREATE INDEX IF NOT EXISTS idx_magic_tokens_email   ON magic_tokens(email);
CREATE INDEX IF NOT EXISTS idx_matches_student      ON matches(student_id);
CREATE INDEX IF NOT EXISTS idx_signals_match        ON signals(match_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token   ON refresh_tokens(token);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_student ON refresh_tokens(student_id);
CREATE INDEX IF NOT EXISTS idx_card_queue_student_date ON card_queue(student_id, queued_for);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads (lower(company));
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
    university           TEXT,
    created_at           TEXT DEFAULT (datetime('now')),
    last_seen            TEXT,
    deactivated_at       TEXT,
    notify_matches       INTEGER NOT NULL DEFAULT 0,
    notify_frequency     TEXT    NOT NULL DEFAULT 'daily',
    referral_code        TEXT UNIQUE,
    referred_by          TEXT,
    daily_cards_override INTEGER DEFAULT NULL,
    region               TEXT NOT NULL DEFAULT 'UK'
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
    opening_date TEXT,
    closing_date TEXT,
    source       TEXT,
    raw          TEXT,
    role_type    TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matches (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id           INTEGER REFERENCES students(id),
    job_id               INTEGER REFERENCES jobs(id),
    match_date           TEXT,
    person_name          TEXT,
    person_title         TEXT,
    person_company       TEXT,
    person_linkedin_url  TEXT,
    person_university    TEXT,
    person_tenure_months INTEGER,
    is_alumni            INTEGER DEFAULT 0,
    relevance_score      REAL,
    score_breakdown      TEXT,
    expected_email       TEXT,
    email_confidence     REAL,
    email_subject        TEXT,
    email_body           TEXT,
    status               TEXT DEFAULT 'pending',
    sent_at              TEXT,
    replied_at           TEXT,
    created_at           TEXT DEFAULT (datetime('now')),
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

CREATE TABLE IF NOT EXISTS suppression_list (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier      TEXT NOT NULL,
    identifier_type TEXT NOT NULL DEFAULT 'linkedin',
    reason          TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    score           REAL NOT NULL,
    queued_for      TEXT NOT NULL,
    consumed        INTEGER NOT NULL DEFAULT 0,
    score_breakdown TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(student_id, job_id, queued_for)
);

CREATE TABLE IF NOT EXISTS leads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    title            TEXT,
    company          TEXT,
    university       TEXT,
    linkedin_url     TEXT UNIQUE NOT NULL,
    snippet          TEXT,
    location_city    TEXT,
    location_country TEXT,
    tenure_months    INTEGER DEFAULT 0,
    is_alumni        INTEGER DEFAULT 0,
    dept_tag         TEXT,
    lead_type        TEXT DEFAULT 'relevant',
    job_opening_date TEXT,
    fetched_at       TEXT DEFAULT (datetime('now')),
    stale_after      TEXT
);

CREATE TABLE IF NOT EXISTS company_email_formats (
    company    TEXT PRIMARY KEY,
    fmt_code   TEXT NOT NULL,
    domain     TEXT NOT NULL,
    source     TEXT DEFAULT 'groq',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_magic_tokens_token ON magic_tokens(token);
CREATE INDEX IF NOT EXISTS idx_magic_tokens_email ON magic_tokens(email);
CREATE INDEX IF NOT EXISTS idx_matches_student    ON matches(student_id);
CREATE INDEX IF NOT EXISTS idx_signals_match      ON signals(match_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token   ON refresh_tokens(token);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_student ON refresh_tokens(student_id);
CREATE INDEX IF NOT EXISTS idx_card_queue_student_date ON card_queue(student_id, queued_for);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads (lower(company));
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
    _run_migrations()


def _run_migrations():
    """
    Idempotent migrations for existing databases.
    Renames old contact_* columns → person_* and adds any missing columns
    to the matches and card_queue tables.
    """
    if USE_POSTGRES:
        _run_migrations_postgres()
    else:
        _run_migrations_sqlite()


def _run_migrations_postgres():
    """Postgres migrations — uses ADD COLUMN IF NOT EXISTS and column rename."""
    migrations = [
        # Rename contact_name -> person_name if old column still exists
        """DO $$ BEGIN
             IF EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='matches' AND column_name='contact_name')
             THEN ALTER TABLE matches RENAME COLUMN contact_name TO person_name; END IF;
           END $$""",
        # Drop contact_email if it exists (pipeline does not use it)
        """DO $$ BEGIN
             IF EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='matches' AND column_name='contact_email')
             THEN ALTER TABLE matches DROP COLUMN contact_email; END IF;
           END $$""",
        # Rename contact_linkedin -> person_linkedin_url if old column still exists
        """DO $$ BEGIN
             IF EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='matches' AND column_name='contact_linkedin')
             THEN ALTER TABLE matches RENAME COLUMN contact_linkedin TO person_linkedin_url; END IF;
           END $$""",
        # Add missing matches columns
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS person_title TEXT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS person_company TEXT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS person_university TEXT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS person_tenure_months INTEGER",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS relevance_score REAL",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS score_breakdown TEXT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS expected_email TEXT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS email_confidence REAL",
        # Ensure person_name and person_linkedin_url exist (new installs already have them)
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS person_name TEXT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS person_linkedin_url TEXT",
        # Add score_breakdown to card_queue
        "ALTER TABLE card_queue ADD COLUMN IF NOT EXISTS score_breakdown TEXT",
        # Add opening_date and closing_date to jobs
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS opening_date TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS closing_date TEXT",
        # Back-fill dates from raw JSON for existing rows
        """UPDATE jobs SET
             opening_date = COALESCE(raw::json->>'opening_date', ''),
             closing_date  = COALESCE(raw::json->>'closing_date', '')
           WHERE opening_date IS NULL AND raw IS NOT NULL AND raw != '' AND raw != '{}'""",
        # Create leads table if missing (added after initial deploy)
        """CREATE TABLE IF NOT EXISTS leads (
            id               SERIAL PRIMARY KEY,
            name             TEXT NOT NULL,
            title            TEXT,
            company          TEXT,
            university       TEXT,
            linkedin_url     TEXT UNIQUE NOT NULL,
            snippet          TEXT,
            location_city    TEXT,
            location_country TEXT,
            tenure_months    INTEGER DEFAULT 0,
            is_alumni        BOOLEAN DEFAULT FALSE,
            dept_tag         TEXT,
            fetched_at       TIMESTAMPTZ DEFAULT NOW(),
            stale_after      TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_leads_company ON leads (lower(company))",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS job_opening_date TEXT",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS scraped_rank INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS job_title TEXT",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS job_expected_email TEXT",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_type TEXT DEFAULT 'relevant'",
        """CREATE TABLE IF NOT EXISTS company_email_formats (
            company    TEXT PRIMARY KEY,
            fmt_code   TEXT NOT NULL,
            domain     TEXT NOT NULL,
            source     TEXT DEFAULT 'groq',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""",
        # Feature: email reminders
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS notify_matches BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS notify_frequency TEXT NOT NULL DEFAULT 'daily'",
        # Feature: referral bonus
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS referral_code TEXT UNIQUE",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS referred_by TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS daily_cards_override INTEGER DEFAULT NULL",
        # Feature: Outlook OAuth (one-click email send)
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS outlook_access_token TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS outlook_refresh_token TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS outlook_token_expiry INTEGER",
        # magic_tokens: add purpose column if missing
        "ALTER TABLE magic_tokens ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'login'",
        # Feature: daily match snapshot (overwritten each day)
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match1_job_url TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match1_name_title TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match1_linkedin TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match2_job_url TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match2_name_title TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match2_linkedin TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match3_job_url TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match3_name_title TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS match3_linkedin TEXT",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS matches_updated_date DATE",
        # Referral code: backfill for existing students
        """UPDATE students SET referral_code = substring(md5(random()::text || id::text), 1, 8)
           WHERE referral_code IS NULL""",
        # Feature: notify deduplication — track last email send date per student
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS notify_sent_date DATE",
        # Feature: role type screening (internship_grad vs entry_level)
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS role_type TEXT",
        # Backfill role_type from raw JSON for existing rows
        """UPDATE jobs SET role_type = COALESCE(raw::json->>'role_type', 'entry_level')
           WHERE role_type IS NULL AND raw IS NOT NULL AND raw != '' AND raw != '{}'""",
        "UPDATE jobs SET role_type = 'entry_level' WHERE role_type IS NULL",
    ]
    with get_conn() as conn:
        cur = conn.cursor()
        for stmt in migrations:
            try:
                cur.execute(stmt)
            except Exception as e:
                print(f"[db] Migration warning (postgres): {e}")


def _run_migrations_sqlite():
    """
    SQLite migrations — SQLite does not support RENAME COLUMN before 3.25.0
    or ADD COLUMN IF NOT EXISTS, so we use PRAGMA table_info + try/except.
    """
    with get_conn() as conn:
        cur = conn.cursor()

        # Inspect existing matches columns
        cur.execute("PRAGMA table_info(matches)")
        existing_matches_cols = {row[1] for row in cur.fetchall()}

        # Rename contact_name -> person_name (SQLite 3.25+ supports RENAME COLUMN)
        if "contact_name" in existing_matches_cols and "person_name" not in existing_matches_cols:
            try:
                cur.execute("ALTER TABLE matches RENAME COLUMN contact_name TO person_name")
                existing_matches_cols.add("person_name")
                existing_matches_cols.discard("contact_name")
            except Exception as e:
                print(f"[db] Migration warning (rename contact_name): {e}")

        # Rename contact_linkedin -> person_linkedin_url
        if "contact_linkedin" in existing_matches_cols and "person_linkedin_url" not in existing_matches_cols:
            try:
                cur.execute("ALTER TABLE matches RENAME COLUMN contact_linkedin TO person_linkedin_url")
                existing_matches_cols.add("person_linkedin_url")
                existing_matches_cols.discard("contact_linkedin")
            except Exception as e:
                print(f"[db] Migration warning (rename contact_linkedin): {e}")

        # Drop contact_email — SQLite <3.35 doesn't support DROP COLUMN; leave it in place
        # (extra column is harmless; pipeline does not INSERT into it)

        # Add missing matches columns
        matches_new_cols = [
            ("person_name",          "TEXT"),
            ("person_title",         "TEXT"),
            ("person_company",       "TEXT"),
            ("person_linkedin_url",  "TEXT"),
            ("person_university",    "TEXT"),
            ("person_tenure_months", "INTEGER"),
            ("relevance_score",      "REAL"),
            ("score_breakdown",      "TEXT"),
            ("expected_email",       "TEXT"),
            ("email_confidence",     "REAL"),
        ]
        for col, col_type in matches_new_cols:
            if col not in existing_matches_cols:
                try:
                    cur.execute(f"ALTER TABLE matches ADD COLUMN {col} {col_type}")
                except Exception as e:
                    print(f"[db] Migration warning (matches.{col}): {e}")

        # Add score_breakdown to card_queue
        cur.execute("PRAGMA table_info(card_queue)")
        existing_cq_cols = {row[1] for row in cur.fetchall()}
        if "score_breakdown" not in existing_cq_cols:
            try:
                cur.execute("ALTER TABLE card_queue ADD COLUMN score_breakdown TEXT")
            except Exception as e:
                print(f"[db] Migration warning (card_queue.score_breakdown): {e}")

        # Feature: lead_type tier tagging
        cur.execute("PRAGMA table_info(leads)")
        existing_leads_cols = {row[1] for row in cur.fetchall()}
        if "lead_type" not in existing_leads_cols:
            try:
                cur.execute("ALTER TABLE leads ADD COLUMN lead_type TEXT DEFAULT 'relevant'")
            except Exception as e:
                print(f"[db] Migration warning (leads.lead_type): {e}")

        # Feature: role type screening (internship_grad vs entry_level)
        cur.execute("PRAGMA table_info(jobs)")
        existing_jobs_cols = {row[1] for row in cur.fetchall()}
        if "role_type" not in existing_jobs_cols:
            try:
                cur.execute("ALTER TABLE jobs ADD COLUMN role_type TEXT")
                cur.execute("UPDATE jobs SET role_type = 'entry_level' WHERE role_type IS NULL")
            except Exception as e:
                print(f"[db] Migration warning (jobs.role_type): {e}")

        # Feature: Outlook OAuth + daily match snapshot
        cur.execute("PRAGMA table_info(students)")
        existing_student_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in [
            ("outlook_access_token",  "TEXT"),
            ("outlook_refresh_token", "TEXT"),
            ("outlook_token_expiry",  "INTEGER"),
            ("match1_name_title",     "TEXT"),
            ("match1_linkedin",       "TEXT"),
            ("match2_name_title",     "TEXT"),
            ("match2_linkedin",       "TEXT"),
            ("match3_name_title",     "TEXT"),
            ("match3_linkedin",       "TEXT"),
            ("matches_updated_date",  "TEXT"),
            ("notify_sent_date",      "TEXT"),
        ]:
            if col not in existing_student_cols:
                try:
                    cur.execute(f"ALTER TABLE students ADD COLUMN {col} {col_type}")
                except Exception as e:
                    print(f"[db] Migration warning (students.{col}): {e}")


# ── Convenience wrappers used by api/server.py ──────────────────────────────

def get_student_by_email(email):
    return fetchone("SELECT * FROM students WHERE email = ?", (email,))


def get_student_by_id(student_id):
    return fetchone("SELECT * FROM students WHERE id = ?", (student_id,))


def create_student(email, university=None):
    import secrets as _secrets, string as _string
    referral_code = ''.join(_secrets.choice(_string.ascii_uppercase + _string.digits) for _ in range(8))
    execute(
        "INSERT INTO students (email, university, referral_code) VALUES (?, ?, ?) ON CONFLICT (email) DO NOTHING",
        (email, university, referral_code)
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
    """Hard-delete the student and all related data so the email can be reused."""
    for tbl in ("matches", "refresh_tokens", "signals", "card_queue", "magic_tokens"):
        try:
            execute(f"DELETE FROM {tbl} WHERE student_id = ?", (student_id,))
        except Exception:
            pass
    # magic_tokens uses email not student_id — clean up via sub-select
    try:
        execute(
            "DELETE FROM magic_tokens WHERE email = (SELECT email FROM students WHERE id = ?)",
            (student_id,)
        )
    except Exception:
        pass
    execute("DELETE FROM students WHERE id = ?", (student_id,))


# ── Pipeline helpers ─────────────────────────────────────────────────────────

# Alias used by pipeline/daily_cards.py and cli.py
db_conn = get_conn


def _exec(conn, sql, params=()):
    """
    Execute SQL on a connection, returning a cursor.
    psycopg2 connections have no .execute() — must use conn.cursor().
    SQLite connections do have .execute() but we normalise here for consistency.
    """
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return cur
    else:
        return conn.execute(sql, params or ())


def get_active_jobs(conn, industries=None, region=None, seniority=None,
                    days_fresh=14, limit=100):
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
            # Filter on opening_date (when job was posted); fall back to created_at for jobs
            # where opening_date is unknown.
            clauses.append(
                f"(opening_date IS NULL OR opening_date > to_char(NOW() - INTERVAL '{days_fresh} days', 'YYYY-MM-DD'))"
            )
            clauses.append(
                f"(created_at IS NULL OR created_at > NOW() - INTERVAL '{days_fresh} days')"
            )
            # Exclude jobs whose closing date has already passed
            clauses.append(
                "(closing_date IS NULL OR closing_date = '' OR closing_date >= to_char(NOW(), 'YYYY-MM-DD'))"
            )
        else:
            clauses.append(
                f"(opening_date IS NULL OR opening_date > date('now', '-{days_fresh} days'))"
            )
            clauses.append(
                f"(created_at IS NULL OR created_at > datetime('now', '-{days_fresh} days'))"
            )
            clauses.append(
                "(closing_date IS NULL OR closing_date = '' OR closing_date >= date('now'))"
            )

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

    # Always exclude roles too senior for entry-level targeting
    senior_filter = (
        r"title !~* '\y(Senior|Director|President|Vice\s+President|VP|"
        r"Head\s+of|Managing\s+Director|Chief|Partner|Principal|Manager|Lead)\y'"
        if USE_POSTGRES else
        "title NOT LIKE '%Senior%' AND title NOT LIKE '%Director%' "
        "AND title NOT LIKE '%President%' AND title NOT LIKE '%Managing%' "
        "AND title NOT LIKE '%Principal%' AND title NOT LIKE '% Lead%' "
        "AND title NOT LIKE 'Lead %'"
    )
    clauses.append(senior_filter)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql   = f"""
        SELECT id, title, company AS company_name, url, location, industry,
               company_size, opening_date, created_at AS posted_date, source, raw, role_type
        FROM   jobs
        {where}
        ORDER  BY opening_date DESC NULLS LAST, created_at DESC
        LIMIT  {ph}
    """
    params.append(limit)

    if USE_POSTGRES:
        sql = sql.replace("?", "%s")

    rows = _exec(conn, sql, params).fetchall()
    return [dict(r) for r in rows]


def upsert_job(conn, job: dict) -> tuple:
    """
    Insert or update a job. Returns (job_id, is_new).
    job dict keys: company_name, title, url, industries, seniority, region,
                   source_id, source_name, posted_at (optional), company_size (optional).
    """
    import json as _json
    ph = "%s" if USE_POSTGRES else "?"

    industry = ""
    inds = job.get("industries") or []
    if isinstance(inds, list) and inds:
        industry = inds[0]
    elif isinstance(inds, str):
        industry = inds

    company      = (job.get("company_name") or "").strip()
    title        = (job.get("title") or "").strip()
    url          = (job.get("url") or "").strip()
    location     = (job.get("region") or "").strip()
    posted_at    = job.get("posted_at") or job.get("posted_date") or None
    opening_date = (job.get("opening_date") or "").strip() or None
    closing_date = (job.get("closing_date") or "").strip() or None
    source       = job.get("source_id") or job.get("source_name") or ""
    company_sz   = job.get("company_size") or ""
    role_type    = job.get("role_type") or "entry_level"
    raw          = _json.dumps(job)

    # Check if already exists by url (primary) or company+title
    existing = None
    if url:
        existing = _exec(conn, "SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    if not existing:
        existing = _exec(
            conn,
            "SELECT id FROM jobs WHERE company = ? AND title = ?",
            (company, title)
        ).fetchone()

    if existing:
        job_id = existing[0] if not isinstance(existing, dict) else existing["id"]
        now_expr = "NOW()" if USE_POSTGRES else "datetime('now')"
        _exec(
            conn,
            f"UPDATE jobs SET title=?, company=?, url=?, location=?, "
            f"industry=?, company_size=?, opening_date=?, closing_date=?, source=?, raw=?, role_type=?, created_at={now_expr} "
            f"WHERE id=?",
            (title, company, url, location, industry, company_sz,
             opening_date, closing_date, source, raw, role_type, job_id)
        )
        return job_id, False
    else:
        if USE_POSTGRES:
            cur = _exec(
                conn,
                "INSERT INTO jobs (title, company, url, location, industry, company_size, "
                "opening_date, closing_date, source, raw, role_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (title, company, url, location, industry, company_sz,
                 opening_date, closing_date, source, raw, role_type)
            )
            row = cur.fetchone()
            job_id = row["id"] if isinstance(row, dict) else row[0]
        else:
            cur = _exec(
                conn,
                "INSERT INTO jobs (title, company, url, location, industry, company_size, "
                "opening_date, closing_date, source, raw, role_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (title, company, url, location, industry, company_sz,
                 opening_date, closing_date, source, raw, role_type)
            )
            job_id = cur.lastrowid
        return job_id, True


def log_scrape_run(conn, source_id: str, source_name: str, status: str,
                   jobs_found: int, jobs_new: int, error_msg: str = "", duration: float = 0.0):
    """Log a completed scrape run. Creates table on first use."""
    ph = "%s" if USE_POSTGRES else "?"
    try:
        id_type  = "SERIAL" if USE_POSTGRES else "INTEGER"
        ts_type  = "TIMESTAMPTZ DEFAULT NOW()" if USE_POSTGRES else "TEXT DEFAULT (datetime('now'))"
        _exec(conn,
            f"CREATE TABLE IF NOT EXISTS scrape_runs ("
            f"id {id_type} PRIMARY KEY, "
            f"source_id TEXT, source_name TEXT, status TEXT, "
            f"jobs_found INTEGER, jobs_new INTEGER, error_msg TEXT, "
            f"duration_s REAL, finished_at {ts_type}"
            f")"
        )
        _exec(conn,
            "INSERT INTO scrape_runs (source_id, source_name, status, jobs_found, jobs_new, error_msg, duration_s) "
            "VALUES (?,?,?,?,?,?,?)",
            (source_id, source_name, status, jobs_found, jobs_new, error_msg, round(duration, 2))
        )
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).warning(f"log_scrape_run failed: {e}")


def db_stats(conn) -> dict:
    """Return a dict of high-level DB stats for the admin panel."""
    def _count(sql):
        try:
            row = _exec(conn, sql).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    from datetime import date as _date
    today = _date.today().isoformat()
    ph = "%s" if USE_POSTGRES else "?"

    return {
        "total_jobs":    _count("SELECT COUNT(*) FROM jobs"),
        "active_jobs":   _count("SELECT COUNT(*) FROM jobs WHERE created_at > " +
                                 ("NOW() - INTERVAL '14 days'" if USE_POSTGRES
                                  else "datetime('now', '-14 days')")),
        "companies":     _count("SELECT COUNT(DISTINCT company) FROM jobs"),
        "students":      _count("SELECT COUNT(*) FROM students"),
        "matches_today": _count(f"SELECT COUNT(*) FROM matches WHERE match_date = '{today}'"),
        "emails_week":   _count("SELECT COUNT(*) FROM matches WHERE status = 'sent' AND " +
                                 ("sent_at > NOW() - INTERVAL '7 days'" if USE_POSTGRES
                                  else "sent_at > datetime('now', '-7 days')")),
    }


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


def enqueue_card(student_id, job_id, score, queued_for, score_breakdown=None):
    """Insert a card into the queue; ignore if already queued for that date."""
    if USE_POSTGRES:
        execute(
            """INSERT INTO card_queue (student_id, job_id, score, queued_for, score_breakdown)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (student_id, job_id, queued_for) DO NOTHING""",
            (student_id, job_id, score, queued_for, score_breakdown),
        )
    else:
        execute(
            """INSERT OR IGNORE INTO card_queue (student_id, job_id, score, queued_for, score_breakdown)
               VALUES (?, ?, ?, ?, ?)""",
            (student_id, job_id, score, queued_for, score_breakdown),
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


# ── Leads pool ───────────────────────────────────────────────────────────────

def upsert_lead(lead: dict) -> None:
    """Insert or update a pre-fetched lead in the leads table."""
    from datetime import datetime, timedelta
    stale_after = (datetime.utcnow() + timedelta(days=30)).isoformat()
    if USE_POSTGRES:
        execute(
            """INSERT INTO leads
               (job_title, name, title, company, university, linkedin_url, snippet,
                location_city, location_country, tenure_months, is_alumni,
                dept_tag, lead_type, scraped_rank, job_expected_email, job_opening_date, fetched_at, stale_after)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NOW(),?)
               ON CONFLICT (linkedin_url) DO UPDATE SET
                 job_title=EXCLUDED.job_title,
                 name=EXCLUDED.name, title=EXCLUDED.title, company=EXCLUDED.company,
                 university=EXCLUDED.university, snippet=EXCLUDED.snippet,
                 location_city=EXCLUDED.location_city,
                 location_country=EXCLUDED.location_country,
                 tenure_months=EXCLUDED.tenure_months, is_alumni=EXCLUDED.is_alumni,
                 dept_tag=EXCLUDED.dept_tag, lead_type=EXCLUDED.lead_type,
                 scraped_rank=EXCLUDED.scraped_rank,
                 job_expected_email=EXCLUDED.job_expected_email,
                 job_opening_date=EXCLUDED.job_opening_date,
                 fetched_at=NOW(), stale_after=EXCLUDED.stale_after""",
            (
                lead.get("job_title", ""),
                lead.get("name", ""), lead.get("title", ""), lead.get("company", ""),
                lead.get("university", ""), lead["linkedin_url"], lead.get("snippet", ""),
                lead.get("location_city", ""), lead.get("location_country", ""),
                lead.get("tenure_months", 0), bool(lead.get("is_alumni", False)),
                lead.get("dept_tag", ""), lead.get("lead_type", "relevant"),
                lead.get("scraped_rank", 0),
                lead.get("job_expected_email", ""), lead.get("job_opening_date", ""), stale_after,
            ),
        )
    else:
        execute(
            """INSERT INTO leads
               (job_title, name, title, company, university, linkedin_url, snippet,
                location_city, location_country, tenure_months, is_alumni,
                dept_tag, lead_type, scraped_rank, job_expected_email, job_opening_date, fetched_at, stale_after)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?)
               ON CONFLICT (linkedin_url) DO UPDATE SET
                 job_title=excluded.job_title,
                 name=excluded.name, title=excluded.title, company=excluded.company,
                 university=excluded.university, snippet=excluded.snippet,
                 location_city=excluded.location_city,
                 location_country=excluded.location_country,
                 tenure_months=excluded.tenure_months, is_alumni=excluded.is_alumni,
                 dept_tag=excluded.dept_tag, lead_type=excluded.lead_type,
                 scraped_rank=excluded.scraped_rank,
                 job_expected_email=excluded.job_expected_email,
                 job_opening_date=excluded.job_opening_date,
                 fetched_at=datetime('now'), stale_after=excluded.stale_after""",
            (
                lead.get("job_title", ""),
                lead.get("name", ""), lead.get("title", ""), lead.get("company", ""),
                lead.get("university", ""), lead["linkedin_url"], lead.get("snippet", ""),
                lead.get("location_city", ""), lead.get("location_country", ""),
                lead.get("tenure_months", 0), 1 if lead.get("is_alumni") else 0,
                lead.get("dept_tag", ""), lead.get("lead_type", "relevant"),
                lead.get("scraped_rank", 0),
                lead.get("job_expected_email", ""), lead.get("job_opening_date", ""), stale_after,
            ),
        )


def get_leads_for_company(company: str) -> list[dict]:
    """Return all non-stale leads for a company (case-insensitive match)."""
    if USE_POSTGRES:
        return fetchall(
            """SELECT * FROM leads
               WHERE lower(company) = lower(?)
               AND (stale_after IS NULL OR stale_after > NOW())
               ORDER BY is_alumni DESC, tenure_months DESC""",
            (company,),
        )
    else:
        return fetchall(
            """SELECT * FROM leads
               WHERE lower(company) = lower(?)
               AND (stale_after IS NULL OR stale_after > datetime('now'))
               ORDER BY is_alumni DESC, tenure_months DESC""",
            (company,),
        )


def get_email_format(company: str) -> tuple[str, str] | None:
    """Return (fmt_code, domain) for a company from the persistent cache, or None."""
    row = fetchone(
        "SELECT fmt_code, domain FROM company_email_formats WHERE lower(company) = lower(?)",
        (company.strip(),),
    )
    if row:
        return row["fmt_code"], row["domain"]
    return None


def save_email_format(company: str, fmt_code: str, domain: str, source: str = "groq") -> None:
    """Persist a company email format to the DB (upsert)."""
    if USE_POSTGRES:
        execute(
            """INSERT INTO company_email_formats (company, fmt_code, domain, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (company) DO UPDATE SET
                 fmt_code=EXCLUDED.fmt_code, domain=EXCLUDED.domain, source=EXCLUDED.source""",
            (company.strip().lower(), fmt_code, domain, source),
        )
    else:
        execute(
            """INSERT OR REPLACE INTO company_email_formats (company, fmt_code, domain, source)
               VALUES (?, ?, ?, ?)""",
            (company.strip().lower(), fmt_code, domain, source),
        )


def get_student_by_referral_code(code: str) -> dict | None:
    """Return the student row whose referral_code matches, or None."""
    return fetchone("SELECT * FROM students WHERE referral_code = ?", (code.strip().upper(),))


def get_seen_linkedin_urls(student_id: int) -> set:
    """Return all LinkedIn URLs this student has ever been shown (all-time)."""
    rows = fetchall(
        "SELECT person_linkedin_url FROM matches WHERE student_id = ? AND person_linkedin_url IS NOT NULL",
        (student_id,),
    )
    return {r["person_linkedin_url"] for r in rows}


def get_recently_matched_linkedin_urls(days: int = 3) -> set:
    """Return all LinkedIn URLs matched to ANY student in the last N days.
    Prevents the same person being surfaced to multiple students within a short window."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    ph = "%s" if USE_POSTGRES else "?"
    rows = fetchall(
        f"SELECT DISTINCT person_linkedin_url FROM matches "
        f"WHERE match_date >= {ph} AND person_linkedin_url IS NOT NULL",
        (cutoff,),
    )
    return {r["person_linkedin_url"] for r in rows}


def get_leads_stats() -> dict:
    """Return aggregate stats about the leads pool for admin panel."""
    total     = fetchone("SELECT COUNT(*) AS cnt FROM leads") or {}
    with_city = fetchone("SELECT COUNT(*) AS cnt FROM leads WHERE location_city IS NOT NULL AND location_city != ''") or {}
    with_ten  = fetchone("SELECT COUNT(*) AS cnt FROM leads WHERE tenure_months > 0") or {}
    alumni_sql = "SELECT COUNT(*) AS cnt FROM leads WHERE is_alumni = TRUE" if USE_POSTGRES else "SELECT COUNT(*) AS cnt FROM leads WHERE is_alumni = 1"
    alumni    = fetchone(alumni_sql) or {}
    companies = fetchone("SELECT COUNT(DISTINCT lower(company)) AS cnt FROM leads") or {}
    return {
        "total_leads":          int(total.get("cnt", 0)),
        "with_city":            int(with_city.get("cnt", 0)),
        "with_tenure":          int(with_ten.get("cnt", 0)),
        "alumni_leads":         int(alumni.get("cnt", 0)),
        "distinct_companies":   int(companies.get("cnt", 0)),
    }
