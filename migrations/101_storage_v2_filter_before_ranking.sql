-- Preserve complete search results while avoiding work on excluded identities.
-- Use semi-joins for large document scopes and defer zero-score stage metadata.
BEGIN;

DO $scope$
DECLARE
    definition TEXT;
    old TEXT := $old$        SELECT posting.document_id,posting.term,posting.term_frequency::BIGINT
          FROM public.storage_v2_search_posting posting
         WHERE posting.term_sha256=public.digest(p_term,'sha256')
           AND posting.term=p_term AND posting.document_id=ANY(p_document_ids)
        UNION ALL
        SELECT block.document_id,item.term,item.frequency
          FROM public.storage_v2_compact_posting_block block
          CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
         WHERE block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
           AND block.document_id=ANY(p_document_ids) AND item.term=p_term;$old$;
    replacement TEXT := $new$        WITH requested AS MATERIALIZED (
            SELECT id FROM unnest(p_document_ids) input(id)
        )
        SELECT posting.document_id,posting.term,posting.term_frequency::BIGINT
          FROM (
              SELECT probe.document_id,probe.term,probe.term_frequency
                FROM public.storage_v2_search_posting probe
               WHERE probe.term=p_term
                 AND probe.term_sha256=public.digest(p_term,'sha256') OFFSET 0
          ) posting
         WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=posting.document_id)
        UNION ALL
        SELECT block.document_id,item.term,item.frequency
          FROM public.storage_v2_compact_posting_block block
          CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
         WHERE block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
           AND item.term=p_term
           AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.document_id);$new$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_scoped_term_posting(bigint[],text)'::REGPROCEDURE);
    IF strpos(definition,replacement)>0 AND strpos(definition,old)=0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
        RAISE EXCEPTION 'scoped posting semi-join definition differs';
    END IF;
    EXECUTE replace(definition,old,replacement);
END
$scope$;

-- Array membership uses a semi-join without sorting or deduplicating the scope.
-- Large scopes use a hash join. Small scopes retain their required,
-- bounded lateral primary-key probes; disabling nested loops does not remove
-- the only legal correlated plan. Suppress JIT on that deliberately high-cost
-- fallback, including direct helper calls outside the search wrapper.
ALTER FUNCTION storage_v2_scoped_term_posting(BIGINT[],TEXT) SET enable_nestloop=off;
ALTER FUNCTION storage_v2_scoped_term_posting(BIGINT[],TEXT) SET jit=off;

DO $matches$
DECLARE
    definition TEXT;
    old TEXT := $old$    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED ($old$;
    replacement TEXT := $new$    IF cardinality(p_occurrence_ids)=0 OR cardinality(p_source_ids)=0 THEN
        RETURN;
    END IF;
    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED ($new$;
    old_query TEXT := $old$    ), matching AS MATERIALIZED (
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
      JOIN requested ON requested.id=matching.occurrence_id;$old$;
    new_query TEXT := $new$    ), requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) AS input(id)
    ), matching AS MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               segment.segment_order
          FROM public.storage_v2_lexical_segment segment
         WHERE segment.fts_vector@@v_query
           AND segment.source_id IN (SELECT id FROM authorized_sources)
           AND segment.source_id=ANY(p_source_ids)
    ), eligible AS MATERIALIZED (
        SELECT matching.* FROM matching
          WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=matching.occurrence_id)
    )
    SELECT eligible.occurrence_id,eligible.source_id,eligible.artifact_version_id,
           eligible.segment_order,ts_rank_cd(detail.fts_vector,v_query,0)
      FROM eligible JOIN LATERAL (
          SELECT stored.source_id,stored.artifact_version_id,stored.fts_vector
            FROM public.storage_v2_lexical_segment stored
           WHERE stored.occurrence_id=eligible.occurrence_id
             AND stored.segment_order=eligible.segment_order OFFSET 0
      ) detail ON detail.source_id=eligible.source_id
       AND detail.artifact_version_id=eligible.artifact_version_id;$new$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_authorized_lexical_matches(bigint[],bigint[],text)'::REGPROCEDURE);
    IF strpos(definition,replacement)>0 AND strpos(definition,new_query)>0
       AND strpos(definition,old_query)=0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
       OR (length(definition)-length(replace(definition,old_query,'')))/length(old_query)<>1 THEN
        RAISE EXCEPTION 'authorized lexical membership definition differs';
    END IF;
    EXECUTE replace(replace(definition,old,replacement),old_query,new_query);
