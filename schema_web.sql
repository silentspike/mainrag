-- ===================================================================
-- MAINRAG Web Frontend Schema Extension
-- ===================================================================
-- Version: 1.0
-- Supports: Next.js/React frontend with Vercel AI SDK integration
-- Reference: https://github.com/vercel/ai-sdk-rag-starter
-- Reference: https://sdk.vercel.ai/docs/guides/rag-chatbot
-- ===================================================================

-- ===================================================================
-- CONVERSATIONS: Chat conversation threads
-- ===================================================================
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Conversation metadata
    title TEXT,  -- Auto-generated or user-set
    description TEXT,
    is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,

    -- Context settings
    default_sources BIGINT[],  -- Source IDs to search by default
    system_prompt TEXT,  -- Custom system prompt
    model_settings JSONB,  -- temperature, max_tokens, etc.

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ
);

CREATE INDEX idx_conversations_user ON conversations(user_id);
CREATE INDEX idx_conversations_user_recent ON conversations(user_id, last_message_at DESC);
CREATE INDEX idx_conversations_pinned ON conversations(user_id, is_pinned) WHERE is_pinned = TRUE;

-- ===================================================================
-- MESSAGES: Individual chat messages
-- ===================================================================
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

    -- Message content
    role TEXT NOT NULL,  -- 'user', 'assistant', 'system'
    content TEXT NOT NULL,

    -- RAG context (what was retrieved for this message)
    retrieved_chunks BIGINT[],  -- chunk IDs used as context
    search_queries TEXT[],  -- Queries that were run
    sources_used TEXT[],  -- Source names that provided context

    -- Token usage
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,

    -- Model info
    model TEXT,  -- e.g., 'claude-3-opus'
    latency_ms INTEGER,

    -- Feedback
    user_rating INTEGER,  -- -1, 0, 1
    user_feedback TEXT,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id);
CREATE INDEX idx_messages_conv_time ON messages(conversation_id, created_at);
CREATE INDEX idx_messages_role ON messages(role);
CREATE INDEX idx_messages_feedback ON messages(user_rating) WHERE user_rating IS NOT NULL;

-- ===================================================================
-- MESSAGE_ATTACHMENTS: Files attached to messages
-- ===================================================================
CREATE TABLE IF NOT EXISTS message_attachments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,

    -- File info
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,

    -- Storage
    storage_path TEXT NOT NULL,  -- Path in object storage
    storage_provider TEXT NOT NULL DEFAULT 'local',  -- 'local', 's3', 'gcs'

    -- Processing status
    is_processed BOOLEAN NOT NULL DEFAULT FALSE,
    processing_error TEXT,

    -- If indexed into RAG
    file_id BIGINT REFERENCES files(id) ON DELETE SET NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_attachments_message ON message_attachments(message_id);

