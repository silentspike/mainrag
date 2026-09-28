-- Preserve compatibility ranking for immutable fragments and retain the exact
-- legacy REAL rank inside a DOUBLE PRECISION tier. Keep the historical scalar
-- rank surface intact for existing callers.
DO $migration$
DECLARE
    v_signature REGPROCEDURE := 'storage_v2_copy_legacy_lexical_segments(bigint,bigint)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT := E'    PERFORM storage_v2_materialize_legacy_chunk_ranks(\n'
        || E'        p_occurrence_id, p_artifact_version_id\n'
        || E'    );\n';
    v_anchor TEXT := '    SELECT file.id INTO v_file_id FROM files file';
    v_prefix TEXT := E'    -- Materialize rank inputs before the exact contiguous-copy guard.\n'
        || E'    IF NOT EXISTS (SELECT 1 FROM storage_v2_legacy_lexical_segment\n'
        || E'                    WHERE occurrence_id=p_occurrence_id) THEN\n'
        || E'        PERFORM storage_v2_materialize_legacy_chunk_ranks(\n'
        || E'            p_occurrence_id,p_artifact_version_id);\n'
        || E'    END IF;\n';
BEGIN
    IF strpos(v_definition,v_prefix)>0 AND strpos(v_definition,v_old)=0 THEN RETURN; END IF;
    IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
        OR (length(v_definition)-length(replace(v_definition,v_anchor,'')))/length(v_anchor)<>1 THEN
        RAISE EXCEPTION 'legacy lexical copy definition differs before fragment repair';
    END IF;
    v_definition := replace(v_definition,v_old,'');
    EXECUTE replace(v_definition,v_anchor,v_prefix||v_anchor);
END
$migration$;

DO $migration$
DECLARE
    v_definition TEXT := pg_get_functiondef('storage_v2_source_segment_ranks(bigint[],text)'::REGPROCEDURE);
    v_return TEXT := 'score real';
    v_legacy TEXT := '(1000000.0 + ts_rank_cd(projection.fts_vector,v_query,0))::REAL';
    v_precise TEXT := '(1000000.0::DOUBLE PRECISION + ts_rank_cd(projection.fts_vector,v_query,0)::DOUBLE PRECISION)';
BEGIN
    IF (length(v_definition)-length(replace(v_definition,v_return,'')))/length(v_return)<>1
        OR (length(v_definition)-length(replace(v_definition,v_legacy,'')))/length(v_legacy)<>1 THEN
        RAISE EXCEPTION 'source rank definition differs before precision repair';
    END IF;
    v_definition := replace(v_definition,'storage_v2_source_segment_ranks(',
                                         'storage_v2_source_segment_ranks_precise(');
    v_definition := replace(v_definition,v_return,'score double precision');
    v_definition := replace(v_definition,v_legacy,v_precise);
    v_definition := replace(v_definition,
        '(1000000.0::DOUBLE PRECISION + ts_rank_cd(projection.fts_vector,v_query,0)::DOUBLE PRECISION)',
        '(1000000.0::DOUBLE PRECISION + projection.legacy_score)');
    v_definition := replace(v_definition,'         WHERE projection.fts_vector @@ v_query','');
    -- Gather query matches once inside the requested source set. The previous
    -- nested plan reread every chunk vector for each requested occurrence,
    -- including queries with no hits. Keep the final occurrence, artifact,
    -- authorization and immutable-body checks below this input boundary.
    v_definition := replace(v_definition,
        '    ), ranked_projection AS MATERIALIZED (',
        $replacement$    ), requested_sources AS MATERIALIZED (
        SELECT DISTINCT occurrence_row.source_id
          FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id
    ), query_projection AS MATERIALIZED (
        SELECT projection.occurrence_id,projection.source_id,projection.artifact_version_id,
               projection.legacy_chunk_id,
               ts_rank_cd(projection.fts_vector,v_query,0)::DOUBLE PRECISION AS legacy_score
          FROM storage_v2_legacy_lexical_segment projection
         WHERE projection.fts_vector @@ v_query
           AND projection.source_id IN (SELECT source_id FROM requested_sources)
    ), ranked_projection AS MATERIALIZED ($replacement$);
    v_definition := replace(v_definition,
        'JOIN storage_v2_legacy_lexical_segment projection',
        'JOIN query_projection projection');
    -- The generated branch remains the same bounded REAL value, widened at the
    -- result boundary; its existing rank classes and scalar behavior stay intact.
    v_definition := replace(v_definition,'END)::REAL,','END)::DOUBLE PRECISION,');
    EXECUTE v_definition;
END
$migration$;
ALTER FUNCTION storage_v2_source_segment_ranks_precise(BIGINT[],TEXT) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_source_segment_ranks_precise(BIGINT[],TEXT) SET plan_cache_mode TO force_custom_plan;
REVOKE ALL ON FUNCTION storage_v2_source_segment_ranks_precise(BIGINT[],TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks_precise(BIGINT[],TEXT) TO mainrag;

DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := 'storage_v2_source_segment_ranks(';
    v_new TEXT := 'storage_v2_source_segment_ranks_precise(';
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF strpos(v_definition,v_new)>0 AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'search rank caller definition differs';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;

-- Keep corpus scoring complete, but hydrate immutable identities only at the
-- exact score/tie boundary. Foreign keys bind membership to its item/artifact.
DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT;
    v_new TEXT;
BEGIN
    v_signature := 'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)';
    v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
    v_old := $old$    visible_occurrence AS (
        SELECT occurrence_row.*, source.name AS source_name, item.item_key,
               artifact.expected_content_hash, view_row.view_digest
          FROM occurrence occurrence_row
          JOIN retrieval_view view_row ON view_row.id = occurrence_row.view_id
          JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN sources source ON source.id = occurrence_row.source_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
$old$;
    v_new := $new$    visible_occurrence AS (
        SELECT occurrence_row.*
          FROM occurrence occurrence_row
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.artifact_version_id = occurrence_row.artifact_version_id
$new$;
    IF strpos(v_definition,v_new)=0 THEN
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'search identity hydration definition differs';
        END IF;
        v_definition := replace(v_definition,v_old,v_new);
    END IF;
    v_old := $old$    identified AS MATERIALIZED (
        SELECT bounded.*,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(bounded.item_key, 'UTF8'),
                        convert_to(bounded.expected_content_hash, 'UTF8'),
                        bounded.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(bounded.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM bounded
    ),
$old$;
    v_new := $new$    identified AS MATERIALIZED (
        SELECT bounded.*, source.name AS source_name, item.item_key,
               artifact.expected_content_hash, view_row.view_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'),
                        view_row.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(bounded.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM bounded
          JOIN artifact_version artifact ON artifact.id = bounded.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN retrieval_view view_row ON view_row.id = bounded.view_id
          JOIN sources source ON source.id = bounded.source_id
    ),
$new$;
    IF strpos(v_definition,v_new)=0 THEN
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'search identity hydration definition differs';
        END IF;
        v_definition := replace(v_definition,v_old,v_new);
    END IF;
    EXECUTE v_definition;
    v_signature := 'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)';
    v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
    v_old := $old$    visible_occurrence AS (
        SELECT occurrence_row.*, source.name AS source_name, item.item_key,
               source.generation_seq,
               artifact.expected_content_hash, view_row.view_digest
          FROM occurrence occurrence_row
          JOIN retrieval_view view_row ON view_row.id = occurrence_row.view_id
          JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN eligible_source source ON source.id = occurrence_row.source_id
          JOIN generation_item_version membership
            ON membership.source_id = occurrence_row.source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
$old$;
    v_new := $new$    visible_occurrence AS (
        SELECT occurrence_row.*, source.name AS source_name, source.generation_seq
          FROM occurrence occurrence_row
          JOIN eligible_source source ON source.id = occurrence_row.source_id
          JOIN generation_item_version membership
            ON membership.source_id = occurrence_row.source_id
           AND membership.artifact_version_id = occurrence_row.artifact_version_id
$new$;
    IF strpos(v_definition,v_new)=0 THEN
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'search identity hydration definition differs';
        END IF;
        v_definition := replace(v_definition,v_old,v_new);
    END IF;
    v_old := $old$    identified AS MATERIALIZED (
        SELECT bounded.*,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(bounded.item_key, 'UTF8'),
                        convert_to(bounded.expected_content_hash, 'UTF8'),
                        bounded.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(bounded.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM bounded
    ),
$old$;
    v_new := $new$    identified AS MATERIALIZED (
        SELECT bounded.*, item.item_key,
               artifact.expected_content_hash, view_row.view_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'),
                        view_row.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(bounded.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM bounded
          JOIN artifact_version artifact ON artifact.id = bounded.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN retrieval_view view_row ON view_row.id = bounded.view_id
    ),
$new$;
    IF strpos(v_definition,v_new)=0 THEN
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'search identity hydration definition differs';
        END IF;
        v_definition := replace(v_definition,v_old,v_new);
    END IF;
    EXECUTE v_definition;
END
$migration$;
