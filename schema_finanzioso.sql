-- ===================================================================
-- Finanzioso PostgreSQL Schema - AI Stock Assistant
-- ===================================================================
-- Version: 1.0
-- Part of MAINRAG - extends existing infrastructure
-- WARNING: This is NOT investment advice - educational/research only!
-- ===================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Create dedicated schema
CREATE SCHEMA IF NOT EXISTS finanzioso;

-- ===================================================================
-- ENUMS
-- ===================================================================

CREATE TYPE finanzioso.event_status AS ENUM ('pending', 'processing', 'done', 'failed');
CREATE TYPE finanzioso.action_type AS ENUM ('BUY', 'HOLD', 'SELL');
CREATE TYPE finanzioso.confidence_level AS ENUM ('HIGH', 'MEDIUM', 'LOW');
CREATE TYPE finanzioso.source_class AS ENUM ('A', 'B', 'C');
CREATE TYPE finanzioso.signal_type AS ENUM ('PRICE_MOVE', 'NEWS', 'FILING', 'SENTIMENT', 'VOLUME');

-- ===================================================================
-- TABLES: Core Entities
-- ===================================================================

-- Stocks: Watchlist entries
CREATE TABLE finanzioso.stocks (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    name VARCHAR(255),
    exchange VARCHAR(20) NOT NULL DEFAULT 'NYSE',
    cik VARCHAR(20),  -- SEC CIK number
    cik_verified BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT stocks_symbol_exchange_unique UNIQUE (symbol, exchange),
    CONSTRAINT stocks_symbol_format CHECK (symbol ~ '^[A-Z0-9.]+$')
);

CREATE INDEX idx_stocks_symbol ON finanzioso.stocks(symbol);
CREATE INDEX idx_stocks_cik ON finanzioso.stocks(cik) WHERE cik IS NOT NULL;
CREATE INDEX idx_stocks_active ON finanzioso.stocks(active) WHERE active = TRUE;

-- Trading Calendar: NYSE/NASDAQ trading days
CREATE TABLE finanzioso.trading_calendar (
    id BIGSERIAL PRIMARY KEY,
    exchange VARCHAR(20) NOT NULL,
    date DATE NOT NULL,
    is_trading_day BOOLEAN NOT NULL DEFAULT TRUE,
    early_close BOOLEAN DEFAULT FALSE,
    holiday_name VARCHAR(100),

    CONSTRAINT trading_calendar_unique UNIQUE (exchange, date)
);

CREATE INDEX idx_trading_calendar_date ON finanzioso.trading_calendar(date);
CREATE INDEX idx_trading_calendar_trading_days ON finanzioso.trading_calendar(exchange, date)
    WHERE is_trading_day = TRUE;

-- ===================================================================
-- TABLES: Price Data
-- ===================================================================

-- Daily Prices: OHLCV data from providers
CREATE TABLE finanzioso.prices_daily (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT NOT NULL REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    provider VARCHAR(50) NOT NULL DEFAULT 'yahoo',
    open NUMERIC(12, 4),
    high NUMERIC(12, 4),
    low NUMERIC(12, 4),
    close NUMERIC(12, 4) NOT NULL,
    adj_close NUMERIC(12, 4),
    volume BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT prices_daily_unique UNIQUE (stock_id, date, provider),
    CONSTRAINT prices_daily_ohlc_valid CHECK (
        (high IS NULL OR low IS NULL) OR (high >= low)
    )
);

CREATE INDEX idx_prices_stock_date ON finanzioso.prices_daily(stock_id, date DESC);
CREATE INDEX idx_prices_date ON finanzioso.prices_daily(date DESC);

-- Stock Symbols: Provider-specific symbol mappings
CREATE TABLE finanzioso.stock_symbols (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT NOT NULL REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL,
    provider_symbol VARCHAR(50) NOT NULL,
    verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT stock_symbols_unique UNIQUE (stock_id, provider)
);

