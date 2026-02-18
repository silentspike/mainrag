-- Migration 019: Revoked Tokens for JWT Revocation
-- Sprint 2.8: Token Revocation (moka cache + DB persistence + startup-gate)

CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- Index for efficient startup warmup query and cleanup
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens (expires_at);

COMMENT ON TABLE revoked_tokens IS 'Revoked JWT token IDs (jti). Used for startup warmup into moka cache.';
COMMENT ON COLUMN revoked_tokens.expires_at IS 'JWT expiry time. Tokens past expiry are cleaned up periodically.';
