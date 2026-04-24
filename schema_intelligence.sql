-- ============================================================
-- Intelligence Layer: Generische Symbol-Enrichment-Tabellen
-- Migration: schema_intelligence.sql
-- Datum: 2026-03-20
-- ============================================================

-- symbol_cards: Enriched metadata per symbol.
-- Felder wie layer, side_effect_type, affected_resource sind FREITEXT,
-- nicht ENUM — Domain Profiles definieren die Taxonomie.
CREATE TABLE IF NOT EXISTS symbol_cards (
    symbol_id BIGINT PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,

    -- Classification (domain-defined taxonomy, not hardcoded)
    layer TEXT,
    side_effect_type TEXT,
    affected_resource TEXT,

    -- Delegation (multi-candidate, V1 als JSONB)
    delegation_targets JSONB DEFAULT '[]',

    -- Context
    thread_requirement TEXT,
    preconditions TEXT,

    -- Confidence
    classification_confidence REAL DEFAULT 1.0,

    -- Summary (kompakte Hilfserklaerung, keine autoritative Interpretation)
    summary TEXT,

    -- Provenance
    domain_profile TEXT,
    enrichment_version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sc_layer ON symbol_cards(layer);
CREATE INDEX IF NOT EXISTS idx_sc_side_effect ON symbol_cards(side_effect_type);
CREATE INDEX IF NOT EXISTS idx_sc_resource ON symbol_cards(affected_resource);
CREATE INDEX IF NOT EXISTS idx_sc_domain ON symbol_cards(domain_profile);
CREATE INDEX IF NOT EXISTS idx_sc_delegation ON symbol_cards USING GIN (delegation_targets);
CREATE INDEX IF NOT EXISTS idx_sc_confidence ON symbol_cards(classification_confidence);

-- symbol_annotations: Extracted code-level facts (thread assertions, dispatch patterns, etc.)
CREATE TABLE IF NOT EXISTS symbol_annotations (
    id BIGSERIAL PRIMARY KEY,
    symbol_id BIGINT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    annotation_type TEXT NOT NULL,
    value TEXT NOT NULL,
    evidence_line INT,
    confidence REAL DEFAULT 1.0,
    domain_profile TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol_id, annotation_type, value)
);

CREATE INDEX IF NOT EXISTS idx_sa_symbol ON symbol_annotations(symbol_id);
CREATE INDEX IF NOT EXISTS idx_sa_type ON symbol_annotations(annotation_type);
CREATE INDEX IF NOT EXISTS idx_sa_domain ON symbol_annotations(domain_profile);

-- negative_evidence: Known dead-end paths (domain-scoped)
CREATE TABLE IF NOT EXISTS negative_evidence (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT REFERENCES sources(id) ON DELETE CASCADE,
    domain_profile TEXT,
    concept TEXT NOT NULL,
    path_description TEXT NOT NULL,
    reason TEXT NOT NULL,
    symbols JSONB DEFAULT '[]',
    severity TEXT NOT NULL DEFAULT 'warning',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ne_concept ON negative_evidence USING GIN (to_tsvector('simple', concept));
CREATE INDEX IF NOT EXISTS idx_ne_symbols ON negative_evidence USING GIN (symbols);
CREATE INDEX IF NOT EXISTS idx_ne_domain ON negative_evidence(domain_profile);
CREATE INDEX IF NOT EXISTS idx_ne_source ON negative_evidence(source_id);
