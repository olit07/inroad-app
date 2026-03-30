"""
CCC — LinkedIn Profile Cache

Caches resolved LinkedIn profile data for 7 days to avoid re-fetching.
Uses the main ccc.db database (separate profile_cache table).
"""
import json, logging
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH
from db.database import db_conn

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_cache (
    linkedin_url    TEXT PRIMARY KEY,
    name            TEXT,
    title           TEXT,
    company         TEXT,
    university      TEXT,
    tenure_months   INTEGER DEFAULT 0,
    is_verified     INTEGER DEFAULT 0,
    resolved_at     TEXT NOT NULL,
    hit_count       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pc_resolved ON profile_cache(resolved_at);
"""


def init_cache(db_path=DB_PATH):
    with db_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def get_cached(linkedin_url: str, db_path=DB_PATH) -> dict | None:
    """Return cached profile dict or None if missing/expired."""
    cutoff = (datetime.utcnow() - timedelta(days=CACHE_TTL_DAYS)).isoformat()
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM profile_cache WHERE linkedin_url=? AND resolved_at > ?",
            (linkedin_url, cutoff)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE profile_cache SET hit_count=hit_count+1 WHERE linkedin_url=?",
                (linkedin_url,)
            )
            return dict(row)
    return None


def put_cached(linkedin_url: str, data: dict, db_path=DB_PATH):
    """Insert or update a profile in the cache."""
    now = datetime.utcnow().isoformat()
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO profile_cache
               (linkedin_url, name, title, company, university, tenure_months, resolved_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(linkedin_url) DO UPDATE SET
               name=excluded.name, title=excluded.title, company=excluded.company,
               university=excluded.university, tenure_months=excluded.tenure_months,
               resolved_at=excluded.resolved_at""",
            (
                linkedin_url,
                data.get("name", ""),
                data.get("title", ""),
                data.get("company", ""),
                data.get("university", ""),
                data.get("tenure_months", 0),
                now,
            )
        )


def cache_stats(db_path=DB_PATH) -> dict:
    cutoff = (datetime.utcnow() - timedelta(days=CACHE_TTL_DAYS)).isoformat()
    with db_conn(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM profile_cache").fetchone()[0]
        fresh = conn.execute(
            "SELECT COUNT(*) FROM profile_cache WHERE resolved_at > ?", (cutoff,)
        ).fetchone()[0]
        hits  = conn.execute("SELECT SUM(hit_count) FROM profile_cache").fetchone()[0] or 0
    return {
        "total": total, "fresh": fresh, "stale": total - fresh,
        "hit_rate": round(hits / max(total, 1), 2)
    }


def prune_stale(days: int = 14, db_path=DB_PATH) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM profile_cache WHERE resolved_at < ?", (cutoff,)
        )
        return cur.rowcount
