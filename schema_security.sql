-- ===================================================================
-- MAINRAG Security & Auth Schema Extension
-- ===================================================================
-- Version: 1.0
-- Enterprise-grade security for multi-tenant RAG system
-- Reference: https://ragaboutit.com/the-ultimate-guide-to-rag-authorization/
-- Reference: https://www.daxa.ai/blogs/secure-retrieval-augmented-generation-rag-in-enterprise-environments
-- ===================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- For encryption
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- For UUIDs

-- ===================================================================
-- USERS: User accounts with secure password storage
-- ===================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT NOT NULL UNIQUE,
    email TEXT UNIQUE,
    password_hash TEXT NOT NULL,  -- bcrypt/argon2 hashed
    display_name TEXT,
    avatar_url TEXT,

    -- Account status
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,

    -- MFA
    mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    mfa_secret TEXT,  -- TOTP secret (encrypted)
    mfa_backup_codes TEXT[],  -- Encrypted backup codes

    -- Metadata
    last_login TIMESTAMPTZ,
    login_count INTEGER DEFAULT 0,
    failed_login_count INTEGER DEFAULT 0,
    locked_until TIMESTAMPTZ,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_email ON users(email) WHERE email IS NOT NULL;
CREATE INDEX idx_users_active ON users(is_active);

-- ===================================================================
-- ROLES: Role-Based Access Control (RBAC)
-- ===================================================================
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,

    -- Role hierarchy
    parent_role_id INTEGER REFERENCES roles(id),
    priority INTEGER NOT NULL DEFAULT 0,  -- Higher = more privileged

    -- Built-in roles flag
    is_system BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insert default roles
INSERT INTO roles (name, description, priority, is_system) VALUES
    ('admin', 'Full system access', 1000, TRUE),
    ('engineer', 'Read/write access to code sources', 500, TRUE),
    ('user', 'Read access to allowed sources', 100, TRUE),
    ('viewer', 'Read-only access to public sources', 50, TRUE),
    ('agent', 'Claude Code agent access', 200, TRUE)
ON CONFLICT (name) DO NOTHING;

-- ===================================================================
-- USER_ROLES: Many-to-many user-role mapping
-- ===================================================================
CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    granted_by UUID REFERENCES users(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,  -- Optional role expiration

    PRIMARY KEY (user_id, role_id)
);

CREATE INDEX idx_user_roles_user ON user_roles(user_id);
CREATE INDEX idx_user_roles_role ON user_roles(role_id);

-- ===================================================================
-- PERMISSIONS: Fine-grained permissions (ABAC)
-- ===================================================================
CREATE TABLE IF NOT EXISTS permissions (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,  -- 'source', 'file', 'chunk', 'entity', 'query'
    action TEXT NOT NULL,  -- 'read', 'write', 'delete', 'search', 'admin'
    description TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insert default permissions
INSERT INTO permissions (name, resource_type, action, description) VALUES
    ('source:read', 'source', 'read', 'View source metadata'),
    ('source:write', 'source', 'write', 'Add/modify sources'),
    ('source:delete', 'source', 'delete', 'Remove sources'),
    ('source:search', 'source', 'search', 'Search within sources'),
    ('file:read', 'file', 'read', 'View file contents'),
    ('chunk:search', 'chunk', 'search', 'Semantic search on chunks'),
    ('entity:read', 'entity', 'read', 'View knowledge graph'),
    ('entity:write', 'entity', 'write', 'Modify knowledge graph'),
    ('query:execute', 'query', 'execute', 'Execute RAG queries'),
    ('analytics:read', 'analytics', 'read', 'View query analytics'),
    ('admin:users', 'admin', 'users', 'Manage users'),
    ('admin:roles', 'admin', 'roles', 'Manage roles'),
    ('admin:system', 'admin', 'system', 'System administration')
ON CONFLICT (name) DO NOTHING;

-- ===================================================================
-- ROLE_PERMISSIONS: Role-permission mapping
-- ===================================================================
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,

    PRIMARY KEY (role_id, permission_id)
);

-- Grant permissions to default roles
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p
WHERE r.name = 'admin'
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p
WHERE r.name = 'engineer' AND p.name IN (
    'source:read', 'source:write', 'source:search',
    'file:read', 'chunk:search', 'entity:read', 'entity:write',
    'query:execute', 'analytics:read'
)
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p
WHERE r.name = 'user' AND p.name IN (
    'source:read', 'source:search', 'file:read',
    'chunk:search', 'entity:read', 'query:execute'
)
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p
WHERE r.name = 'agent' AND p.name IN (
    'source:read', 'source:search', 'file:read',
    'chunk:search', 'entity:read', 'query:execute'
)
ON CONFLICT DO NOTHING;