-- ===================================================================
-- SEARCH_HISTORY: User search history for quick access
-- ===================================================================
CREATE TABLE IF NOT EXISTS search_history (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Search details
    query TEXT NOT NULL,
    query_vector vector(384),  -- For "similar searches" feature
    search_type TEXT NOT NULL,  -- 'semantic', 'fts', 'hybrid', 'graph'
    source_filter TEXT,  -- Which source was filtered

    -- Results summary
    result_count INTEGER,
    top_result_ids BIGINT[],

    -- Interaction
    clicked_results BIGINT[],  -- Which results user clicked
    saved_to_collection BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_search_history_user ON search_history(user_id);
CREATE INDEX idx_search_history_user_time ON search_history(user_id, created_at DESC);
CREATE INDEX idx_search_history_query_trgm ON search_history USING GIN (query gin_trgm_ops);

-- HNSW for "similar searches" feature
CREATE INDEX idx_search_history_vector ON search_history
    USING hnsw (query_vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE query_vector IS NOT NULL;

-- ===================================================================
-- COLLECTIONS: User-curated collections of chunks/files
-- ===================================================================
CREATE TABLE IF NOT EXISTS collections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Collection metadata
    name TEXT NOT NULL,
    description TEXT,
    color TEXT,  -- For UI display
    icon TEXT,  -- Icon name

    -- Sharing
    is_public BOOLEAN NOT NULL DEFAULT FALSE,
    share_token TEXT UNIQUE,  -- For sharing links

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_collections_user ON collections(user_id);
CREATE INDEX idx_collections_public ON collections(is_public) WHERE is_public = TRUE;
CREATE INDEX idx_collections_share ON collections(share_token) WHERE share_token IS NOT NULL;

-- ===================================================================
-- COLLECTION_ITEMS: Items in collections
-- ===================================================================
CREATE TABLE IF NOT EXISTS collection_items (
    id BIGSERIAL PRIMARY KEY,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,

    -- Can reference different entity types
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
    entity_id BIGINT REFERENCES entities(id) ON DELETE CASCADE,

    -- Item metadata
    note TEXT,  -- User's note about this item
    highlight TEXT,  -- Highlighted portion
    tags TEXT[],

    -- Ordering
    position INTEGER NOT NULL DEFAULT 0,

    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Ensure at least one reference
    CONSTRAINT collection_item_ref CHECK (
        chunk_id IS NOT NULL OR file_id IS NOT NULL OR entity_id IS NOT NULL
    )
);

CREATE INDEX idx_collection_items_collection ON collection_items(collection_id);
CREATE INDEX idx_collection_items_chunk ON collection_items(chunk_id) WHERE chunk_id IS NOT NULL;
CREATE INDEX idx_collection_items_file ON collection_items(file_id) WHERE file_id IS NOT NULL;

-- ===================================================================
-- USER_PREFERENCES: UI and behavior preferences
-- ===================================================================
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,

    -- UI Theme
    theme TEXT NOT NULL DEFAULT 'system',  -- 'light', 'dark', 'system'
    accent_color TEXT DEFAULT 'blue',
    font_size TEXT DEFAULT 'medium',  -- 'small', 'medium', 'large'

    -- Search preferences
    default_search_type TEXT DEFAULT 'hybrid',
    default_result_limit INTEGER DEFAULT 10,
    show_source_filter BOOLEAN DEFAULT TRUE,
    auto_search_similar BOOLEAN DEFAULT TRUE,

    -- Chat preferences
    stream_responses BOOLEAN DEFAULT TRUE,
    show_token_count BOOLEAN DEFAULT FALSE,
    show_latency BOOLEAN DEFAULT FALSE,
    default_model TEXT DEFAULT 'claude-3-sonnet',

    -- RAG preferences
    show_retrieved_chunks BOOLEAN DEFAULT TRUE,
    chunk_preview_lines INTEGER DEFAULT 5,
    highlight_matches BOOLEAN DEFAULT TRUE,

    -- Notifications
    email_notifications BOOLEAN DEFAULT FALSE,
    browser_notifications BOOLEAN DEFAULT FALSE,

    -- Keyboard shortcuts
    shortcuts JSONB DEFAULT '{}',

    -- Advanced
    developer_mode BOOLEAN DEFAULT FALSE,
    show_debug_info BOOLEAN DEFAULT FALSE,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ===================================================================
-- BOOKMARKS: Quick access to specific content
-- ===================================================================
CREATE TABLE IF NOT EXISTS bookmarks (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Bookmarked item
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    message_id UUID REFERENCES messages(id) ON DELETE CASCADE,

    -- Bookmark metadata
    title TEXT,  -- User-set or auto-generated
    note TEXT,
    tags TEXT[],

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT bookmark_ref CHECK (
        chunk_id IS NOT NULL OR file_id IS NOT NULL OR
        conversation_id IS NOT NULL OR message_id IS NOT NULL
    )
);

CREATE INDEX idx_bookmarks_user ON bookmarks(user_id);
CREATE INDEX idx_bookmarks_user_time ON bookmarks(user_id, created_at DESC);
CREATE INDEX idx_bookmarks_tags ON bookmarks USING GIN (tags);

-- ===================================================================
-- SHARED_LINKS: Public sharing of conversations/searches
-- ===================================================================
CREATE TABLE IF NOT EXISTS shared_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- What is shared
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    collection_id UUID REFERENCES collections(id) ON DELETE CASCADE,

    -- Share settings
    token TEXT NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    password_hash TEXT,  -- Optional password protection

    -- Limits
    expires_at TIMESTAMPTZ,
    max_views INTEGER,
    view_count INTEGER DEFAULT 0,

    -- Access log
    last_accessed TIMESTAMPTZ,
    access_log JSONB DEFAULT '[]',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT shared_link_ref CHECK (
        conversation_id IS NOT NULL OR collection_id IS NOT NULL
    )
);

CREATE INDEX idx_shared_links_token ON shared_links(token);
CREATE INDEX idx_shared_links_user ON shared_links(user_id);
CREATE INDEX idx_shared_links_active ON shared_links(is_active) WHERE is_active = TRUE;

-- ===================================================================
-- NOTIFICATIONS: In-app notifications
-- ===================================================================
CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Notification content
    type TEXT NOT NULL,  -- 'info', 'success', 'warning', 'error', 'mention'
    title TEXT NOT NULL,
    message TEXT,
    action_url TEXT,  -- Where to go when clicked

    -- Status
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ,

    -- Metadata
    metadata JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notifications_user ON notifications(user_id);
CREATE INDEX idx_notifications_user_unread ON notifications(user_id, is_read) WHERE is_read = FALSE;
CREATE INDEX idx_notifications_time ON notifications(created_at);

-- ===================================================================
-- AGENT_SESSIONS: Claude Code agent sessions
-- ===================================================================
CREATE TABLE IF NOT EXISTS agent_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    api_key_id UUID REFERENCES api_keys(id) ON DELETE SET NULL,

    -- Session metadata
    agent_name TEXT,  -- e.g., 'coderag-cli', 'claude-code-agent-1'
    workspace_path TEXT,
    project_name TEXT,

    -- Session state
    status TEXT NOT NULL DEFAULT 'active',  -- 'active', 'idle', 'terminated'
    last_query TIMESTAMPTZ,
    query_count INTEGER DEFAULT 0,

    -- Resource usage
    tokens_used BIGINT DEFAULT 0,
    chunks_retrieved BIGINT DEFAULT 0,

    -- Timestamps
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    last_heartbeat TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_agent_sessions_user ON agent_sessions(user_id);
CREATE INDEX idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX idx_agent_sessions_active ON agent_sessions(status) WHERE status = 'active';

-- ===================================================================
-- FEATURE_FLAGS: Feature toggles for gradual rollout
-- ===================================================================
CREATE TABLE IF NOT EXISTS feature_flags (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,

    -- Rollout settings
    is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    rollout_percentage INTEGER DEFAULT 0,  -- 0-100
    enabled_for_users UUID[],  -- Specific users
    enabled_for_roles INTEGER[],  -- Specific roles

    -- Metadata
    metadata JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insert default feature flags
INSERT INTO feature_flags (name, description, is_enabled, rollout_percentage) VALUES
    ('graphrag', 'Enable GraphRAG multi-hop queries', TRUE, 100),
    ('hype', 'Enable HyPE hypothetical questions', TRUE, 100),
    ('colbert', 'Enable ColBERT reranking', FALSE, 0),
    ('late_chunking', 'Enable JinaAI late chunking', FALSE, 0),
    ('code_ast', 'Enable AST-based code analysis', TRUE, 100),
    ('multi_model', 'Enable multi-model embeddings', FALSE, 0),
    ('web_search', 'Enable web search in RAG', FALSE, 0),
    ('streaming', 'Enable response streaming', TRUE, 100),
    ('dark_mode', 'Enable dark mode UI', TRUE, 100),
    ('export_pdf', 'Enable PDF export', TRUE, 100)
ON CONFLICT (name) DO NOTHING;

-- Function to check if feature is enabled for user
CREATE OR REPLACE FUNCTION is_feature_enabled(
    p_feature_name TEXT,
    p_user_id UUID DEFAULT NULL
) RETURNS BOOLEAN AS $$
DECLARE
    flag RECORD;
    user_role_ids INTEGER[];
BEGIN
    SELECT * INTO flag FROM feature_flags WHERE name = p_feature_name;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- Check global enable
    IF NOT flag.is_enabled THEN
        RETURN FALSE;
    END IF;

    -- Check specific user enable
    IF p_user_id = ANY(flag.enabled_for_users) THEN
        RETURN TRUE;
    END IF;

    -- Check role-based enable
    IF p_user_id IS NOT NULL AND flag.enabled_for_roles IS NOT NULL THEN
        SELECT ARRAY_AGG(role_id) INTO user_role_ids
        FROM user_roles WHERE user_id = p_user_id;

        IF user_role_ids && flag.enabled_for_roles THEN
            RETURN TRUE;
        END IF;
    END IF;

    -- Check percentage rollout (deterministic based on user_id)
    IF flag.rollout_percentage >= 100 THEN
        RETURN TRUE;
    ELSIF flag.rollout_percentage > 0 AND p_user_id IS NOT NULL THEN
        RETURN (hashtext(p_user_id::TEXT) % 100) < flag.rollout_percentage;
    END IF;

    RETURN FALSE;
END;
$$ LANGUAGE plpgsql;

-- ===================================================================
-- Views for Frontend
-- ===================================================================

-- Recent conversations view
CREATE OR REPLACE VIEW v_recent_conversations AS
SELECT
    c.id,
    c.user_id,
    c.title,
    c.is_pinned,
    c.is_archived,
    c.created_at,
    c.last_message_at,
    COUNT(m.id) AS message_count,
    (
        SELECT content FROM messages m2
        WHERE m2.conversation_id = c.id
        ORDER BY m2.created_at DESC LIMIT 1
    ) AS last_message_preview
FROM conversations c
LEFT JOIN messages m ON m.conversation_id = c.id
GROUP BY c.id
ORDER BY c.is_pinned DESC, c.last_message_at DESC NULLS LAST;

-- Search analytics view
CREATE OR REPLACE VIEW v_search_analytics AS
SELECT
    DATE_TRUNC('day', created_at) AS date,
    search_type,
    COUNT(*) AS search_count,
    AVG(result_count) AS avg_results,
    COUNT(DISTINCT user_id) AS unique_users
FROM search_history
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY DATE_TRUNC('day', created_at), search_type
ORDER BY date DESC, search_type;

-- ===================================================================
-- Grants
-- ===================================================================
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO coderag;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO coderag;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO coderag;