-- ===================================================================
-- TABLES: Scores & Signals
-- ===================================================================

-- Scores: Calculated stock scores
CREATE TABLE finanzioso.scores (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT NOT NULL REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    score NUMERIC(5, 2) NOT NULL,  -- 0.00 to 100.00
    action finanzioso.action_type NOT NULL DEFAULT 'HOLD',
    confidence finanzioso.confidence_level NOT NULL DEFAULT 'LOW',
    confidence_score NUMERIC(5, 2),  -- 0.00 to 100.00
    safe_mode BOOLEAN NOT NULL DEFAULT FALSE,
    safe_mode_reason TEXT,
    factors JSONB,  -- {"price_momentum": 0.5, "volume_signal": -0.2, ...}
    data_snapshot JSONB,  -- Snapshot of input data for debugging
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    idempotency_key VARCHAR(64),

    CONSTRAINT scores_score_range CHECK (score >= 0 AND score <= 100),
    CONSTRAINT scores_confidence_range CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 100)),
    CONSTRAINT scores_idempotency_unique UNIQUE (idempotency_key)
);

CREATE INDEX idx_scores_stock ON finanzioso.scores(stock_id);
CREATE INDEX idx_scores_calculated ON finanzioso.scores(calculated_at DESC);
CREATE INDEX idx_scores_stock_calculated ON finanzioso.scores(stock_id, calculated_at DESC);
CREATE INDEX idx_scores_action ON finanzioso.scores(action);
CREATE INDEX idx_scores_safe_mode ON finanzioso.scores(safe_mode) WHERE safe_mode = TRUE;