-- ===================================================================
-- SOURCE_PERMISSIONS: Per-source access control (ReBAC)
-- Relationship-Based Access Control for fine-grained source access
-- ===================================================================
CREATE TABLE IF NOT EXISTS source_permissions (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,

    -- Can grant to user OR role
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,

    -- Permissions for this source
    can_read BOOLEAN NOT NULL DEFAULT TRUE,
    can_search BOOLEAN NOT NULL DEFAULT TRUE,
    can_write BOOLEAN NOT NULL DEFAULT FALSE,
    can_delete BOOLEAN NOT NULL DEFAULT FALSE,

    -- Grant metadata
    granted_by UUID REFERENCES users(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,

    -- Ensure at least one target
    CONSTRAINT source_perm_target CHECK (user_id IS NOT NULL OR role_id IS NOT NULL)
);

CREATE INDEX idx_source_perms_source ON source_permissions(source_id);
CREATE INDEX idx_source_perms_user ON source_permissions(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX idx_source_perms_role ON source_permissions(role_id) WHERE role_id IS NOT NULL;

-- ===================================================================
-- API_KEYS: For programmatic access (CLI, agents)
-- ===================================================================
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,

    -- Key storage (prefix visible, hash for verification)
    key_prefix TEXT NOT NULL,  -- e.g., "crag_sk_abc123"
    key_hash TEXT NOT NULL,  -- SHA256 of full key

    -- Permissions
    scopes TEXT[] NOT NULL DEFAULT ARRAY['read'],  -- 'read', 'write', 'admin'

    -- Rate limiting
    rate_limit_per_minute INTEGER DEFAULT 60,
    rate_limit_per_day INTEGER DEFAULT 10000,

    -- Usage tracking
    last_used TIMESTAMPTZ,
    use_count BIGINT DEFAULT 0,

    -- Lifecycle
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoked_by UUID REFERENCES users(id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_api_keys_user ON api_keys(user_id);
CREATE INDEX idx_api_keys_prefix ON api_keys(key_prefix);
CREATE INDEX idx_api_keys_active ON api_keys(is_active) WHERE is_active = TRUE;

-- ===================================================================
-- SESSIONS: User sessions for web frontend
-- ===================================================================
CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Session token (hashed)
    token_hash TEXT NOT NULL UNIQUE,

    -- Session metadata
    ip_address INET,
    user_agent TEXT,
    device_info JSONB,

    -- Lifecycle
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_token ON sessions(token_hash);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
CREATE INDEX idx_sessions_active ON sessions(is_active, expires_at);

-- ===================================================================
-- AUDIT_LOG: Comprehensive audit trail (SIEM-ready)
-- ===================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,

    -- Actor
    user_id UUID REFERENCES users(id),
    api_key_id UUID REFERENCES api_keys(id),
    session_id UUID REFERENCES sessions(id),
    ip_address INET,
    user_agent TEXT,

    -- Action
    action TEXT NOT NULL,  -- 'login', 'logout', 'search', 'add_source', etc.
    resource_type TEXT,  -- 'source', 'file', 'user', etc.
    resource_id TEXT,  -- ID of affected resource

    -- Details
    details JSONB,  -- Action-specific details
    query_text TEXT,  -- For search actions

    -- Result
    success BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,

    -- Performance
    duration_ms INTEGER,

    -- Timestamp (partitioned by month for performance)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) PARTITION BY RANGE (created_at);

-- Create initial partitions (extend as needed)
CREATE TABLE audit_log_2025_01 PARTITION OF audit_log
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
CREATE TABLE audit_log_2025_02 PARTITION OF audit_log
    FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');
CREATE TABLE audit_log_2025_03 PARTITION OF audit_log
    FOR VALUES FROM ('2025-03-01') TO ('2025-04-01');
CREATE TABLE audit_log_2025_04 PARTITION OF audit_log
    FOR VALUES FROM ('2025-04-01') TO ('2025-05-01');
CREATE TABLE audit_log_2025_05 PARTITION OF audit_log
    FOR VALUES FROM ('2025-05-01') TO ('2025-06-01');
CREATE TABLE audit_log_2025_06 PARTITION OF audit_log
    FOR VALUES FROM ('2025-06-01') TO ('2025-07-01');
CREATE TABLE audit_log_2025_07 PARTITION OF audit_log
    FOR VALUES FROM ('2025-07-01') TO ('2025-08-01');
CREATE TABLE audit_log_2025_08 PARTITION OF audit_log
    FOR VALUES FROM ('2025-08-01') TO ('2025-09-01');
CREATE TABLE audit_log_2025_09 PARTITION OF audit_log
    FOR VALUES FROM ('2025-09-01') TO ('2025-10-01');
CREATE TABLE audit_log_2025_10 PARTITION OF audit_log
    FOR VALUES FROM ('2025-10-01') TO ('2025-11-01');
CREATE TABLE audit_log_2025_11 PARTITION OF audit_log
    FOR VALUES FROM ('2025-11-01') TO ('2025-12-01');
CREATE TABLE audit_log_2025_12 PARTITION OF audit_log
    FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');

CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
CREATE INDEX idx_audit_time ON audit_log(created_at);
CREATE INDEX idx_audit_success ON audit_log(success) WHERE success = FALSE;

COMMENT ON TABLE audit_log IS
'Immutable audit log for compliance and security analysis.
Partitioned by month for query performance and retention management.
All actions are logged including: authentication, queries, modifications.
SIEM-ready format for security monitoring integration.';

-- ===================================================================
-- RATE_LIMITS: Per-user rate limiting state
-- ===================================================================
CREATE TABLE IF NOT EXISTS rate_limits (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    api_key_id UUID REFERENCES api_keys(id) ON DELETE CASCADE,

    -- Rate limit window
    window_type TEXT NOT NULL,  -- 'minute', 'hour', 'day'
    window_start TIMESTAMPTZ NOT NULL,

    -- Counters
    request_count INTEGER NOT NULL DEFAULT 0,

    -- Ensure one target
    CONSTRAINT rate_limit_target CHECK (user_id IS NOT NULL OR api_key_id IS NOT NULL),
    UNIQUE (user_id, window_type, window_start),
    UNIQUE (api_key_id, window_type, window_start)
);

CREATE INDEX idx_rate_limits_user ON rate_limits(user_id, window_start);
CREATE INDEX idx_rate_limits_key ON rate_limits(api_key_id, window_start);

-- ===================================================================
-- SENSITIVE_PATTERNS: PII/Secret detection patterns
-- For data loss prevention (DLP)
-- ===================================================================
CREATE TABLE IF NOT EXISTS sensitive_patterns (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    pattern TEXT NOT NULL,  -- Regex pattern
    category TEXT NOT NULL,  -- 'pii', 'secret', 'credential', 'financial'
    severity TEXT NOT NULL DEFAULT 'medium',  -- 'low', 'medium', 'high', 'critical'
    action TEXT NOT NULL DEFAULT 'warn',  -- 'warn', 'redact', 'block'
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insert common sensitive patterns
INSERT INTO sensitive_patterns (name, pattern, category, severity, action) VALUES
    ('email', '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', 'pii', 'medium', 'warn'),
    ('phone', '\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', 'pii', 'medium', 'warn'),
    ('ssn', '\b\d{3}-\d{2}-\d{4}\b', 'pii', 'critical', 'block'),
    ('credit_card', '\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', 'financial', 'critical', 'block'),
    ('api_key_aws', 'AKIA[0-9A-Z]{16}', 'secret', 'critical', 'block'),
    ('api_key_generic', '[a-zA-Z0-9_-]{32,}', 'secret', 'high', 'warn'),
    ('private_key', '-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----', 'secret', 'critical', 'block'),
    ('jwt', 'eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*', 'secret', 'high', 'warn'),
    ('password_field', '(password|passwd|pwd)\s*[:=]\s*[^\s]+', 'credential', 'critical', 'redact'),
    ('github_token', 'gh[pousr]_[A-Za-z0-9_]{36,}', 'secret', 'critical', 'block')
ON CONFLICT (name) DO NOTHING;

-- ===================================================================
-- SENSITIVE_FINDINGS: Detected sensitive data in content
-- ===================================================================
CREATE TABLE IF NOT EXISTS sensitive_findings (
    id BIGSERIAL PRIMARY KEY,
    file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    pattern_id INTEGER NOT NULL REFERENCES sensitive_patterns(id),

    -- Finding details
    line_number INTEGER,
    column_start INTEGER,
    column_end INTEGER,
    matched_text TEXT,  -- Can be redacted
    context TEXT,  -- Surrounding text (redacted)

    -- Status
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'reviewed', 'false_positive', 'remediated'
    reviewed_by UUID REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    notes TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sensitive_file ON sensitive_findings(file_id);
CREATE INDEX idx_sensitive_chunk ON sensitive_findings(chunk_id);
CREATE INDEX idx_sensitive_status ON sensitive_findings(status);
CREATE INDEX idx_sensitive_pattern ON sensitive_findings(pattern_id);

-- ===================================================================
-- Security Helper Functions
-- ===================================================================

-- Function to check if user has permission for source
CREATE OR REPLACE FUNCTION user_can_access_source(
    p_user_id UUID,
    p_source_id BIGINT,
    p_action TEXT DEFAULT 'read'
) RETURNS BOOLEAN AS $$
DECLARE
    has_permission BOOLEAN := FALSE;
    user_is_admin BOOLEAN;
BEGIN
    -- Check if user is admin
    SELECT is_admin INTO user_is_admin FROM users WHERE id = p_user_id;
    IF user_is_admin THEN
        RETURN TRUE;
    END IF;

    -- Check direct user permission
    SELECT EXISTS(
        SELECT 1 FROM source_permissions sp
        WHERE sp.source_id = p_source_id
          AND sp.user_id = p_user_id
          AND (sp.expires_at IS NULL OR sp.expires_at > NOW())
          AND CASE p_action
              WHEN 'read' THEN sp.can_read
              WHEN 'search' THEN sp.can_search
              WHEN 'write' THEN sp.can_write
              WHEN 'delete' THEN sp.can_delete
              ELSE FALSE
          END
    ) INTO has_permission;

    IF has_permission THEN
        RETURN TRUE;
    END IF;

    -- Check role-based permission
    SELECT EXISTS(
        SELECT 1 FROM source_permissions sp
        JOIN user_roles ur ON ur.role_id = sp.role_id
        WHERE sp.source_id = p_source_id
          AND ur.user_id = p_user_id
          AND (ur.expires_at IS NULL OR ur.expires_at > NOW())
          AND (sp.expires_at IS NULL OR sp.expires_at > NOW())
          AND CASE p_action
              WHEN 'read' THEN sp.can_read
              WHEN 'search' THEN sp.can_search
              WHEN 'write' THEN sp.can_write
              WHEN 'delete' THEN sp.can_delete
              ELSE FALSE
          END
    ) INTO has_permission;

    RETURN has_permission;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get all accessible sources for a user
CREATE OR REPLACE FUNCTION get_accessible_sources(p_user_id UUID)
RETURNS TABLE (
    source_id BIGINT,
    source_name TEXT,
    can_read BOOLEAN,
    can_search BOOLEAN,
    can_write BOOLEAN
) AS $$
BEGIN
    -- Check if admin
    IF EXISTS(SELECT 1 FROM users WHERE id = p_user_id AND is_admin = TRUE) THEN
        RETURN QUERY
        SELECT s.id, s.name, TRUE, TRUE, TRUE
        FROM sources s;
        RETURN;
    END IF;

    RETURN QUERY
    SELECT DISTINCT ON (s.id)
        s.id,
        s.name,
        COALESCE(bool_or(sp.can_read), FALSE),
        COALESCE(bool_or(sp.can_search), FALSE),
        COALESCE(bool_or(sp.can_write), FALSE)
    FROM sources s
    LEFT JOIN source_permissions sp ON sp.source_id = s.id
    LEFT JOIN user_roles ur ON ur.role_id = sp.role_id AND ur.user_id = p_user_id
    WHERE sp.user_id = p_user_id
       OR ur.user_id = p_user_id
    GROUP BY s.id, s.name;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to log audit event
CREATE OR REPLACE FUNCTION log_audit_event(
    p_user_id UUID,
    p_action TEXT,
    p_resource_type TEXT DEFAULT NULL,
    p_resource_id TEXT DEFAULT NULL,
    p_details JSONB DEFAULT NULL,
    p_success BOOLEAN DEFAULT TRUE,
    p_error_message TEXT DEFAULT NULL
) RETURNS BIGINT AS $$
DECLARE
    new_id BIGINT;
BEGIN
    INSERT INTO audit_log (
        user_id, action, resource_type, resource_id,
        details, success, error_message
    ) VALUES (
        p_user_id, p_action, p_resource_type, p_resource_id,
        p_details, p_success, p_error_message
    ) RETURNING id INTO new_id;

    RETURN new_id;
END;
$$ LANGUAGE plpgsql;

-- Function to check rate limit
CREATE OR REPLACE FUNCTION check_rate_limit(
    p_user_id UUID,
    p_limit_per_minute INTEGER DEFAULT 60
) RETURNS BOOLEAN AS $$
DECLARE
    current_count INTEGER;
    window_start TIMESTAMPTZ;
BEGIN
    window_start := date_trunc('minute', NOW());

    -- Get or create rate limit record
    INSERT INTO rate_limits (user_id, window_type, window_start, request_count)
    VALUES (p_user_id, 'minute', window_start, 1)
    ON CONFLICT (user_id, window_type, window_start)
    DO UPDATE SET request_count = rate_limits.request_count + 1
    RETURNING request_count INTO current_count;

    RETURN current_count <= p_limit_per_minute;
END;
$$ LANGUAGE plpgsql;

-- ===================================================================
-- Row-Level Security (RLS) Policies
-- ===================================================================

-- Enable RLS on sensitive tables
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE files ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;

-- FORCE RLS even for table owner (critical for security!)
-- Without FORCE, the table owner (mainrag) would bypass all policies
ALTER TABLE sources FORCE ROW LEVEL SECURITY;
ALTER TABLE files FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;

-- Policy: Users can only see sources they have access to
CREATE POLICY source_access_policy ON sources
    FOR SELECT
    USING (
        -- Admins see all
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
        OR
        -- Users see sources they have permission for
        user_can_access_source(current_setting('app.user_id')::UUID, id, 'read')
    );

-- Policy: Users can only see files from accessible sources
CREATE POLICY file_access_policy ON files
    FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
        OR
        user_can_access_source(current_setting('app.user_id')::UUID, source_id, 'read')
    );

-- Policy: Users can only see chunks from accessible files
CREATE POLICY chunk_access_policy ON chunks
    FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
        OR
        EXISTS (
            SELECT 1 FROM files f
            WHERE f.id = file_id
            AND user_can_access_source(current_setting('app.user_id')::UUID, f.source_id, 'read')
        )
    );

-- ===================================================================
-- Admin write policies (INSERT, UPDATE, DELETE)
-- Required because FORCE ROW LEVEL SECURITY blocks even table owner
-- ===================================================================

-- Sources: Admin can INSERT/UPDATE/DELETE
CREATE POLICY source_admin_insert ON sources
    FOR INSERT
    WITH CHECK (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

CREATE POLICY source_admin_update ON sources
    FOR UPDATE
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

CREATE POLICY source_admin_delete ON sources
    FOR DELETE
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

-- Files: Admin can INSERT/UPDATE/DELETE
CREATE POLICY file_admin_insert ON files
    FOR INSERT
    WITH CHECK (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

CREATE POLICY file_admin_update ON files
    FOR UPDATE
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

CREATE POLICY file_admin_delete ON files
    FOR DELETE
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

-- Chunks: Admin can INSERT/UPDATE/DELETE
CREATE POLICY chunk_admin_insert ON chunks
    FOR INSERT
    WITH CHECK (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

CREATE POLICY chunk_admin_update ON chunks
    FOR UPDATE
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

CREATE POLICY chunk_admin_delete ON chunks
    FOR DELETE
    USING (
        EXISTS (SELECT 1 FROM users u WHERE u.id = current_setting('app.user_id')::UUID AND u.is_admin = TRUE)
    );

-- ===================================================================
-- Grants for mainrag application user
-- ===================================================================
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainrag;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mainrag;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mainrag;

-- ===================================================================
-- Initial Admin User (change password immediately!)
-- ===================================================================
-- Password: "<REDACTED_ADMIN_PW>" (bcrypt hash)
INSERT INTO users (username, email, password_hash, display_name, is_active, is_verified, is_admin)
VALUES (
    'admin',
    'admin@localhost',
    '<REDACTED_BCRYPT_HASH>',  -- <REDACTED_ADMIN_PW>
    'Administrator',
    TRUE,
    TRUE,
    TRUE
)
ON CONFLICT (username) DO NOTHING;

-- Assign admin role to admin user
INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id
FROM users u, roles r
WHERE u.username = 'admin' AND r.name = 'admin'
ON CONFLICT DO NOTHING;

COMMENT ON TABLE users IS
'User accounts with secure bcrypt/argon2 password hashing.
Supports MFA via TOTP, account locking, and session management.
Initial admin password: <REDACTED_ADMIN_PW> - MUST be changed immediately!';
