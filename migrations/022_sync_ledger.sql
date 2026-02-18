-- Sprint 8.4: Qdrant Consistency Tracking (sync_ledger)
-- Tracks synchronization state between PostgreSQL chunks and Qdrant vectors.
-- Periodic audit job (6h) detects drift.

CREATE TABLE IF NOT EXISTS sync_ledger (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    pg_chunk_count BIGINT NOT NULL DEFAULT 0,
    qdrant_point_count BIGINT NOT NULL DEFAULT 0,
    drift_count BIGINT NOT NULL DEFAULT 0,  -- abs(pg - qdrant)
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status VARCHAR(16) NOT NULL DEFAULT 'ok',  -- ok, drift, error
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_ledger_source ON sync_ledger (source_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_ledger_drift ON sync_ledger (status) WHERE status != 'ok';
