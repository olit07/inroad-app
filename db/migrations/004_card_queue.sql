-- Migration 004: pre-generated card queue
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
CREATE INDEX IF NOT EXISTS idx_card_queue_student_date ON card_queue(student_id, queued_for);