END
$matches$;

DO $unprojected$
DECLARE
    signature TEXT;
    definition TEXT;
    old TEXT := $old$    ), matching_segment AS MATERIALIZED (
        SELECT segment.*
         FROM storage_v2_authorized_lexical_matches(
             p_occurrence_ids,ARRAY(SELECT source_id FROM requested_sources),p_query
         ) segment
    ), matching_legacy AS MATERIALIZED (
        SELECT DISTINCT projection.occurrence_id
          FROM storage_v2_legacy_lexical_segment projection
         WHERE projection.fts_vector @@ v_query
           AND projection.source_id IN (SELECT source_id FROM requested_sources)
    ), requested_matches AS MATERIALIZED ($old$;
    replacement TEXT := $new$    ), matching_legacy AS MATERIALIZED (
        SELECT DISTINCT projection.occurrence_id
          FROM storage_v2_legacy_lexical_segment projection
         WHERE projection.fts_vector @@ v_query
           AND projection.source_id IN (SELECT source_id FROM requested_sources)
    ), requested_unprojected AS MATERIALIZED (
        SELECT requested.id FROM requested
          WHERE NOT EXISTS (SELECT 1 FROM matching_legacy projection
                             WHERE projection.occurrence_id=requested.id)
    ), matching_segment AS MATERIALIZED (
        SELECT segment.*
         FROM storage_v2_authorized_lexical_matches(
             ARRAY(SELECT id FROM requested_unprojected),
             ARRAY(SELECT source_id FROM requested_sources),p_query
         ) segment
    ), requested_matches AS MATERIALIZED ($new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF (strpos(definition,replacement)>0
            OR strpos(definition,replace(replacement,
                'projection.source_id IN (SELECT source_id FROM requested_sources)',
                'projection.source_id=ANY(v_requested_sources)'))>0)
           AND strpos(definition,old)=0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
            RAISE EXCEPTION 'unprojected lexical scope definition differs';
        END IF;
        EXECUTE replace(definition,old,replacement);
    END LOOP;
END
$unprojected$;

-- The source array is derived from authorized requested occurrences. Expose
-- those exact source keys to the custom plan instead of a one-row-estimated
-- CTE semi-join, while retaining all occurrence/artifact/document guards.
DO $projection_scope$
DECLARE
    signature TEXT;
    definition TEXT;
    old TEXT := 'projection.source_id IN (SELECT source_id FROM requested_sources)';
    replacement TEXT := 'projection.source_id=ANY(v_requested_sources)';
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,replacement)>0 AND strpos(definition,old)=0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,old,'')))/length(old)
           <> (CASE WHEN signature LIKE '%_precise(%' THEN 2 ELSE 1 END) THEN
            RAISE EXCEPTION 'legacy projection source scope definition differs';
        END IF;
        EXECUTE replace(definition,old,replacement);
    END LOOP;
END
$projection_scope$;

-- Hash the small source set before aggregating it. DISTINCT inside array_agg
-- otherwise sorts every requested occurrence, including duplicate identities.
DO $requested_sources$
DECLARE
    signature TEXT;
    definition TEXT;
    old TEXT := $old$    SELECT array_agg(DISTINCT occurrence_row.source_id) INTO v_requested_sources
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
      JOIN authorized_source ON authorized_source.id=occurrence_row.source_id;$old$;
    replacement TEXT := $new$    SELECT array_agg(requested_source.source_id ORDER BY requested_source.source_id)
      INTO v_requested_sources FROM (
        SELECT occurrence_row.source_id FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id
         GROUP BY occurrence_row.source_id
      ) requested_source;$new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,replacement)>0 AND strpos(definition,old)=0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
            RAISE EXCEPTION 'requested source grouping definition differs';
        END IF;
        EXECUTE replace(definition,old,replacement);
    END LOOP;
END
$requested_sources$;

