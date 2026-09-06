-- Minimal schema for the real non-streaming skip paths and intelligence writer.
-- No embeddings table: accidentally reaching vector writes must fail this test.
CREATE TABLE files (
    id BIGSERIAL PRIMARY KEY, source_id BIGINT NOT NULL, path TEXT NOT NULL,
    hash BYTEA NOT NULL, content BYTEA NOT NULL, content_text TEXT, language TEXT,
    size_original INTEGER NOT NULL, size_compressed INTEGER NOT NULL,
    last_modified TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    intelligence_analyzed_at TIMESTAMPTZ,
    intelligence_symbols_count INTEGER NOT NULL DEFAULT 0,
    intelligence_calls_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_id, path)
);
CREATE TABLE symbols (
    id BIGSERIAL PRIMARY KEY, file_id BIGINT NOT NULL REFERENCES files(id),
    name TEXT NOT NULL, qualified_name TEXT, type TEXT NOT NULL,
    line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
    context TEXT, signature TEXT, doc_comment TEXT, visibility TEXT, language TEXT,
    UNIQUE(file_id, name, line_start)
);
CREATE TABLE call_graph (
    id BIGSERIAL PRIMARY KEY,
    caller_symbol_id BIGINT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    callee_symbol_id BIGINT REFERENCES symbols(id) ON DELETE SET NULL,
    callee_name TEXT NOT NULL, call_line INTEGER NOT NULL, call_type TEXT NOT NULL
);
CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY, file_id BIGINT NOT NULL REFERENCES files(id),
    start_line INTEGER NOT NULL, chunk_content_hash TEXT, chunker_version TEXT,
    embedding_model_id TEXT, tokenizer_version TEXT
);
CREATE TABLE indexing_outbox (
    action TEXT NOT NULL, chunk_id BIGINT NOT NULL, file_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL, payload JSONB NOT NULL
);
CREATE FUNCTION fixture_fail() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'issue51 synthetic write failure';
END;
$$;
CREATE TRIGGER fixture_failure BEFORE INSERT ON symbols
    FOR EACH STATEMENT EXECUTE FUNCTION fixture_fail();
ALTER TABLE symbols DISABLE TRIGGER fixture_failure;
CREATE TRIGGER fixture_failure BEFORE INSERT ON call_graph
    FOR EACH STATEMENT EXECUTE FUNCTION fixture_fail();
ALTER TABLE call_graph DISABLE TRIGGER fixture_failure;
