-- Migration 058: complete active-set search under an exact activation receipt.
-- The function is additive; installation does not change the application default.
-- Query evaluation is set-based across every authorized active source.

ALTER TABLE storage_v2_activation_set_evidence
    ADD COLUMN IF NOT EXISTS source_classification_sha256 TEXT
    CHECK (source_classification_sha256 ~ '^[0-9a-f]{64}$');

DROP TRIGGER IF EXISTS storage_v2_activation_receipt_controlled_write
    ON storage_v2_activation_set_evidence;
CREATE TRIGGER storage_v2_activation_receipt_controlled_write
    BEFORE INSERT OR UPDATE OR DELETE ON storage_v2_activation_set_evidence
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

CREATE OR REPLACE FUNCTION storage_v2_bind_activation_source_classification()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
BEGIN
    SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
        'source_id', source.id, 'is_test', source.is_test
    ) ORDER BY source.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
      INTO NEW.source_classification_sha256 FROM sources source;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS storage_v2_activation_source_classification
    ON storage_v2_activation_set_evidence;
CREATE TRIGGER storage_v2_activation_source_classification
    BEFORE INSERT ON storage_v2_activation_set_evidence
    FOR EACH ROW EXECUTE FUNCTION storage_v2_bind_activation_source_classification();

CREATE OR REPLACE FUNCTION public.storage_v2_search_active(p_manifest_sha256 text, p_ast jsonb, p_filters jsonb DEFAULT '{}'::jsonb, p_limit bigint DEFAULT 20, p_source_id bigint DEFAULT NULL, p_include_test boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'off'
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
    v_receipt storage_v2_activation_set_evidence;
    v_result JSONB;
BEGIN
    IF p_manifest_sha256 IS NULL OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'exact activated manifest digest is required';
    END IF;
    SELECT * INTO v_receipt FROM storage_v2_activation_set_evidence
     ORDER BY created_at DESC, id DESC LIMIT 1;
    IF NOT FOUND OR v_receipt.manifest_sha256 <> p_manifest_sha256
       OR v_receipt.source_count <> (SELECT COUNT(*) FROM sources)
       OR v_receipt.source_count <> (SELECT COUNT(*) FROM logical_source)
       OR v_receipt.source_classification_sha256 IS DISTINCT FROM (
           SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
               'source_id', source.id, 'is_test', source.is_test
           ) ORDER BY source.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
             FROM sources source
       )
       OR EXISTS (
           SELECT 1 FROM logical_source pointer
           LEFT JOIN source_generation active_generation
             ON active_generation.id = pointer.active_generation_id
            AND active_generation.source_id = pointer.id
           WHERE active_generation.id IS NULL OR active_generation.status <> 'active'
       ) THEN
        RAISE EXCEPTION 'complete activated source set and exact receipt are required';
    END IF;
    IF p_include_test AND NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'test scope requires administrator authority' USING ERRCODE = '42501';
    END IF;
    IF p_source_id IS NOT NULL THEN
        PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
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

    WITH RECURSIVE
    eligible_source AS MATERIALIZED (
        SELECT source.id, source.name, active_generation.generation_seq
          FROM sources source
          JOIN logical_source pointer ON pointer.id = source.id
          JOIN source_generation active_generation
            ON active_generation.id = pointer.active_generation_id
           AND active_generation.source_id = pointer.id
           AND active_generation.status = 'active'
         WHERE (p_source_id IS NULL OR source.id = p_source_id)
           AND storage_v2_can_access_source(source.id, 'read')
           AND (p_include_test OR NOT source.is_test)
    ),
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
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term'), ARRAY[]::BYTEA[]) AS term_hashes,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::TEXT[]) AS score_terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::BYTEA[]) AS score_term_hashes,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'phrase'), ARRAY[]::TEXT[]) AS phrases,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'exact'), ARRAY[]::TEXT[]) AS exact_values
          FROM leaves
    ),
    visible_occurrence AS (
        SELECT occurrence_row.*, source.name AS source_name, item.item_key,
               source.generation_seq,
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
          JOIN eligible_source source ON source.id = occurrence_row.source_id
          JOIN generation_item_version membership
            ON membership.source_id = occurrence_row.source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
         WHERE membership.valid_from_seq <= source.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > source.generation_seq)
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
               binding.role_weight, document.token_count
          FROM visible_occurrence visible
          JOIN storage_v2_search_view_document binding ON binding.view_id = visible.view_id
          JOIN storage_v2_search_document document ON document.id = binding.document_id
    ),
    view_stats AS (
        SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length
          FROM scoped_binding GROUP BY occurrence_id
    ),
    corpus_stats AS (
        SELECT COUNT(DISTINCT occurrence_id)::DOUBLE PRECISION AS view_count,
               AVG(view_length) AS average_view_length FROM view_stats
    ),
    scoped_posting AS MATERIALIZED (
        SELECT binding.occurrence_id, binding.component_ordinal, binding.role_weight,
               posting.term, posting.term_frequency
          FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN LATERAL (
              SELECT term, term_frequency FROM storage_v2_search_posting
               WHERE document_id = binding.document_id
                 AND term_sha256 = ANY(query.term_hashes)
               OFFSET 0
          ) posting
         WHERE posting.term = ANY(query.terms)
    ),
    document_frequency AS (
        SELECT term, COUNT(DISTINCT occurrence_id)::DOUBLE PRECISION AS frequency
          FROM scoped_posting GROUP BY term
    ),
    term_rows AS (
        SELECT posting.occurrence_id, posting.term, posting.component_ordinal,
               posting.role_weight,
               posting.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_posting posting
          JOIN view_stats ON view_stats.occurrence_id = posting.occurrence_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
    ),
    term_match_aggregate AS MATERIALIZED (
        SELECT occurrence_id, array_agg(DISTINCT term ORDER BY term) AS matched_terms
          FROM scoped_posting GROUP BY occurrence_id
    ),
    best_term AS (
        SELECT DISTINCT ON (occurrence_id, term)
               occurrence_id, term, component_ordinal, role_weight, contribution
          FROM term_rows
         ORDER BY occurrence_id, term, contribution DESC, component_ordinal
    ),
    term_aggregate AS MATERIALIZED (
        SELECT occurrence_id,
               array_agg(term ORDER BY term) AS matched_terms,
               SUM(contribution) AS lexical_terms,
               jsonb_agg(jsonb_build_object(
                   'term', term, 'component_ordinal', component_ordinal,
                   'role_weight', role_weight, 'score', contribution
               ) ORDER BY term) AS detail
          FROM best_term GROUP BY occurrence_id
    ),
    phrase_aggregate AS MATERIALIZED (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT phrase.value ORDER BY phrase.value) AS matched_phrases
          FROM (
              SELECT scope.occurrence_id, document.fts_simple, document.search_text
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.phrases) AS phrase(value)
         WHERE cardinality((SELECT phrases FROM query_values)) > 0 AND storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)
         GROUP BY binding.occurrence_id
    ),
    exact_aggregate AS MATERIALIZED (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT exact.value ORDER BY exact.value) AS matched_exact
          FROM (
              SELECT scope.occurrence_id, document.exact_identifiers
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.exact_values) AS exact(value)
         WHERE cardinality((SELECT exact_values FROM query_values)) > 0 AND exact.value = ANY(binding.exact_identifiers)
         GROUP BY binding.occurrence_id
    ),
    matched AS (
        SELECT visible.*, view_stats.view_length,
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
            'generation_seq', generation_seq,
            'source_name', source_name,
            'source_path', source_path,
            'locator', locator,
            'role', role,
            'content', (
                SELECT string_agg(document.search_text, E'\n' ORDER BY binding.ordinal)
                  FROM storage_v2_search_view_document binding
                  JOIN storage_v2_search_document document ON document.id = binding.document_id
                 WHERE binding.view_id = ordered.view_id
            ),
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
        'generation_seq', NULL,
        'execution', 'complete_scoped_view_evaluation',
        'fully_scored_views', (SELECT COUNT(*) FROM view_stats),
        'total', (SELECT COUNT(*) FROM ranked),
        'results', COALESCE((SELECT value FROM results), '[]'::JSONB)
    ) INTO v_result;

    IF EXISTS (
        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
        JOIN sources source ON source.id = occurrence_row.source_id
        JOIN logical_source pointer ON pointer.id = source.id
        JOIN source_generation active_generation
          ON active_generation.id = pointer.active_generation_id
         AND active_generation.source_id = pointer.id
         AND active_generation.status = 'active'
        JOIN generation_item_version membership
          ON membership.source_id = occurrence_row.source_id
         AND membership.source_item_id = artifact.item_id
         AND membership.artifact_version_id = artifact.id
       WHERE (p_source_id IS NULL OR occurrence_row.source_id = p_source_id)
           AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
           AND (p_include_test OR NOT source.is_test)
         AND membership.valid_from_seq <= active_generation.generation_seq
         AND (membership.valid_to_seq IS NULL
              OR membership.valid_to_seq > active_generation.generation_seq)
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
$function$;
