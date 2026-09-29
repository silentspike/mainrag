-- Gather matching lexical rows once within the authorized requested sources.
-- Materialize only identities and scalar ranks, rather than complete vectors.
-- Preserve requested membership, provenance, rank tiers and Boolean semantics.
BEGIN;

-- Source authorization must resolve persistent relations before caller-owned
-- temporary tables, including when invoked by the dedicated rank definer.
ALTER FUNCTION user_can_access_source(UUID,BIGINT,TEXT)
    SET search_path=pg_catalog,public,pg_temp;

-- This role has no login, membership or write capability. Only the following
-- source-authorized definer can use its SELECT policy. Ordinary RLS policies
-- and FORCE ROW LEVEL SECURITY remain enabled. A restrictive policy still
-- cannot be bypassed. Non-leakproof FTS operators can use their GIN index here
-- without scanning every row behind the ordinary authorization subquery.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mainrag_v2_lexical_rank_owner') THEN
        CREATE ROLE mainrag_v2_lexical_rank_owner NOLOGIN NOSUPERUSER
            NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mainrag_v2_lexical_rank_owner'
               AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                    OR rolinherit OR rolreplication OR rolbypassrls))
       OR EXISTS (SELECT 1 FROM pg_auth_members
                   WHERE roleid='mainrag_v2_lexical_rank_owner'::REGROLE
                      OR member='mainrag_v2_lexical_rank_owner'::REGROLE) THEN
        RAISE EXCEPTION 'lexical rank owner authority differs';
    END IF;
END
$role$;
GRANT USAGE ON SCHEMA public TO mainrag_v2_lexical_rank_owner;
GRANT SELECT ON sources,storage_v2_lexical_segment
    TO mainrag_v2_lexical_rank_owner;
-- The source table's existing administrator policy reads only these columns.
GRANT SELECT(id,is_admin) ON users TO mainrag_v2_lexical_rank_owner;

DO $policy$
DECLARE
    v_policy pg_policy;
BEGIN
    SELECT * INTO v_policy FROM pg_policy
     WHERE polrelid='storage_v2_lexical_segment'::REGCLASS
       AND polname='storage_v2_lexical_segment_rank_reader';
    IF FOUND THEN
        IF v_policy.polcmd<>'r' OR NOT v_policy.polpermissive
           OR v_policy.polroles<>ARRAY['mainrag_v2_lexical_rank_owner'::REGROLE::OID]
           OR pg_get_expr(v_policy.polqual,v_policy.polrelid)<>'true'
           OR v_policy.polwithcheck IS NOT NULL THEN
            RAISE EXCEPTION 'lexical rank reader policy differs';
        END IF;
    ELSE
        CREATE POLICY storage_v2_lexical_segment_rank_reader ON storage_v2_lexical_segment
            FOR SELECT TO mainrag_v2_lexical_rank_owner USING (TRUE);
    END IF;
END
$policy$;

CREATE OR REPLACE FUNCTION storage_v2_authorized_lexical_matches(
    p_occurrence_ids BIGINT[],p_source_ids BIGINT[],p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT,source_id BIGINT,artifact_version_id BIGINT,
                segment_order BIGINT,lexical_score REAL)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp
SET row_security=on
SET plan_cache_mode=force_custom_plan
AS $$
DECLARE
    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
BEGIN
    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED (
        SELECT source.id FROM public.sources source
         WHERE source.id=ANY(p_source_ids)
           AND public.storage_v2_can_access_source(source.id,'read')
    ), matching AS MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               segment.segment_order,ts_rank_cd(segment.fts_vector,v_query,0) AS lexical_score
          FROM public.storage_v2_lexical_segment segment
         WHERE segment.fts_vector@@v_query
           AND segment.source_id IN (SELECT id FROM authorized_sources)
    ), requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    )
    SELECT matching.occurrence_id,matching.source_id,matching.artifact_version_id,
           matching.segment_order,matching.lexical_score
      FROM matching
      JOIN requested ON requested.id=matching.occurrence_id;
