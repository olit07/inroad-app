"""
inroad — Reply Tracker & Outcome Measurement
"""
import re, json, hashlib, logging
from datetime import date, datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH
from db.database import db_conn

logger = logging.getLogger(__name__)

POSITIVE_SIGNALS  = ["happy to","sure","love to","free","available","yes ","sounds good","would be great","absolutely","of course","glad to","looking forward"]
DECLINE_SIGNALS   = ["not able","too busy","no capacity","not the right","unfortunately","pass on","can't","cannot","not in a position","don't do","no longer"]
CONDITIONAL_SIGNALS = ["after","next month","once i","when i'm back","touch base later","in a few weeks","try again","reach out in"]


def parse_reply_sentiment(reply_text: str) -> dict:
    text = reply_text.lower().strip()
    is_positive    = any(s in text for s in POSITIVE_SIGNALS)
    is_decline     = any(s in text for s in DECLINE_SIGNALS)
    is_conditional = any(s in text for s in CONDITIONAL_SIGNALS)

    if is_decline:
        score = -0.7
        action = "mark_closed"
    elif is_positive:
        score = 0.8
        action = "book_now"
    elif is_conditional:
        score = 0.2
        action = "follow_up_2w"
    else:
        score = 0.0
        action = "unknown"

    return {
        "is_positive":    is_positive,
        "is_decline":     is_decline,
        "is_conditional": is_conditional,
        "sentiment_score": score,
        "suggested_followup": action,
    }


def update_match_from_reply(match_id: int, reply_text: str, db_path=DB_PATH) -> dict:
    sentiment = parse_reply_sentiment(reply_text)
    reply_received = 1
    chat_booked    = 1 if sentiment["is_positive"] and "book" in sentiment["suggested_followup"] else 0

    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE matches SET reply_received=?, chat_booked=? WHERE id=?",
            (reply_received, chat_booked, match_id)
        )
        row = conn.execute("SELECT student_id, job_id FROM matches WHERE id=?", (match_id,)).fetchone()
        if row:
            conn.execute(
                "INSERT INTO student_signals (student_id, job_id, signal_type) VALUES (?,?,?)",
                (row["student_id"], row["job_id"], "reply")
            )
            if chat_booked:
                conn.execute(
                    "INSERT INTO student_signals (student_id, job_id, signal_type) VALUES (?,?,?)",
                    (row["student_id"], row["job_id"], "booked")
                )
    return sentiment


def weekly_digest(student_id: int, db_path=DB_PATH) -> dict:
    week_start = (date.today() - timedelta(days=7)).isoformat()

    with db_conn(db_path) as conn:
        sent = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND email_sent_at >= ?",
            (student_id, week_start)
        ).fetchone()[0]

        replies = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND reply_received=1 AND email_sent_at >= ?",
            (student_id, week_start)
        ).fetchone()[0]

        booked = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND chat_booked=1",
            (student_id,)
        ).fetchone()[0]

        all_sent = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND email_sent_at IS NOT NULL",
            (student_id,)
        ).fetchone()[0]

        all_replies = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND reply_received=1",
            (student_id,)
        ).fetchone()[0]

        # Industry breakdown
        ind_rows = conn.execute(
            """SELECT j.industries, COUNT(*) as n
               FROM matches m JOIN jobs j ON m.job_id=j.id
               WHERE m.student_id=? AND m.reply_received=1
               GROUP BY j.industries ORDER BY n DESC LIMIT 3""",
            (student_id,)
        ).fetchall()

        top_industries = []
        for row in ind_rows:
            try:
                inds = json.loads(row["industries"] or "[]")
                top_industries.extend(inds[:1])
            except Exception:
                pass

        # Alumni vs non-alumni rates
        alumni_sent = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND is_alumni=1 AND email_sent_at IS NOT NULL",
            (student_id,)
        ).fetchone()[0]
        alumni_replied = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE student_id=? AND is_alumni=1 AND reply_received=1",
            (student_id,)
        ).fetchone()[0]

        # Streak — consecutive days with sent email
        sent_dates = [
            r[0][:10] for r in conn.execute(
                "SELECT email_sent_at FROM matches WHERE student_id=? AND email_sent_at IS NOT NULL ORDER BY email_sent_at DESC",
                (student_id,)
            ).fetchall()
        ]
        streak = 0
        check_date = date.today()
        for d_str in sorted(set(sent_dates), reverse=True):
            if d_str == check_date.isoformat() or d_str == (check_date - timedelta(days=1)).isoformat():
                streak += 1
                check_date -= timedelta(days=1)
            else:
                break

    return {
        "week_start":                week_start,
        "emails_sent":               sent,
        "replies_received":          replies,
        "chats_booked":              booked,
        "response_rate":             round(replies / sent, 2) if sent else 0.0,
        "booking_rate":              round(booked / all_sent, 2) if all_sent else 0.0,
        "top_responding_industries": top_industries,
        "alumni_response_rate":      round(alumni_replied / alumni_sent, 2) if alumni_sent else 0.0,
        "non_alumni_response_rate":  round((all_replies - alumni_replied) / max(all_sent - alumni_sent, 1), 2),
        "streak_days":               streak,
        "all_time_sent":             all_sent,
        "all_time_replies":          all_replies,
    }


