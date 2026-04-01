-- Migration 002: add match_date to matches
-- Run this once against existing databases that pre-date this column.

ALTER TABLE matches ADD COLUMN IF NOT EXISTS match_date DATE;
UPDATE matches SET match_date = DATE(created_at) WHERE match_date IS NULL;