END
$$;
ALTER FUNCTION storage_v2_authorized_lexical_matches(BIGINT[],BIGINT[],TEXT)
    OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_authorized_lexical_matches(BIGINT[],BIGINT[],TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_authorized_lexical_matches(BIGINT[],BIGINT[],TEXT)
    TO mainrag_v2_frontier_owner;

-- Small scopes keep primary-key probes. For a large fraction of the physical
-- document corpus, a term-index scan plus exact document membership avoids
-- one random index lookup per document. Catalog estimates only select a plan;
-- both paths return the complete same scoped postings with a full text guard.
CREATE OR REPLACE FUNCTION storage_v2_scoped_term_posting(
    p_document_ids BIGINT[],p_term TEXT
) RETURNS TABLE(document_id BIGINT,term TEXT,term_frequency BIGINT)
LANGUAGE plpgsql STABLE STRICT
SET search_path=pg_catalog,public,pg_temp
SET plan_cache_mode=force_custom_plan
AS $$
DECLARE
    v_document_estimate DOUBLE PRECISION;
BEGIN
    SELECT GREATEST(reltuples,1) INTO v_document_estimate FROM pg_class
     WHERE oid='public.storage_v2_search_document'::REGCLASS;
    IF cardinality(p_document_ids)>4096
       AND cardinality(p_document_ids)>=v_document_estimate*0.1 THEN
        RETURN QUERY
        SELECT posting.document_id,posting.term,posting.term_frequency::BIGINT
          FROM public.storage_v2_search_posting posting
         WHERE posting.term_sha256=digest(p_term,'sha256')
           AND posting.term=p_term AND posting.document_id=ANY(p_document_ids);
    ELSE
        RETURN QUERY
        SELECT requested.id,posting.term,posting.term_frequency::BIGINT
          FROM (SELECT DISTINCT id FROM unnest(p_document_ids) input(id)) requested
          CROSS JOIN LATERAL (
              SELECT probe.term,probe.term_frequency
                FROM public.storage_v2_search_posting probe
               WHERE probe.document_id=requested.id
                 AND probe.term_sha256=digest(p_term,'sha256') OFFSET 0
          ) posting
         WHERE posting.term=p_term;
    END IF;
END
$$;
ALTER FUNCTION storage_v2_scoped_term_posting(BIGINT[],TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_scoped_term_posting(BIGINT[],TEXT) FROM PUBLIC;

DO $posting$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := $old$        SELECT document.document_id, posting.term, posting.term_frequency
          FROM term_probe_state state
          CROSS JOIN scoped_document document
          CROSS JOIN LATERAL (
              SELECT term, term_frequency FROM storage_v2_search_posting
               WHERE document_id = document.document_id
                 AND term_sha256 = digest(state.term, 'sha256')
               OFFSET 0
          ) posting
         WHERE state.overflow AND posting.term = state.term$old$;
    v_new TEXT := $new$        SELECT posting.document_id, posting.term, posting.term_frequency
          FROM term_probe_state state
          CROSS JOIN LATERAL storage_v2_scoped_term_posting(
              ARRAY(SELECT document_id FROM scoped_document),state.term
          ) posting
         WHERE state.overflow AND posting.term = state.term$new$;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=1
           AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
           OR strpos(v_definition,v_new)>0 THEN
            RAISE EXCEPTION 'scoped term posting definition differs';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$posting$;

-- Derive the requested source set once for the two provenance branches.
DO $sources$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := $old$        SELECT DISTINCT occurrence_row.source_id
          FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id$old$;
    v_new TEXT := $new$        SELECT source_id FROM unnest(v_requested_sources) input(source_id)$new$;
    v_declaration TEXT := $old$    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
BEGIN$old$;
    v_cached TEXT := $new$    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
    v_requested_sources BIGINT[];
BEGIN
    WITH requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id,'read')
    )
    SELECT array_agg(DISTINCT occurrence_row.source_id) INTO v_requested_sources
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
      JOIN authorized_source ON authorized_source.id=occurrence_row.source_id;$new$;
    v_count INTEGER;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        v_count := CASE WHEN v_signature LIKE '%_precise(%' THEN 2 ELSE 1 END;
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=v_count
           AND strpos(v_definition,v_cached)>0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>v_count
           OR (length(v_definition)-length(replace(v_definition,v_declaration,'')))/length(v_declaration)<>1
           OR strpos(v_definition,v_new)>0 OR strpos(v_definition,v_cached)>0 THEN
            RAISE EXCEPTION 'requested source cache definition differs';
        END IF;
        EXECUTE replace(replace(v_definition,v_old,v_new),v_declaration,v_cached);
    END LOOP;
END
$sources$;