-- Resolve broad copied-projection document predicates through the existing
-- document GIN index once. Keep complete occurrence/artifact/body guards and
-- the independently checked segment fallback; small scopes retain PK reads.
DO $copied_document_matches$
DECLARE
    signature TEXT;
    definition TEXT;
    old TEXT := $old$    ), ranked_projection AS MATERIALIZED ($old$;
    replacement TEXT := $new$    ), matching_document AS MATERIALIZED (
        SELECT candidate.id FROM public.storage_v2_search_document candidate
         WHERE cardinality(p_occurrence_ids)>=32768 AND candidate.fts_simple@@v_query
    ), ranked_projection AS MATERIALIZED ($new$;
    old_predicate TEXT := $old$     WHERE document.fts_simple @@ v_query
        OR storage_v2_source_legacy_segment_matches(occurrence_row.id,p_query);$old$;
    new_predicate TEXT := $new$     WHERE CASE WHEN cardinality(p_occurrence_ids)>=32768
                    THEN EXISTS (SELECT 1 FROM matching_document matched
                                  WHERE matched.id=document.id)
                    ELSE document.fts_simple@@v_query END
        OR storage_v2_source_legacy_segment_matches(occurrence_row.id,p_query);$new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,replacement)>0 AND strpos(definition,new_predicate)>0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
           OR (length(definition)-length(replace(definition,old_predicate,'')))/length(old_predicate)<>1 THEN
            RAISE EXCEPTION 'copied document predicate definition differs';
        END IF;
        EXECUTE replace(replace(definition,old,replacement),old_predicate,new_predicate);
    END LOOP;
END
$copied_document_matches$;

-- Corpus normalization needs only immutable document IDs and token counts.
-- Keep that scan on a small covering index instead of the text/vector heap.
CREATE INDEX IF NOT EXISTS idx_storage_v2_document_token_count
    ON storage_v2_search_document(id) INCLUDE(token_count);
DO $token_index$
DECLARE
    actual pg_index;
BEGIN
    SELECT * INTO STRICT actual FROM pg_index
     WHERE indexrelid='idx_storage_v2_document_token_count'::REGCLASS;
    IF actual.indrelid<>'storage_v2_search_document'::REGCLASS
       OR NOT actual.indisvalid OR NOT actual.indisready
       OR actual.indisunique OR actual.indnkeyatts<>1 OR actual.indnatts<>2
       OR (SELECT amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam
            WHERE c.oid=actual.indexrelid)<>'btree'
       OR pg_get_indexdef(actual.indexrelid,1,TRUE)<>'id'
       OR pg_get_indexdef(actual.indexrelid,2,TRUE)<>'token_count'
       OR actual.indpred IS NOT NULL OR actual.indexprs IS NOT NULL THEN
        RAISE EXCEPTION 'document token count index identity differs';
    END IF;
END
$token_index$;

CREATE INDEX IF NOT EXISTS idx_storage_v2_nonzero_stage_score
    ON storage_v2_occurrence_score_component(stage,profile_id,occurrence_id)
    INCLUDE(score) WHERE score IS NOT NULL AND score<>0;

-- Compare the parsed predicate, including its casts, rather than accepting a
-- same-named partial index that could silently omit valid nonzero scores.
CREATE TEMP TABLE storage_v2_expected_nonzero_score (
    stage TEXT,profile_id TEXT,occurrence_id BIGINT,score DOUBLE PRECISION
);
CREATE INDEX storage_v2_expected_nonzero_score_index
    ON storage_v2_expected_nonzero_score(stage,profile_id,occurrence_id)
    INCLUDE(score) WHERE score IS NOT NULL AND score<>0;
DO $index$
DECLARE
    actual pg_index;
    expected pg_index;