-- Signals: Evidence supporting scores
CREATE TABLE finanzioso.signals (
    id BIGSERIAL PRIMARY KEY,
    score_id BIGINT NOT NULL REFERENCES finanzioso.scores(id) ON DELETE CASCADE,
    signal_type finanzioso.signal_type NOT NULL,
    quote TEXT,  -- Relevant text excerpt
    span_start INTEGER,  -- Position in source chunk
    span_end INTEGER,
    impact NUMERIC(5, 2),  -- Impact on score (positive or negative)
    evidence_strength INTEGER CHECK (evidence_strength >= 1 AND evidence_strength <= 5),
    source_url TEXT,
    source_class finanzioso.source_class,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signals_score ON finanzioso.signals(score_id);
CREATE INDEX idx_signals_type ON finanzioso.signals(signal_type);
CREATE INDEX idx_signals_strength ON finanzioso.signals(evidence_strength DESC);

-- Latest Scores: Materialized view for quick watchlist access
CREATE TABLE finanzioso.latest_scores (
    stock_id BIGINT PRIMARY KEY REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    score_id BIGINT NOT NULL REFERENCES finanzioso.scores(id) ON DELETE CASCADE,
    symbol VARCHAR(10) NOT NULL,
    score NUMERIC(5, 2) NOT NULL,
    action finanzioso.action_type NOT NULL,
    confidence finanzioso.confidence_level NOT NULL,
    safe_mode BOOLEAN NOT NULL DEFAULT FALSE,
    calculated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_latest_scores_action ON finanzioso.latest_scores(action);
CREATE INDEX idx_latest_scores_calculated ON finanzioso.latest_scores(calculated_at DESC);

-- ===================================================================
-- TABLES: News & Filings
-- ===================================================================

-- News Items: Aggregated news articles
CREATE TABLE finanzioso.news_items (
    id BIGSERIAL PRIMARY KEY,
    headline TEXT NOT NULL,
    content TEXT,
    source VARCHAR(255) NOT NULL,
    source_url TEXT,
    url_hash VARCHAR(64) NOT NULL,  -- SHA256 of normalized URL
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sentiment_score NUMERIC(4, 3),  -- -1.000 to +1.000
    flagged BOOLEAN DEFAULT FALSE,  -- Suspicious content detected
    qdrant_point_id UUID,  -- For coordinated deletion

    CONSTRAINT news_items_url_unique UNIQUE (url_hash)
);

CREATE INDEX idx_news_published ON finanzioso.news_items(published_at DESC);
CREATE INDEX idx_news_source ON finanzioso.news_items(source);
CREATE INDEX idx_news_sentiment ON finanzioso.news_items(sentiment_score);
CREATE INDEX idx_news_qdrant ON finanzioso.news_items(qdrant_point_id) WHERE qdrant_point_id IS NOT NULL;

-- News Stocks: Junction table for news-stock relationships
CREATE TABLE finanzioso.news_stocks (
    id BIGSERIAL PRIMARY KEY,
    news_item_id BIGINT NOT NULL REFERENCES finanzioso.news_items(id) ON DELETE CASCADE,
    stock_id BIGINT NOT NULL REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    relevance_score NUMERIC(4, 3),  -- 0.000 to 1.000

    CONSTRAINT news_stocks_unique UNIQUE (news_item_id, stock_id)
);

CREATE INDEX idx_news_stocks_stock ON finanzioso.news_stocks(stock_id);
CREATE INDEX idx_news_stocks_news ON finanzioso.news_stocks(news_item_id);

-- SEC Filings
CREATE TABLE finanzioso.filings (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT NOT NULL REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    form_type VARCHAR(20) NOT NULL,  -- 10-K, 10-Q, 8-K, DEF 14A
    filed_at DATE NOT NULL,
    accession_number VARCHAR(30) NOT NULL,
    filing_url TEXT,
    content TEXT,  -- Extracted text content
    content_hash VARCHAR(64),
    sentiment_score NUMERIC(4, 3),
    flagged BOOLEAN DEFAULT FALSE,
    qdrant_point_id UUID,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT filings_accession_unique UNIQUE (accession_number)
);

CREATE INDEX idx_filings_stock ON finanzioso.filings(stock_id);
CREATE INDEX idx_filings_form ON finanzioso.filings(form_type);
CREATE INDEX idx_filings_filed ON finanzioso.filings(filed_at DESC);
CREATE INDEX idx_filings_stock_form ON finanzioso.filings(stock_id, form_type, filed_at DESC);

-- ===================================================================
-- TABLES: Event Queue
-- ===================================================================

-- Events: Async job queue with claiming
CREATE TABLE finanzioso.events (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,  -- NEW_PRICES, BACKFILL_STOCK, MANUAL_REFRESH, etc.
    payload JSONB NOT NULL DEFAULT '{}',
    status finanzioso.event_status NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    processor_id VARCHAR(100),  -- Worker that claimed this event
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_status ON finanzioso.events(status);
CREATE INDEX idx_events_pending ON finanzioso.events(created_at) WHERE status = 'pending';
CREATE INDEX idx_events_processing ON finanzioso.events(claimed_at) WHERE status = 'processing';
CREATE INDEX idx_events_type ON finanzioso.events(event_type);

-- Outbox: For transactional external actions (Qdrant, etc.)
CREATE TABLE finanzioso.outbox (
    id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,  -- score_embedding, news_embedding, etc.
    entity_id BIGINT NOT NULL,
    action VARCHAR(20) NOT NULL,  -- upsert, delete
    payload JSONB NOT NULL DEFAULT '{}',
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_outbox_unprocessed ON finanzioso.outbox(created_at) WHERE processed = FALSE;
CREATE INDEX idx_outbox_entity ON finanzioso.outbox(entity_type, entity_id);

-- ===================================================================
-- TABLES: Rate Limiting & Security
-- ===================================================================

-- Rate Limits: Token bucket per provider
CREATE TABLE finanzioso.rate_limits (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(50) NOT NULL UNIQUE,
    tokens_per_second NUMERIC(10, 2) NOT NULL,
    bucket_size INTEGER NOT NULL,
    current_tokens NUMERIC(10, 2) NOT NULL,
    last_refill TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    circuit_open BOOLEAN DEFAULT FALSE,
    circuit_failures INTEGER DEFAULT 0,
    circuit_opened_at TIMESTAMPTZ
);

-- Initialize default rate limits
INSERT INTO finanzioso.rate_limits (provider, tokens_per_second, bucket_size, current_tokens)
VALUES
    ('sec', 10.0, 10, 10.0),      -- SEC EDGAR: 10 req/s hard limit
    ('yahoo', 5.0, 10, 10.0)      -- Yahoo: conservative
ON CONFLICT (provider) DO NOTHING;

-- Source Whitelist: Allowed domains for fetching
CREATE TABLE finanzioso.source_whitelist (
    id BIGSERIAL PRIMARY KEY,
    domain VARCHAR(255) NOT NULL UNIQUE,
    source_class finanzioso.source_class NOT NULL,
    allowed_redirects TEXT[],  -- Array of allowed redirect domains
    max_redirects INTEGER DEFAULT 3,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Initialize default whitelist
INSERT INTO finanzioso.source_whitelist (domain, source_class, allowed_redirects)
VALUES
    ('sec.gov', 'A', ARRAY['www.sec.gov', 'data.sec.gov', 'efts.sec.gov']),
    ('data.sec.gov', 'A', ARRAY['www.sec.gov', 'sec.gov']),
    ('finance.yahoo.com', 'B', ARRAY['query1.finance.yahoo.com', 'query2.finance.yahoo.com']),
    ('query1.finance.yahoo.com', 'B', NULL),
    ('query2.finance.yahoo.com', 'B', NULL)
ON CONFLICT (domain) DO NOTHING;

-- Stock Sources: Per-stock IR/news sources
CREATE TABLE finanzioso.stock_sources (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT NOT NULL REFERENCES finanzioso.stocks(id) ON DELETE CASCADE,
    domain VARCHAR(255) NOT NULL,
    source_class finanzioso.source_class NOT NULL,
    source_type VARCHAR(20) NOT NULL,  -- IR, NEWS, BLOG
    url_template TEXT,
    active BOOLEAN DEFAULT TRUE,
    last_crawled TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT stock_sources_unique UNIQUE (stock_id, domain, source_type)
);

CREATE INDEX idx_stock_sources_stock ON finanzioso.stock_sources(stock_id);
CREATE INDEX idx_stock_sources_domain ON finanzioso.stock_sources(domain);

-- ===================================================================
-- TABLES: Operations
-- ===================================================================

-- Retention Config
CREATE TABLE finanzioso.retention_config (
    id BIGSERIAL PRIMARY KEY,
    table_name VARCHAR(100) NOT NULL UNIQUE,
    retention_days INTEGER NOT NULL,
    qdrant_collection VARCHAR(100),
    last_cleanup TIMESTAMPTZ,
    rows_deleted BIGINT DEFAULT 0
);

-- Initialize default retention
INSERT INTO finanzioso.retention_config (table_name, retention_days, qdrant_collection)
VALUES
    ('prices_daily', 1825, NULL),           -- 5 years
    ('scores', 365, NULL),                  -- 1 year
    ('signals', 365, NULL),                 -- 1 year
    ('news_items', 180, 'finanzioso_news'), -- 6 months
    ('filings', 730, 'finanzioso_filings'), -- 2 years
    ('events', 7, NULL)                     -- 7 days (done/failed only)
ON CONFLICT (table_name) DO NOTHING;

-- ===================================================================
-- FUNCTIONS
-- ===================================================================

-- Normalize URL for deduplication
CREATE OR REPLACE FUNCTION finanzioso.normalize_url(url TEXT)
RETURNS TEXT AS $$
DECLARE
    normalized TEXT;
BEGIN
    -- Remove trailing slashes, www prefix, protocol
    normalized := lower(url);
    normalized := regexp_replace(normalized, '^https?://', '');
    normalized := regexp_replace(normalized, '^www\.', '');
    normalized := regexp_replace(normalized, '/+$', '');
    normalized := regexp_replace(normalized, '#.*$', '');  -- Remove fragment
    -- Sort query params for consistency
    RETURN normalized;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Calculate trading days between dates
CREATE OR REPLACE FUNCTION finanzioso.trading_days_since(
    target_date DATE,
    exchange_name VARCHAR DEFAULT 'NYSE'
)
RETURNS INTEGER AS $$
BEGIN
    RETURN (
        SELECT COUNT(*)::INTEGER
        FROM finanzioso.trading_calendar
        WHERE exchange = exchange_name
          AND is_trading_day = TRUE
          AND date > target_date
          AND date <= CURRENT_DATE
    );
END;
$$ LANGUAGE plpgsql STABLE;

-- Rate limit acquire token (atomic)
CREATE OR REPLACE FUNCTION finanzioso.rate_limit_acquire(
    provider_name VARCHAR,
    tokens_needed INTEGER DEFAULT 1
)
RETURNS BOOLEAN AS $$
DECLARE
    rl RECORD;
    elapsed_seconds NUMERIC;
    new_tokens NUMERIC;
    acquired BOOLEAN := FALSE;
BEGIN
    -- Lock the rate limit row
    SELECT * INTO rl
    FROM finanzioso.rate_limits
    WHERE provider = provider_name
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- Check circuit breaker
    IF rl.circuit_open THEN
        -- Check if circuit should close (5 minutes)
        IF rl.circuit_opened_at < NOW() - INTERVAL '5 minutes' THEN
            UPDATE finanzioso.rate_limits
            SET circuit_open = FALSE, circuit_failures = 0
            WHERE provider = provider_name;
        ELSE
            RETURN FALSE;
        END IF;
    END IF;

    -- Refill tokens based on elapsed time
    elapsed_seconds := EXTRACT(EPOCH FROM (NOW() - rl.last_refill));
    new_tokens := LEAST(
        rl.bucket_size,
        rl.current_tokens + (elapsed_seconds * rl.tokens_per_second)
    );

    -- Try to acquire tokens
    IF new_tokens >= tokens_needed THEN
        UPDATE finanzioso.rate_limits
        SET current_tokens = new_tokens - tokens_needed,
            last_refill = NOW()
        WHERE provider = provider_name;
        acquired := TRUE;
    ELSE
        -- Update refill time but don't consume
        UPDATE finanzioso.rate_limits
        SET current_tokens = new_tokens,
            last_refill = NOW()
        WHERE provider = provider_name;
    END IF;

    RETURN acquired;
END;
$$ LANGUAGE plpgsql;

-- Record rate limit failure (for circuit breaker)
CREATE OR REPLACE FUNCTION finanzioso.rate_limit_failure(provider_name VARCHAR)
RETURNS VOID AS $$
BEGIN
    UPDATE finanzioso.rate_limits
    SET circuit_failures = circuit_failures + 1,
        circuit_open = CASE WHEN circuit_failures >= 4 THEN TRUE ELSE circuit_open END,
        circuit_opened_at = CASE WHEN circuit_failures >= 4 THEN NOW() ELSE circuit_opened_at END
    WHERE provider = provider_name;
END;
$$ LANGUAGE plpgsql;

-- Record rate limit success (reset failures)
CREATE OR REPLACE FUNCTION finanzioso.rate_limit_success(provider_name VARCHAR)
RETURNS VOID AS $$
BEGIN
    UPDATE finanzioso.rate_limits
    SET circuit_failures = 0
    WHERE provider = provider_name;
END;
$$ LANGUAGE plpgsql;

-- Claim events for processing (SKIP LOCKED pattern)
CREATE OR REPLACE FUNCTION finanzioso.claim_events(
    processor VARCHAR,
    event_types VARCHAR[],
    batch_size INTEGER DEFAULT 10
)
RETURNS SETOF finanzioso.events AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        SELECT e.id
        FROM finanzioso.events e
        WHERE e.status = 'pending'
          AND e.event_type = ANY(event_types)
          AND e.attempts < e.max_attempts
        ORDER BY e.created_at
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE finanzioso.events e
    SET status = 'processing',
        processor_id = processor,
        claimed_at = NOW(),
        attempts = attempts + 1
    FROM claimed
    WHERE e.id = claimed.id
    RETURNING e.*;
END;
$$ LANGUAGE plpgsql;

-- Complete event (success)
CREATE OR REPLACE FUNCTION finanzioso.complete_event(event_id BIGINT)
RETURNS VOID AS $$
BEGIN
    UPDATE finanzioso.events
    SET status = 'done',
        completed_at = NOW()
    WHERE id = event_id;
END;
$$ LANGUAGE plpgsql;

-- Fail event
CREATE OR REPLACE FUNCTION finanzioso.fail_event(event_id BIGINT, error_msg TEXT)
RETURNS VOID AS $$
DECLARE
    evt RECORD;
BEGIN
    SELECT * INTO evt FROM finanzioso.events WHERE id = event_id;

    IF evt.attempts >= evt.max_attempts THEN
        UPDATE finanzioso.events
        SET status = 'failed',
            completed_at = NOW(),
            error_message = error_msg
        WHERE id = event_id;
    ELSE
        -- Reset to pending for retry
        UPDATE finanzioso.events
        SET status = 'pending',
            processor_id = NULL,
            claimed_at = NULL,
            error_message = error_msg
        WHERE id = event_id;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- UPSERT latest score (called after each score insert)
CREATE OR REPLACE FUNCTION finanzioso.upsert_latest_score()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO finanzioso.latest_scores (stock_id, score_id, symbol, score, action, confidence, safe_mode, calculated_at)
    SELECT
        NEW.stock_id,
        NEW.id,
        s.symbol,
        NEW.score,
        NEW.action,
        NEW.confidence,
        NEW.safe_mode,
        NEW.calculated_at
    FROM finanzioso.stocks s
    WHERE s.id = NEW.stock_id
    ON CONFLICT (stock_id) DO UPDATE SET
        score_id = EXCLUDED.score_id,
        symbol = EXCLUDED.symbol,
        score = EXCLUDED.score,
        action = EXCLUDED.action,
        confidence = EXCLUDED.confidence,
        safe_mode = EXCLUDED.safe_mode,
        calculated_at = EXCLUDED.calculated_at;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for latest_scores upsert
CREATE TRIGGER trigger_upsert_latest_score
AFTER INSERT ON finanzioso.scores
FOR EACH ROW
EXECUTE FUNCTION finanzioso.upsert_latest_score();

-- Update stocks.updated_at on change
CREATE OR REPLACE FUNCTION finanzioso.update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_stocks_updated
BEFORE UPDATE ON finanzioso.stocks
FOR EACH ROW
EXECUTE FUNCTION finanzioso.update_timestamp();

-- ===================================================================
-- COMMENTS (for documentation)
-- ===================================================================

COMMENT ON SCHEMA finanzioso IS 'AI Stock Assistant - Educational/Research only, NOT investment advice';
COMMENT ON TABLE finanzioso.stocks IS 'User watchlist entries';
COMMENT ON TABLE finanzioso.scores IS 'Calculated stock scores with action recommendations';
COMMENT ON TABLE finanzioso.signals IS 'Evidence supporting each score calculation';
COMMENT ON TABLE finanzioso.events IS 'Async job queue with claiming and retry logic';
COMMENT ON TABLE finanzioso.rate_limits IS 'Token bucket rate limiting per provider';
COMMENT ON FUNCTION finanzioso.rate_limit_acquire IS 'Atomically acquire rate limit tokens, respects circuit breaker';
COMMENT ON FUNCTION finanzioso.claim_events IS 'Claim pending events for processing with SKIP LOCKED';