-- DISTINCT underestimates large ID arrays as 200 rows. Both result branches
-- already choose exactly one rank per occurrence with DISTINCT ON; duplicate
-- input IDs cannot affect scores or ties. Keep their real input cardinality.
DO $requested$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := 'SELECT DISTINCT id FROM unnest(p_occurrence_ids)';
    v_new TEXT := 'SELECT id FROM unnest(p_occurrence_ids)';
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF strpos(v_definition,v_old)=0
           AND (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=3 THEN
            CONTINUE;
        END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>2
           OR (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)<>1 THEN
            RAISE EXCEPTION 'requested occurrence cardinality definition differs';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$requested$;

DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT[] := ARRAY[
        $old$    ), matching_segment AS NOT MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               segment.segment_order,segment.fts_vector$old$,
        $old$         FROM storage_v2_lexical_segment segment
         WHERE segment.fts_vector @@ v_query
           AND segment.source_id IN (SELECT source_id FROM requested_sources)$old$,
        'ts_rank_cd(segment.fts_vector,v_query)',
        $old$    ), unprojected AS MATERIALIZED (
        SELECT requested.id FROM requested
         WHERE EXISTS (
            SELECT 1 FROM matching_segment segment
             WHERE segment.occurrence_id=requested.id
         ) AND NOT EXISTS (
            SELECT 1 FROM matching_legacy projection
             WHERE projection.occurrence_id=requested.id
         )$old$
    ];
    v_new TEXT[] := ARRAY[
        $new$    ), matching_segment AS MATERIALIZED (
        SELECT segment.*$new$,
        $new$         FROM storage_v2_authorized_lexical_matches(
             p_occurrence_ids,ARRAY(SELECT source_id FROM requested_sources),p_query
         ) segment$new$,
        'segment.lexical_score',
        $new$    ), requested_matches AS MATERIALIZED (
        SELECT requested.id
          FROM requested
          JOIN (SELECT DISTINCT segment.occurrence_id FROM matching_segment segment) matched
            ON matched.occurrence_id=requested.id
    ), unprojected AS MATERIALIZED (
        SELECT matched.id FROM requested_matches matched
          LEFT JOIN matching_legacy projection ON projection.occurrence_id=matched.id
         WHERE projection.occurrence_id IS NULL$new$
    ];
    v_index INTEGER;
    v_count INTEGER;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        FOR v_index IN 1..cardinality(v_old) LOOP
            v_count := CASE WHEN v_index=3 THEN 2 ELSE 1 END;
            IF (length(v_definition)-length(replace(v_definition,v_new[v_index],'')))
                    /length(v_new[v_index])=v_count
               AND strpos(v_definition,v_old[v_index])=0 THEN
                CONTINUE;
            END IF;
            IF (length(v_definition)-length(replace(v_definition,v_old[v_index],'')))
                    /length(v_old[v_index])<>v_count
               OR strpos(v_definition,v_new[v_index])>0 THEN
                RAISE EXCEPTION 'materialized lexical match definition differs';
            END IF;
            v_definition := replace(v_definition,v_old[v_index],v_new[v_index]);
        END LOOP;
        EXECUTE v_definition;
    END LOOP;
END
$migration$;

