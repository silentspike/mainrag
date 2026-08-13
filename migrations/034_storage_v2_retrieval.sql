-- Migration 034: exact, occurrence-scoped storage-v2 retrieval
--
-- This schema is additive. The current search path remains the default and no
-- generation pointer is changed here. Native PostgreSQL GIN is the initial
-- backend selected by the frozen #56 prototype. Queries use complete scoped
-- evaluation whenever no safe bound covers every later score contribution.

CREATE TABLE IF NOT EXISTS storage_v2_search_document (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id TEXT NOT NULL CHECK (profile_id <> ''),
    component_kind TEXT NOT NULL CHECK (component_kind IN ('body', 'node')),
    body_id BIGINT REFERENCES content_body(id) ON DELETE RESTRICT,
    node_id BIGINT REFERENCES content_node(id) ON DELETE RESTRICT,
    search_text TEXT NOT NULL,
    token_count BIGINT NOT NULL CHECK (token_count > 0),
    exact_identifiers TEXT[] NOT NULL,
    materialization_sha256 BYTEA NOT NULL CHECK (octet_length(materialization_sha256) = 32),
    fts_simple TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((body_id IS NOT NULL)::INTEGER + (node_id IS NOT NULL)::INTEGER = 1),
    CHECK ((component_kind = 'body') = (body_id IS NOT NULL)),
    CHECK ((component_kind = 'node') = (node_id IS NOT NULL)),
    CONSTRAINT uq_storage_v2_search_document_component
        UNIQUE NULLS NOT DISTINCT (profile_id, component_kind, body_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_search_document_fts
    ON storage_v2_search_document USING GIN (fts_simple);
CREATE INDEX IF NOT EXISTS idx_storage_v2_search_document_exact
    ON storage_v2_search_document USING GIN (exact_identifiers);

CREATE TABLE IF NOT EXISTS storage_v2_search_posting (
    document_id BIGINT NOT NULL REFERENCES storage_v2_search_document(id) ON DELETE RESTRICT,
    term TEXT NOT NULL CHECK (term <> ''),
    term_frequency BIGINT NOT NULL CHECK (term_frequency > 0),
    PRIMARY KEY (document_id, term)
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_search_posting_term
    ON storage_v2_search_posting (term, document_id);

CREATE TABLE IF NOT EXISTS storage_v2_search_view_document (
    view_id BIGINT NOT NULL REFERENCES retrieval_view(id) ON DELETE RESTRICT,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    document_id BIGINT NOT NULL REFERENCES storage_v2_search_document(id) ON DELETE RESTRICT,
    role_weight DOUBLE PRECISION NOT NULL CHECK (
        role_weight > 0
        AND role_weight NOT IN ('Infinity'::DOUBLE PRECISION,
                                '-Infinity'::DOUBLE PRECISION,
                                'NaN'::DOUBLE PRECISION)
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (view_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_search_view_document_document
    ON storage_v2_search_view_document (document_id, view_id);

CREATE TABLE IF NOT EXISTS storage_v2_occurrence_score_component (
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK (stage IN ('graph', 'semantic', 'rerank')),
    profile_id TEXT NOT NULL CHECK (profile_id <> ''),
    status TEXT NOT NULL CHECK (status IN ('available', 'unavailable', 'failed')),
    score DOUBLE PRECISION,
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (occurrence_id, stage, profile_id),
    CHECK ((status = 'available') = (score IS NOT NULL)),
    CHECK (score IS NULL OR score NOT IN (
        'Infinity'::DOUBLE PRECISION,
        '-Infinity'::DOUBLE PRECISION,
        'NaN'::DOUBLE PRECISION
    ))
);

CREATE OR REPLACE FUNCTION storage_v2_reject_retrieval_mutation() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION 'storage-v2 retrieval projections are immutable';
END
$$;

DROP TRIGGER IF EXISTS storage_v2_search_document_immutable ON storage_v2_search_document;
CREATE TRIGGER storage_v2_search_document_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_search_document
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
DROP TRIGGER IF EXISTS storage_v2_search_posting_immutable ON storage_v2_search_posting;
CREATE TRIGGER storage_v2_search_posting_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_search_posting
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
DROP TRIGGER IF EXISTS storage_v2_search_view_document_immutable ON storage_v2_search_view_document;
CREATE TRIGGER storage_v2_search_view_document_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_search_view_document
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
DROP TRIGGER IF EXISTS storage_v2_occurrence_score_component_immutable
    ON storage_v2_occurrence_score_component;
CREATE TRIGGER storage_v2_occurrence_score_component_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_occurrence_score_component
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

CREATE OR REPLACE FUNCTION storage_v2_put_search_document(
    p_profile_id TEXT,
    p_component_kind TEXT,
    p_component_id BIGINT,
    p_search_text TEXT,
    p_exact_identifiers TEXT[] DEFAULT ARRAY[]::TEXT[]
) RETURNS storage_v2_search_document
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_component_digest BYTEA;
    v_exact TEXT[];
    v_token_count BIGINT;
    v_hash BYTEA;
    v_document storage_v2_search_document;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'search-document writes require administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_profile_id IS NULL OR p_profile_id = ''
       OR p_component_kind NOT IN ('body', 'node')
       OR p_component_id IS NULL OR p_search_text IS NULL OR btrim(p_search_text) = ''
       OR p_exact_identifiers IS NULL THEN
        RAISE EXCEPTION 'valid search-document materialization required';
    END IF;
    IF p_component_kind = 'body' THEN
        SELECT digest INTO v_component_digest FROM content_body WHERE id = p_component_id;
    ELSE
        SELECT node_digest INTO v_component_digest FROM content_node WHERE id = p_component_id;
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'search-document component not found'; END IF;

    SELECT COALESCE(array_agg(value ORDER BY value), ARRAY[]::TEXT[])
      INTO v_exact
      FROM (
          SELECT DISTINCT lower(btrim(identifier)) AS value
            FROM unnest(p_exact_identifiers) AS identifier
           WHERE btrim(identifier) <> ''
      ) normalized;
    SELECT COUNT(*) INTO v_token_count
      FROM regexp_split_to_table(lower(p_search_text), '[^[:alnum:]_]+') AS token
     WHERE token <> '';
    IF v_token_count = 0 THEN RAISE EXCEPTION 'search document has no searchable tokens'; END IF;
    v_hash := storage_v2_hash_parts('mainrag.search-document.v1', ARRAY[
        convert_to(p_profile_id, 'UTF8'), convert_to(p_component_kind, 'UTF8'),
        v_component_digest, convert_to(p_search_text, 'UTF8'),
        convert_to(array_to_string(v_exact, E'\n'), 'UTF8')
    ]);

    INSERT INTO storage_v2_search_document(
        profile_id, component_kind, body_id, node_id, search_text, token_count,
        exact_identifiers, materialization_sha256
    ) VALUES (
        p_profile_id, p_component_kind,
        CASE WHEN p_component_kind = 'body' THEN p_component_id END,
        CASE WHEN p_component_kind = 'node' THEN p_component_id END,
        p_search_text, v_token_count, v_exact, v_hash
    ) ON CONFLICT ON CONSTRAINT uq_storage_v2_search_document_component DO NOTHING
    RETURNING * INTO v_document;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_document FROM storage_v2_search_document
         WHERE profile_id = p_profile_id AND component_kind = p_component_kind
           AND body_id IS NOT DISTINCT FROM
               CASE WHEN p_component_kind = 'body' THEN p_component_id END
           AND node_id IS NOT DISTINCT FROM
               CASE WHEN p_component_kind = 'node' THEN p_component_id END;
        IF v_document.materialization_sha256 <> v_hash THEN
            RAISE EXCEPTION 'search-document profile collision' USING ERRCODE = '22000';
        END IF;
        RETURN v_document;
    END IF;

    INSERT INTO storage_v2_search_posting(document_id, term, term_frequency)
    SELECT v_document.id, token, COUNT(*)
      FROM (
          SELECT token
            FROM regexp_split_to_table(lower(p_search_text), '[^[:alnum:]_]+') AS token
           WHERE token <> ''
          UNION ALL
          SELECT token
            FROM regexp_split_to_table(lower(p_search_text), '[[:space:]]+') AS token
           WHERE token <> '' AND token !~ '^[[:alnum:]_]+$'
      ) searchable_tokens
     GROUP BY token;
    RETURN v_document;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_bind_search_document(
    p_view_id BIGINT,
    p_ordinal BIGINT,
    p_document_id BIGINT,
    p_role_weight DOUBLE PRECISION
) RETURNS storage_v2_search_view_document
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_binding storage_v2_search_view_document;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'search-view writes require administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_role_weight IS NULL OR p_role_weight <= 0
       OR p_role_weight IN ('Infinity'::DOUBLE PRECISION,
                            '-Infinity'::DOUBLE PRECISION,
                            'NaN'::DOUBLE PRECISION)
       OR NOT EXISTS (
          SELECT 1 FROM view_component component_row
          JOIN storage_v2_search_document document
            ON document.id = p_document_id
           AND document.component_kind = component_row.component_kind
           AND document.body_id IS NOT DISTINCT FROM component_row.body_id
           AND document.node_id IS NOT DISTINCT FROM component_row.node_id
         WHERE component_row.view_id = p_view_id AND component_row.ordinal = p_ordinal
       ) THEN
        RAISE EXCEPTION 'search document must match the ordered retrieval-view component';
    END IF;
    INSERT INTO storage_v2_search_view_document(view_id, ordinal, document_id, role_weight)
    VALUES (p_view_id, p_ordinal, p_document_id, p_role_weight)
    ON CONFLICT (view_id, ordinal) DO NOTHING
    RETURNING * INTO v_binding;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_binding FROM storage_v2_search_view_document
         WHERE view_id = p_view_id AND ordinal = p_ordinal;
        IF (v_binding.document_id, v_binding.role_weight)
           IS DISTINCT FROM (p_document_id, p_role_weight) THEN
            RAISE EXCEPTION 'search-view binding collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_binding;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_occurrence_score_component(
    p_occurrence_id BIGINT,
    p_stage TEXT,
    p_profile_id TEXT,
    p_status TEXT,
    p_score DOUBLE PRECISION,
    p_detail JSONB DEFAULT '{}'::JSONB
) RETURNS storage_v2_occurrence_score_component
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_component storage_v2_occurrence_score_component;
BEGIN
    SELECT source_id INTO v_source_id FROM occurrence WHERE id = p_occurrence_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write')
       OR p_stage NOT IN ('graph', 'semantic', 'rerank')
       OR p_profile_id IS NULL OR p_profile_id = ''
       OR p_status NOT IN ('available', 'unavailable', 'failed')
       OR ((p_status = 'available') IS DISTINCT FROM (p_score IS NOT NULL))
       OR (p_score IS NOT NULL AND p_score IN (
              'Infinity'::DOUBLE PRECISION,
              '-Infinity'::DOUBLE PRECISION,
              'NaN'::DOUBLE PRECISION
          ))
       OR p_detail IS NULL THEN
        RAISE EXCEPTION 'valid authorized occurrence score component required'
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO storage_v2_occurrence_score_component(
        occurrence_id, stage, profile_id, status, score, detail
    ) VALUES (p_occurrence_id, p_stage, p_profile_id, p_status, p_score, p_detail)
    ON CONFLICT (occurrence_id, stage, profile_id) DO NOTHING
    RETURNING * INTO v_component;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_component FROM storage_v2_occurrence_score_component
         WHERE occurrence_id = p_occurrence_id AND stage = p_stage
           AND profile_id = p_profile_id;
        IF (v_component.status, v_component.score, v_component.detail)
           IS DISTINCT FROM (p_status, p_score, p_detail) THEN
            RAISE EXCEPTION 'occurrence score component collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_component;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_search_ast_matches(
    p_node JSONB,
    p_matched_terms TEXT[],
    p_matched_phrases TEXT[],
    p_matched_exact TEXT[]
) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_child JSONB;
    v_children JSONB;
BEGIN
    IF jsonb_typeof(p_node) <> 'object' THEN RAISE EXCEPTION 'invalid search AST node'; END IF;
    CASE p_node ->> 'type'
        WHEN 'term' THEN RETURN lower(p_node ->> 'value') = ANY(p_matched_terms);
        WHEN 'phrase' THEN RETURN lower(p_node ->> 'value') = ANY(p_matched_phrases);
        WHEN 'exact' THEN RETURN lower(p_node ->> 'value') = ANY(p_matched_exact);
        WHEN 'not' THEN
            v_children := p_node -> 'children';
            IF jsonb_typeof(v_children) <> 'array' OR jsonb_array_length(v_children) <> 1 THEN
                RAISE EXCEPTION 'NOT requires one child';
            END IF;
            RETURN NOT storage_v2_search_ast_matches(
                v_children -> 0, p_matched_terms, p_matched_phrases, p_matched_exact
            );
        WHEN 'group' THEN
            v_children := p_node -> 'children';
            IF jsonb_typeof(v_children) <> 'array' OR jsonb_array_length(v_children) <> 1 THEN
                RAISE EXCEPTION 'group requires one child';
            END IF;
            RETURN storage_v2_search_ast_matches(
                v_children -> 0, p_matched_terms, p_matched_phrases, p_matched_exact
            );
        WHEN 'and' THEN
            v_children := p_node -> 'children';
            IF jsonb_typeof(v_children) <> 'array' OR jsonb_array_length(v_children) < 2 THEN
                RAISE EXCEPTION 'AND requires at least two children';
            END IF;
            FOR v_child IN SELECT value FROM jsonb_array_elements(v_children) LOOP
                IF NOT storage_v2_search_ast_matches(
                    v_child, p_matched_terms, p_matched_phrases, p_matched_exact
                ) THEN RETURN FALSE; END IF;
            END LOOP;
            RETURN TRUE;
        WHEN 'or' THEN
            v_children := p_node -> 'children';
            IF jsonb_typeof(v_children) <> 'array' OR jsonb_array_length(v_children) < 2 THEN
                RAISE EXCEPTION 'OR requires at least two children';
            END IF;
            FOR v_child IN SELECT value FROM jsonb_array_elements(v_children) LOOP
                IF storage_v2_search_ast_matches(
                    v_child, p_matched_terms, p_matched_phrases, p_matched_exact
                ) THEN RETURN TRUE; END IF;
            END LOOP;
            RETURN FALSE;
        ELSE RAISE EXCEPTION 'unsupported search AST node type';
    END CASE;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_search_ast_has_anchor(p_node JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_child JSONB;
    v_children JSONB;
    v_has_anchor BOOLEAN;
BEGIN
    IF jsonb_typeof(p_node) <> 'object' THEN RETURN FALSE; END IF;
    CASE p_node ->> 'type'
        WHEN 'term', 'phrase', 'exact' THEN
            RETURN COALESCE(p_node ->> 'value', '') <> '';
        WHEN 'not' THEN RETURN FALSE;
        WHEN 'group' THEN
            v_children := p_node -> 'children';
            RETURN jsonb_typeof(v_children) = 'array'
               AND jsonb_array_length(v_children) = 1
               AND storage_v2_search_ast_has_anchor(v_children -> 0);
        WHEN 'and' THEN
            v_children := p_node -> 'children';
            IF jsonb_typeof(v_children) <> 'array' OR jsonb_array_length(v_children) < 2 THEN
                RETURN FALSE;
            END IF;
            v_has_anchor := FALSE;
            FOR v_child IN SELECT value FROM jsonb_array_elements(v_children) LOOP
                v_has_anchor := v_has_anchor OR storage_v2_search_ast_has_anchor(v_child);
            END LOOP;
            RETURN v_has_anchor;
        WHEN 'or' THEN
            v_children := p_node -> 'children';
            IF jsonb_typeof(v_children) <> 'array' OR jsonb_array_length(v_children) < 2 THEN
                RETURN FALSE;
            END IF;
            FOR v_child IN SELECT value FROM jsonb_array_elements(v_children) LOOP
                IF NOT storage_v2_search_ast_has_anchor(v_child) THEN RETURN FALSE; END IF;
            END LOOP;
            RETURN TRUE;
        ELSE RETURN FALSE;
    END CASE;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_search_ast_is_valid(p_node JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_child JSONB;
    v_children JSONB;
BEGIN
    IF jsonb_typeof(p_node) <> 'object' THEN RETURN FALSE; END IF;
    CASE p_node ->> 'type'
        WHEN 'term', 'phrase', 'exact' THEN
            RETURN jsonb_typeof(p_node -> 'value') = 'string'
               AND btrim(p_node ->> 'value') <> ''
               AND NOT p_node ? 'children';
        WHEN 'not', 'group' THEN
            v_children := p_node -> 'children';
            RETURN NOT p_node ? 'value'
               AND jsonb_typeof(v_children) = 'array'
               AND jsonb_array_length(v_children) = 1
               AND storage_v2_search_ast_is_valid(v_children -> 0);
        WHEN 'and', 'or' THEN
            v_children := p_node -> 'children';
            IF p_node ? 'value' OR jsonb_typeof(v_children) <> 'array'
               OR jsonb_array_length(v_children) < 2 THEN
                RETURN FALSE;
            END IF;
            FOR v_child IN SELECT value FROM jsonb_array_elements(v_children) LOOP
                IF NOT storage_v2_search_ast_is_valid(v_child) THEN RETURN FALSE; END IF;
            END LOOP;
            RETURN TRUE;
        ELSE RETURN FALSE;
    END CASE;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_search_exact(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_ast JSONB,
    p_filters JSONB DEFAULT '{}'::JSONB,
    p_limit BIGINT DEFAULT 20
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_result JSONB;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'read') THEN
        RAISE EXCEPTION 'authorized generation selector required' USING ERRCODE = '42501';
    END IF;
    IF p_ast IS NULL OR NOT storage_v2_search_ast_is_valid(p_ast)
       OR NOT storage_v2_search_ast_has_anchor(p_ast)
       OR p_filters IS NULL OR jsonb_typeof(p_filters) <> 'object'
       OR EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_filters) AS filter_key(value)
            WHERE filter_key.value NOT IN (
                'path_prefix', 'role', 'occurred_from', 'occurred_to',
                'graph_profile', 'semantic_profile', 'rerank_profile'
            )
       )
       OR EXISTS (
           SELECT 1 FROM jsonb_each(p_filters) AS entry(key, value)
            WHERE jsonb_typeof(entry.value) <> 'string'
               OR btrim(entry.value #>> '{}') = ''
       )
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'valid exact retrieval request required';
    END IF;
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);

    WITH RECURSIVE
    ast_nodes(node, negated) AS (
        SELECT p_ast, FALSE
        UNION ALL
        SELECT child.value,
               parent.negated <> (parent.node ->> 'type' = 'not')
          FROM ast_nodes parent
          CROSS JOIN LATERAL jsonb_array_elements(
              CASE WHEN jsonb_typeof(parent.node -> 'children') = 'array'
                   THEN parent.node -> 'children' ELSE '[]'::JSONB END
          ) child
    ),
    leaves AS (
        SELECT node ->> 'type' AS kind, lower(node ->> 'value') AS value, negated
          FROM ast_nodes
         WHERE node ->> 'type' IN ('term', 'phrase', 'exact')
    ),
    query_values AS (
        SELECT
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term'), ARRAY[]::TEXT[]) AS terms,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::TEXT[]) AS score_terms,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'phrase'), ARRAY[]::TEXT[]) AS phrases,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'exact'), ARRAY[]::TEXT[]) AS exact_values
          FROM leaves
    ),
    visible_occurrence AS (
        SELECT occurrence_row.*, source.name AS source_name, item.item_key,
               artifact.expected_content_hash, view_row.view_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(occurrence_row.source_id),
                        convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'),
                        view_row.view_digest,
                        convert_to(occurrence_row.role, 'UTF8'),
                        int8send(occurrence_row.ordinal),
                        convert_to(occurrence_row.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM occurrence occurrence_row
          JOIN retrieval_view view_row ON view_row.id = occurrence_row.view_id
          JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN sources source ON source.id = occurrence_row.source_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
         WHERE occurrence_row.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq)
           AND (COALESCE(p_filters ->> 'path_prefix', '') = ''
                OR left(occurrence_row.source_path, char_length(p_filters ->> 'path_prefix'))
                   = p_filters ->> 'path_prefix')
           AND (COALESCE(p_filters ->> 'role', '') = ''
                OR occurrence_row.role = p_filters ->> 'role')
           AND (COALESCE(p_filters ->> 'occurred_from', '') = ''
                OR occurrence_row.occurred_at >= (p_filters ->> 'occurred_from')::TIMESTAMPTZ)
           AND (COALESCE(p_filters ->> 'occurred_to', '') = ''
                OR occurrence_row.occurred_at < (p_filters ->> 'occurred_to')::TIMESTAMPTZ)
    ),
    scoped_binding AS (
        SELECT visible.*, visible.id AS occurrence_id,
               binding.ordinal AS component_ordinal, binding.document_id,
               binding.role_weight, document.search_text, document.token_count,
               document.exact_identifiers, document.fts_simple
          FROM visible_occurrence visible
          JOIN storage_v2_search_view_document binding ON binding.view_id = visible.view_id
          JOIN storage_v2_search_document document ON document.id = binding.document_id
    ),
    view_stats AS (
        SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length,
               string_agg(search_text, E'\n' ORDER BY component_ordinal) AS content
          FROM scoped_binding GROUP BY occurrence_id
    ),
    corpus_stats AS (
        SELECT COUNT(DISTINCT occurrence_id)::DOUBLE PRECISION AS view_count,
               AVG(view_length) AS average_view_length FROM view_stats
    ),
    document_frequency AS (
        SELECT posting.term, COUNT(DISTINCT binding.occurrence_id)::DOUBLE PRECISION AS frequency
          FROM scoped_binding binding
          JOIN storage_v2_search_posting posting ON posting.document_id = binding.document_id
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.terms)
         GROUP BY posting.term
    ),
    term_rows AS (
        SELECT binding.occurrence_id, posting.term, binding.component_ordinal,
               binding.role_weight,
               binding.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_binding binding
          JOIN view_stats ON view_stats.occurrence_id = binding.occurrence_id
          JOIN storage_v2_search_posting posting ON posting.document_id = binding.document_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
    ),
    term_match_aggregate AS (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT posting.term ORDER BY posting.term) AS matched_terms
          FROM scoped_binding binding
          JOIN storage_v2_search_posting posting ON posting.document_id = binding.document_id
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.terms)
         GROUP BY binding.occurrence_id
    ),
    best_term AS (
        SELECT DISTINCT ON (occurrence_id, term)
               occurrence_id, term, component_ordinal, role_weight, contribution
          FROM term_rows
         ORDER BY occurrence_id, term, contribution DESC, component_ordinal
    ),
    term_aggregate AS (
        SELECT occurrence_id,
               array_agg(term ORDER BY term) AS matched_terms,
               SUM(contribution) AS lexical_terms,
               jsonb_agg(jsonb_build_object(
                   'term', term, 'component_ordinal', component_ordinal,
                   'role_weight', role_weight, 'score', contribution
               ) ORDER BY term) AS detail
          FROM best_term GROUP BY occurrence_id
    ),
    phrase_aggregate AS (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT phrase.value ORDER BY phrase.value) AS matched_phrases
          FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.phrases) AS phrase(value)
         WHERE binding.fts_simple @@ phraseto_tsquery('simple', phrase.value)
         GROUP BY binding.occurrence_id
    ),
    exact_aggregate AS (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT exact.value ORDER BY exact.value) AS matched_exact
          FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.exact_values) AS exact(value)
         WHERE exact.value = ANY(binding.exact_identifiers)
         GROUP BY binding.occurrence_id
    ),
    matched AS (
        SELECT visible.*, view_stats.content, view_stats.view_length,
               COALESCE(term_match_aggregate.matched_terms, ARRAY[]::TEXT[]) AS matched_terms,
               COALESCE(phrase_aggregate.matched_phrases, ARRAY[]::TEXT[]) AS matched_phrases,
               COALESCE(exact_aggregate.matched_exact, ARRAY[]::TEXT[]) AS matched_exact,
               COALESCE(term_aggregate.lexical_terms, 0.0)
                 + 1.5 * cardinality(COALESCE(phrase_aggregate.matched_phrases, ARRAY[]::TEXT[]))
                 + 2.0 * cardinality(COALESCE(exact_aggregate.matched_exact, ARRAY[]::TEXT[]))
                 AS lexical_score,
               COALESCE(term_aggregate.detail, '[]'::JSONB) AS term_detail
          FROM visible_occurrence visible
          JOIN view_stats ON view_stats.occurrence_id = visible.id
          LEFT JOIN term_match_aggregate ON term_match_aggregate.occurrence_id = visible.id
          LEFT JOIN term_aggregate ON term_aggregate.occurrence_id = visible.id
          LEFT JOIN phrase_aggregate ON phrase_aggregate.occurrence_id = visible.id
          LEFT JOIN exact_aggregate ON exact_aggregate.occurrence_id = visible.id
    ),
    boolean_matched AS (
        SELECT * FROM matched
         WHERE storage_v2_search_ast_matches(
             p_ast, matched_terms, matched_phrases, matched_exact
         )
    ),
    staged AS (
        SELECT matched.*,
               graph.status AS graph_status, COALESCE(graph.score, 0.0) AS graph_score,
               semantic.status AS semantic_status, COALESCE(semantic.score, 0.0) AS semantic_score,
               rerank.status AS rerank_status, COALESCE(rerank.score, 0.0) AS rerank_score
          FROM boolean_matched matched
          LEFT JOIN storage_v2_occurrence_score_component graph
            ON graph.occurrence_id = matched.id AND graph.stage = 'graph'
           AND graph.profile_id = p_filters ->> 'graph_profile'
          LEFT JOIN storage_v2_occurrence_score_component semantic
            ON semantic.occurrence_id = matched.id AND semantic.stage = 'semantic'
           AND semantic.profile_id = p_filters ->> 'semantic_profile'
          LEFT JOIN storage_v2_occurrence_score_component rerank
            ON rerank.occurrence_id = matched.id AND rerank.stage = 'rerank'
           AND rerank.profile_id = p_filters ->> 'rerank_profile'
    ),
    ranked AS (
        SELECT staged.*,
               lexical_score + graph_score + semantic_score + rerank_score AS final_score
          FROM staged
    ),
    ordered AS (
        SELECT * FROM ranked
         ORDER BY final_score DESC, external_hit_id, id
         LIMIT p_limit
    ),
    results AS (
        SELECT jsonb_agg(jsonb_build_object(
            'occurrence_id', id,
            'external_hit_id', external_hit_id,
            'view_id', view_id,
            'source_id', source_id,
            'source_name', source_name,
            'source_path', source_path,
            'locator', locator,
            'role', role,
            'content', content,
            'score', final_score,
            'score_explanation', jsonb_build_object(
                'lexical', lexical_score,
                'role_weighted_terms', term_detail,
                'normalization', jsonb_build_object(
                    'view_token_count', view_length,
                    'scope_average_view_token_count', (SELECT average_view_length FROM corpus_stats)
                ),
                'graph', jsonb_build_object(
                    'status', COALESCE(graph_status,
                        CASE WHEN p_filters ? 'graph_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', graph_score
                ),
                'semantic', jsonb_build_object(
                    'status', COALESCE(semantic_status,
                        CASE WHEN p_filters ? 'semantic_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', semantic_score
                ),
                'rerank', jsonb_build_object(
                    'status', COALESCE(rerank_status,
                        CASE WHEN p_filters ? 'rerank_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', rerank_score
                ),
                'execution', 'complete_scoped_view_evaluation',
                'pruning', 'disabled_unsafe_bounds'
            ),
            'legacy_successors', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'old_hit_id', mapping.old_hit_id,
                    'ordinal', mapping.ordinal,
                    'relation_kind', mapping.relation_kind
                ) ORDER BY mapping.old_hit_id, mapping.ordinal)
                  FROM legacy_hit_mapping mapping WHERE mapping.occurrence_id = ordered.id
            ), '[]'::JSONB)
        ) ORDER BY final_score DESC, external_hit_id, id) AS value FROM ordered
    )
    SELECT jsonb_build_object(
        'generation_seq', v_generation.generation_seq,
        'execution', 'complete_scoped_view_evaluation',
        'fully_scored_views', (SELECT COUNT(*) FROM view_stats),
        'total', (SELECT COUNT(*) FROM ranked),
        'results', COALESCE((SELECT value FROM results), '[]'::JSONB)
    ) INTO v_result;

    IF EXISTS (
        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
        JOIN generation_item_version membership
          ON membership.source_id = p_source_id
         AND membership.source_item_id = artifact.item_id
         AND membership.artifact_version_id = artifact.id
       WHERE occurrence_row.source_id = p_source_id
         AND membership.valid_from_seq <= v_generation.generation_seq
         AND (membership.valid_to_seq IS NULL
              OR membership.valid_to_seq > v_generation.generation_seq)
         AND (COALESCE(p_filters ->> 'path_prefix', '') = ''
              OR left(occurrence_row.source_path, char_length(p_filters ->> 'path_prefix'))
                 = p_filters ->> 'path_prefix')
         AND (COALESCE(p_filters ->> 'role', '') = ''
              OR occurrence_row.role = p_filters ->> 'role')
         AND (COALESCE(p_filters ->> 'occurred_from', '') = ''
              OR occurrence_row.occurred_at >= (p_filters ->> 'occurred_from')::TIMESTAMPTZ)
         AND (COALESCE(p_filters ->> 'occurred_to', '') = ''
              OR occurrence_row.occurred_at < (p_filters ->> 'occurred_to')::TIMESTAMPTZ)
         AND NOT EXISTS (
             SELECT 1 FROM storage_v2_search_view_document binding
              WHERE binding.view_id = occurrence_row.view_id
         )
    ) THEN
        RAISE EXCEPTION 'required lexical search document missing';
    END IF;
    RETURN v_result;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_resolve_legacy_hit(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_old_hit_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_result JSONB;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'read') THEN
        RAISE EXCEPTION 'authorized generation selector required' USING ERRCODE = '42501';
    END IF;
    IF p_old_hit_id IS NULL OR p_old_hit_id = '' THEN
        RAISE EXCEPTION 'legacy hit id is required';
    END IF;
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);

    WITH
    visible AS (
        SELECT mapping.*, occurrence_row.view_id, occurrence_row.source_path,
               occurrence_row.locator, occurrence_row.role,
               occurrence_row.ordinal AS occurrence_ordinal,
               view_row.view_digest, item.item_key,
               artifact.expected_content_hash
          FROM legacy_hit_mapping mapping
          JOIN occurrence occurrence_row ON occurrence_row.id = mapping.occurrence_id
          JOIN retrieval_view view_row ON view_row.id = occurrence_row.view_id
          JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
         WHERE occurrence_row.source_id = p_source_id
           AND mapping.old_hit_id = p_old_hit_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq)
    )
    SELECT jsonb_build_object(
        'old_hit_id', p_old_hit_id,
        'primary_ordinal', CASE WHEN COUNT(*) > 0 THEN 0 ELSE NULL END,
        'targets', COALESCE(jsonb_agg(jsonb_build_object(
            'ordinal', ordinal,
            'relation_kind', relation_kind,
            'occurrence_id', occurrence_id,
            'external_hit_id', 'storage-v2:' || encode(storage_v2_hash_parts(
                'mainrag.external-hit.v1', ARRAY[
                    int8send(p_source_id),
                    convert_to(item_key, 'UTF8'),
                    convert_to(expected_content_hash, 'UTF8'),
                    view_digest, convert_to(role, 'UTF8'), int8send(occurrence_ordinal),
                    convert_to(locator::TEXT, 'UTF8')
                ]
            ), 'hex'),
            'source_path', source_path,
            'locator', locator,
            'byte_overlap', byte_overlap,
            'source_offset', source_offset
        ) ORDER BY ordinal), '[]'::JSONB)
    ) INTO v_result FROM visible;
    RETURN v_result;
END
$$;

ALTER TABLE storage_v2_search_document ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_search_posting ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_search_view_document ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_occurrence_score_component ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS storage_v2_search_document_admin ON storage_v2_search_document;
CREATE POLICY storage_v2_search_document_admin ON storage_v2_search_document
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS storage_v2_search_posting_admin ON storage_v2_search_posting;
CREATE POLICY storage_v2_search_posting_admin ON storage_v2_search_posting
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS storage_v2_search_view_document_admin ON storage_v2_search_view_document;
CREATE POLICY storage_v2_search_view_document_admin ON storage_v2_search_view_document
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS storage_v2_occurrence_score_component_isolation
    ON storage_v2_occurrence_score_component;
CREATE POLICY storage_v2_occurrence_score_component_isolation
    ON storage_v2_occurrence_score_component
    USING (EXISTS (
        SELECT 1 FROM occurrence visible WHERE visible.id = occurrence_id
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM occurrence visible WHERE visible.id = occurrence_id
    ));

REVOKE INSERT, UPDATE, DELETE ON storage_v2_search_document FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_search_posting FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_search_view_document FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_occurrence_score_component FROM PUBLIC;
