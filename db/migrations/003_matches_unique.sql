-- Migration 003: add unique constraint on (student_id, job_id) in matches
-- Safe to run on existing data (removes duplicates first)

-- Remove any existing duplicates, keeping the earliest match
DELETE FROM matches
WHERE id NOT IN (
    SELECT MIN(id)
    FROM matches
    GROUP BY student_id, job_id
);

-- Add the unique constraint
-- PostgreSQL:
ALTER TABLE matches ADD CONSTRAINT uq_matches_student_job UNIQUE (student_id, job_id);
-- SQLite does not support ADD CONSTRAINT; the constraint is only enforced on new tables.
-- For SQLite, recreate the table (handled by running init_db() on a fresh DB).