-- Every accepted AST has a positive anchor. With no term/phrase/exact evidence
-- and no lexical rank it cannot match. CASE avoids recursive AST evaluation
-- for such rows, while retaining the complete scoped corpus for normalization.
DO $evidence$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT[] := ARRAY[
        $old$    matched AS (
        SELECT visible.*, view_stats.view_length,$old$,
        $old$          FROM visible_occurrence visible
          JOIN view_stats ON view_stats.occurrence_id = visible.id$old$,
        $old$          FROM storage_v2_source_segment_ranks_precise(
              (SELECT array_agg(id) FROM matched),$old$,
        $old$                   THEN (SELECT array_agg(id) FROM matched)
                   ELSE NULL::BIGINT[] END$old$
    ];
    v_new TEXT[] := ARRAY[
        $new$    evidence_occurrence AS MATERIALIZED (
        SELECT occurrence_id FROM term_match_aggregate
        UNION SELECT occurrence_id FROM phrase_aggregate
        UNION SELECT occurrence_id FROM exact_aggregate
        UNION SELECT occurrence_id FROM lexical_ranks
    ),
    matched AS MATERIALIZED (
        SELECT visible.*, view_stats.view_length,$new$,
        $new$          FROM visible_occurrence visible
          JOIN evidence_occurrence evidence ON evidence.occurrence_id=visible.id
          JOIN view_stats ON view_stats.occurrence_id = visible.id$new$,
        $new$          FROM storage_v2_source_segment_ranks_precise(
              (SELECT array_agg(id) FROM visible_occurrence),$new$,
        $new$                   THEN (SELECT array_agg(matched.id) FROM matched
                         LEFT JOIN lexical_ranks ranked ON ranked.occurrence_id=matched.id
                        WHERE ranked.occurrence_id IS NULL)
                   ELSE NULL::BIGINT[] END$new$
    ];
    v_index INTEGER;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        FOR v_index IN 1..cardinality(v_old) LOOP
            IF (length(v_definition)-length(replace(v_definition,v_new[v_index],'')))
                    /length(v_new[v_index])=1 AND strpos(v_definition,v_old[v_index])=0 THEN
                CONTINUE;
            END IF;
            IF (length(v_definition)-length(replace(v_definition,v_old[v_index],'')))
                    /length(v_old[v_index])<>1 OR strpos(v_definition,v_new[v_index])>0 THEN
                RAISE EXCEPTION 'complete Boolean evidence definition differs';
            END IF;
            v_definition := replace(v_definition,v_old[v_index],v_new[v_index]);
        END LOOP;
        EXECUTE v_definition;
    END LOOP;
END
$evidence$;

DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := $old$         WHERE (storage_v2_simple_and_query(p_ast) IS NULL AND (
             storage_v2_search_ast_matches(
                 p_ast, matched_terms, matched_phrases, matched_exact
             ) OR (p_ast ->> 'type' = 'term' AND lexical_rank.occurrence_id IS NOT NULL)
         )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (
             lexical_rank.occurrence_id IS NOT NULL
             OR (presence.occurrence_id IS NULL AND storage_v2_search_ast_matches(
                 p_ast, matched_terms, matched_phrases, matched_exact
             ))
         ))$old$;
    v_new TEXT := $new$         WHERE CASE WHEN lexical_rank.occurrence_id IS NOT NULL
                    OR cardinality(matched_terms)>0
                    OR cardinality(matched_phrases)>0
                    OR cardinality(matched_exact)>0 THEN
             (storage_v2_simple_and_query(p_ast) IS NULL AND (
                 storage_v2_search_ast_matches(
                     p_ast, matched_terms, matched_phrases, matched_exact
                 ) OR (p_ast ->> 'type' = 'term' AND lexical_rank.occurrence_id IS NOT NULL)
             )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (
                 lexical_rank.occurrence_id IS NOT NULL
                 OR (presence.occurrence_id IS NULL AND storage_v2_search_ast_matches(
                     p_ast, matched_terms, matched_phrases, matched_exact
                 ))
             ))
             ELSE FALSE END$new$;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=1
           AND strpos(v_definition,v_old)=0 THEN
            CONTINUE;
        END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
           OR strpos(v_definition,v_new)>0 THEN
            RAISE EXCEPTION 'Boolean lexical evidence definition differs';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;

-- Serialize score explanations only after the exact score/tie boundary has
-- selected returned rows. Complete term frequencies and numeric contributions
-- still cover the entire authorized corpus, including non-returned matches.
DO $detail$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT[] := ARRAY[
        $old$        SELECT occurrence_id,
               array_agg(term ORDER BY term) AS matched_terms,
               SUM(contribution) AS lexical_terms,
               jsonb_agg(jsonb_build_object(
                   'term', term, 'component_ordinal', component_ordinal,
                   'role_weight', role_weight, 'score', contribution
               ) ORDER BY term) AS detail
          FROM best_term GROUP BY occurrence_id$old$,
        $old$COALESCE(term_aggregate.detail, '[]'::JSONB) AS term_detail$old$,
        $old$'role_weighted_terms', term_detail,$old$
    ];
    v_new TEXT[] := ARRAY[
        $new$        SELECT occurrence_id,SUM(contribution) AS lexical_terms
          FROM best_term GROUP BY occurrence_id$new$,
        $new$'[]'::JSONB AS term_detail$new$,
        $new$'role_weighted_terms', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'term', detail.term, 'component_ordinal', detail.component_ordinal,
                        'role_weight', detail.role_weight, 'score', detail.contribution
                    ) ORDER BY detail.term)
                    FROM best_term detail WHERE detail.occurrence_id=ordered.id
                ),'[]'::JSONB),$new$
    ];
    v_index INTEGER;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        FOR v_index IN 1..cardinality(v_old) LOOP
            IF (length(v_definition)-length(replace(v_definition,v_new[v_index],'')))
                    /length(v_new[v_index])=1 AND strpos(v_definition,v_old[v_index])=0 THEN
                CONTINUE;
            END IF;
            IF (length(v_definition)-length(replace(v_definition,v_old[v_index],'')))
                    /length(v_old[v_index])<>1 OR strpos(v_definition,v_new[v_index])>0 THEN
                RAISE EXCEPTION 'late score detail definition differs';
            END IF;
            v_definition := replace(v_definition,v_old[v_index],v_new[v_index]);
        END LOOP;
        EXECUTE v_definition;
    END LOOP;
END
$detail$;

COMMIT;
