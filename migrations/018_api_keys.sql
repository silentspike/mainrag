-- Migration 018: API Keys for Agent Authentication
-- Sprint 1.3: Dual-Auth (API-Key for Agents, JWT for Admin)

-- API Keys table for per-agent authentication
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash BYTEA NOT NULL UNIQUE,
    key_prefix VARCHAR(8) NOT NULL,
    agent_name VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'rotating', 'revoked', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    rotated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

-- Index for fast key lookup during auth (hot path)
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash)
    WHERE status IN ('active', 'rotating');

-- Index for agent management
CREATE INDEX IF NOT EXISTS idx_api_keys_agent ON api_keys (agent_id);

-- Index for expired key cleanup
CREATE INDEX IF NOT EXISTS idx_api_keys_expires ON api_keys (expires_at)
    WHERE status = 'rotating';

COMMENT ON TABLE api_keys IS 'Per-agent API keys for authentication. Keys are HMAC-SHA256 hashed with server pepper.';
COMMENT ON COLUMN api_keys.key_hash IS 'HMAC-SHA256(API_KEY_PEPPER, raw_key) stored as raw 32 bytes';
COMMENT ON COLUMN api_keys.key_prefix IS 'First 8 chars of Base64-encoded key for audit logs';
COMMENT ON COLUMN api_keys.status IS 'active: valid, rotating: old key in grace period, revoked: immediately invalid, expired: grace period ended';
