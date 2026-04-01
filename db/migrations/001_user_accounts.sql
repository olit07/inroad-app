-- Migration 001: user accounts
-- Run this once against existing databases to add JWT auth support

ALTER TABLE students ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          SERIAL PRIMARY KEY,
    token       TEXT UNIQUE NOT NULL,
    student_id  INTEGER REFERENCES students(id),
    expires_at  TIMESTAMP NOT NULL,
    revoked_at  TIMESTAMP,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token ON refresh_tokens(token);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_student ON refresh_tokens(student_id);