BEGIN
    SELECT * INTO STRICT actual FROM pg_index
     WHERE indexrelid='idx_storage_v2_nonzero_stage_score'::REGCLASS;
    SELECT * INTO STRICT expected FROM pg_index
     WHERE indexrelid='pg_temp.storage_v2_expected_nonzero_score_index'::REGCLASS;
    IF actual.indrelid<>'storage_v2_occurrence_score_component'::REGCLASS
       OR NOT actual.indisvalid OR NOT actual.indisready
       OR actual.indisunique OR actual.indnkeyatts<>3 OR actual.indnatts<>4
       OR (SELECT relam FROM pg_class WHERE oid=actual.indexrelid)
          <>(SELECT relam FROM pg_class WHERE oid=expected.indexrelid)
       OR pg_get_indexdef(actual.indexrelid,1,TRUE)<>'stage'
       OR pg_get_indexdef(actual.indexrelid,2,TRUE)<>'profile_id'
       OR pg_get_indexdef(actual.indexrelid,3,TRUE)<>'occurrence_id'
       OR pg_get_indexdef(actual.indexrelid,4,TRUE)<>'score'
       OR pg_get_expr(actual.indpred,actual.indrelid)
          IS DISTINCT FROM pg_get_expr(expected.indpred,expected.indrelid) THEN
        RAISE EXCEPTION 'nonzero score index identity differs';
    END IF;
END
$index$;
DROP TABLE pg_temp.storage_v2_expected_nonzero_score;

DO $readers$
DECLARE
    signature TEXT;
    definition TEXT;
    stage TEXT;
    old TEXT;
    replacement TEXT;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        old:='SELECT occurrence_row.*';
        replacement:=$new$SELECT occurrence_row.id,occurrence_row.source_id,
               occurrence_row.artifact_version_id,occurrence_row.view_id,
               occurrence_row.role,occurrence_row.ordinal$new$;
        IF strpos(definition,replacement)=0 THEN
            IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
                RAISE EXCEPTION 'visible occurrence projection definition differs';
            END IF;
            definition:=replace(definition,old,replacement);
        END IF;
        -- Immutable paths/locators are needed only after the complete scoring
        -- boundary. Keep them out of all corpus-sized materialized rows.
        old:=CASE WHEN signature='storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'
             THEN 'SELECT bounded.*, source.name AS source_name, item.item_key,'
             ELSE 'SELECT bounded.*, item.item_key,' END;
        replacement:=replace(old,'SELECT bounded.*,',
            E'SELECT bounded.*, identified_occurrence.source_path,\n               identified_occurrence.locator,');
        IF strpos(definition,replacement)=0 THEN
            IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
               OR strpos(definition,'convert_to(bounded.locator::TEXT')=0
               OR strpos(definition,E'FROM bounded\n          JOIN artifact_version artifact')=0 THEN
                RAISE EXCEPTION 'late occurrence identity definition differs';
            END IF;
            definition:=replace(definition,old,replacement);
            definition:=replace(definition,'convert_to(bounded.locator::TEXT',
                                'convert_to(identified_occurrence.locator::TEXT');
            definition:=replace(definition,E'FROM bounded\n          JOIN artifact_version artifact',
                E'FROM bounded\n          JOIN occurrence identified_occurrence ON identified_occurrence.id=bounded.id\n          JOIN artifact_version artifact');
        END IF;
        FOREACH stage IN ARRAY ARRAY['graph','semantic','rerank'] LOOP
            old:=format('AND %s.profile_id = p_filters ->> ''%s_profile''',stage,stage);
            replacement:=old||format(' AND %s.score IS NOT NULL AND %s.score<>0',stage,stage);
            IF strpos(definition,replacement)=0 THEN
                IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
                    RAISE EXCEPTION 'nonzero stage scoring definition differs';
                END IF;
                definition:=replace(definition,old,replacement);
            END IF;
            old:=format('COALESCE(%s_status,',stage);
            replacement:=format($new$COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='%s'
                         AND component.profile_id=p_filters->>'%s_profile'),$new$,stage,stage);
            IF strpos(definition,replacement)=0 THEN
                IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
                    RAISE EXCEPTION 'returned stage status definition differs';
                END IF;
                definition:=replace(definition,old,replacement);
            END IF;
        END LOOP;
        old:='WHERE CASE WHEN lexical_rank.occurrence_id IS NOT NULL';
        replacement:=$new$WHERE CASE WHEN lexical_rank.occurrence_id IS NOT NULL
                    AND (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
                    THEN TRUE
                    WHEN lexical_rank.occurrence_id IS NOT NULL$new$;
        IF strpos(definition,replacement)=0 THEN
            IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1 THEN
                RAISE EXCEPTION 'established lexical match definition differs';
            END IF;
            definition:=replace(definition,old,replacement);
        END IF;
        EXECUTE definition;
    END LOOP;
END
$readers$;

COMMIT;
