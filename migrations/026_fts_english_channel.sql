-- Migration 026: Add English-stemmed FTS channel
--
-- Problem: 'simple' FTS config does no stemming.
-- "searching" doesn't match "search", "running" doesn't match "run".
-- This hurts NL queries while 'simple' is correct for code identifiers.
--
-- Fix: Add second FTS vector with 'english' config for stemmed matching.
-- Search logic uses GREATEST(simple_score, english_score * 0.8) for NL queries.

SET statement_timeout = '0';

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS fts_vector_english TSVECTOR GENERATED ALWAYS AS (
    to_tsvector('english', COALESCE(content_text, ''))
) STORED;

CREATE INDEX IF NOT EXISTS idx_chunks_fts_en ON chunks USING GIN (fts_vector_english) WITH (fastupdate = off);