def cohort_comparison(db_path=DB_PATH) -> dict:
    cohorts_data = {}
    with db_conn(db_path) as conn:
        students = conn.execute("SELECT id FROM students").fetchall()
        for s in students:
            sid = s[0]
            cohort = assign_cohort(sid)
            if cohort not in cohorts_data:
                cohorts_data[cohort] = {"n": 0, "sent": 0, "replies": 0, "booked": 0}
            cohorts_data[cohort]["n"] += 1
            row = conn.execute(
                """SELECT
                   COUNT(*) FILTER(WHERE email_sent_at IS NOT NULL) as sent,
                   COUNT(*) FILTER(WHERE reply_received=1) as replies,
                   COUNT(*) FILTER(WHERE chat_booked=1) as booked
                   FROM matches WHERE student_id=?""",
                (sid,)
            ).fetchone()
            if row:
                cohorts_data[cohort]["sent"]    += row["sent"] or 0
                cohorts_data[cohort]["replies"] += row["replies"] or 0
                cohorts_data[cohort]["booked"]  += row["booked"] or 0

    cohorts = []
    best_rate = -1
    winning = "control"
    for cohort_id, d in cohorts_data.items():
        rate = round(d["replies"] / d["sent"], 3) if d["sent"] else 0.0
        cohorts.append({
            "cohort_id":        cohort_id,
            "n_students":       d["n"],
            "avg_send_rate":    round(d["sent"] / max(d["n"], 1), 1),
            "avg_reply_rate":   rate,
            "avg_booking_rate": round(d["booked"] / max(d["sent"], 1), 3),
        })
        if rate > best_rate:
            best_rate = rate
            winning = cohort_id

    total_students = sum(d["n"] for d in cohorts_data.values())
    confidence = "high" if total_students >= 50 else "medium" if total_students >= 15 else "low"

    return {"cohorts": cohorts, "winning_cohort": winning, "confidence": confidence}


def generate_followup_suggestion(match_id: int, db_path=DB_PATH) -> str:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        return "unknown"

    row = dict(row)
    if row.get("chat_booked"):
        return "book_confirmed"
    if row.get("reply_received"):
        return "wait_longer"
    sent_at = row.get("email_sent_at")
    if not sent_at:
        return "mark_closed"

    days_ago = (datetime.utcnow() - datetime.fromisoformat(sent_at)).days
    if days_ago < 5:
        return "wait_longer"
    if days_ago < 14:
        return "send_followup"
    return "mark_closed"


def assign_cohort(student_id: int) -> str:
    h = int(hashlib.md5(str(student_id).encode()).hexdigest(), 16)
    idx = h % 3
    return ["control", "variant_a", "variant_b"][idx]
