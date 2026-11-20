-- Reuse the already authorized legacy winner for fragment target keys.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
           storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $copied_fragment_keys$
DECLARE
    target_start CONSTANT TEXT := '), fragment_targets AS MATERIALIZED (';
    target_end CONSTANT TEXT := '), fragment_meta AS MATERIALIZED (';
    target_keys CONSTANT TEXT := $targets$), fragment_targets AS MATERIALIZED (
        -- The chosen binding already passed FTS for this exact source,
        -- occurrence, artifact and chunk in the same stable snapshot.
        SELECT DISTINCT selected.source_id,selected.source_path,selected.legacy_file_hash
          FROM authorized_projection selected
          JOIN authorized_source source ON source.id = selected.source_id
         WHERE encode(selected.legacy_file_hash,'hex') <> selected.canonical_content_hash
    $targets$;
    target RECORD;
    patch RECORD;
    definition TEXT;
    installed BYTEA;
    metadata JSONB;
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
    first_position INTEGER;
    last_position INTEGER;
    projection_arrays TEXT;
    projection_fields TEXT;
BEGIN
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;

    FOR target IN
        SELECT signature, decode(expected_definition_sha256, 'hex') AS predecessor, scoped
        FROM (VALUES
            ('storage_v2_source_segment_rank_candidates(bigint[],text)',
             'f685d3182843d1a9d53ce63316fccf0f775280373889c11b58634815ea0f70b7',FALSE),
            ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',
             '611375271f827a5877c876255f7d7970bd354485c86792221cfaf52969c52353',TRUE)
        ) AS expected(signature, expected_definition_sha256, scoped)
    LOOP
        SELECT pg_get_functiondef(p.oid), to_jsonb(p)-'prosrc'
          INTO STRICT definition, metadata
          FROM pg_proc p
         WHERE p.oid = target.signature::REGPROCEDURE
           AND p.proowner = 'mainrag_v2_frontier_owner'::REGROLE
           AND p.prosecdef AND p.proisstrict AND p.provolatile = 's';
        IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM target.predecessor
           OR EXISTS (SELECT 1 FROM storage_v2_posting_conversion_contract c
                      WHERE c.signature = target.signature) THEN
            RAISE EXCEPTION 'copied fragment keys rank predecessor differs: %', target.signature;
        END IF;

        projection_arrays := 'v_projection_source_ids,v_projection_source_paths,v_projection_legacy_hashes';
        projection_fields := 'source_id,source_path,legacy_file_hash';
        IF target.scoped THEN
            projection_arrays := projection_arrays || ',v_projection_canonical_hashes';
            projection_fields := projection_fields || ',canonical_content_hash';
        END IF;
        FOR patch IN
            SELECT * FROM (VALUES
                ('    v_projection_views BIGINT[];',
                 '    v_projection_views BIGINT[];' || E'\n    v_projection_source_ids BIGINT[];'
                  || E'\n    v_projection_source_paths TEXT[];\n    v_projection_legacy_hashes BYTEA[];'
                  || CASE WHEN target.scoped THEN E'\n    v_projection_canonical_hashes TEXT[];' ELSE '' END),
                (CASE WHEN target.scoped THEN 'binding.legacy_chunk_id,payload.legacy_score'
                      ELSE 'projection.legacy_chunk_id,' || E'\n               ts_rank_cd(projection.fts_vector,v_query,0)' END,
                 CASE WHEN target.scoped THEN 'binding.legacy_chunk_id,binding.legacy_file_hash,payload.legacy_score'
                      ELSE 'projection.legacy_chunk_id,projection.legacy_file_hash,'
                          || E'\n               ts_rank_cd(projection.fts_vector,v_query,0)' END),
                ('projection.legacy_chunk_id,occurrence_row.view_id',
                 'projection.legacy_chunk_id,occurrence_row.view_id,occurrence_row.source_id,'
                 || E'\n               occurrence_row.source_path,projection.legacy_file_hash'),
                ('document.id AS canonical_document_id',
                 'document.id AS canonical_document_id,artifact.expected_content_hash AS canonical_content_hash'),
                ('ARRAY(SELECT DISTINCT matching.occurrence_id FROM query_projection matching)',
                 'array_agg(projection.source_id),array_agg(projection.source_path),array_agg(projection.legacy_file_hash),'
                  || CASE WHEN target.scoped THEN E'\n           array_agg(projection.canonical_content_hash),' ELSE '' END
                  || E'\n           ARRAY(SELECT DISTINCT matching.occurrence_id FROM query_projection matching)'),
                (CASE WHEN target.scoped THEN 'v_projection_documents,v_matching_legacy'
                      ELSE 'v_projection_views,v_matching_legacy' END,
                 CASE WHEN target.scoped THEN 'v_projection_documents,' ELSE 'v_projection_views,' END
                 || E'\n           ' || projection_arrays || ',v_matching_legacy'),
                (CASE WHEN target.scoped THEN 'v_projection_views,v_projection_documents)'
                      ELSE 'v_projection_chunks,v_projection_views)' END,
                 CASE WHEN target.scoped THEN 'v_projection_views,v_projection_documents,'
                      ELSE 'v_projection_chunks,v_projection_views,' END
                 || E'\n            ' || projection_arrays || ')'),
                (CASE WHEN target.scoped THEN 'projection(occurrence_id,artifact_version_id,score,legacy_chunk_id,view_id,canonical_document_id)'
                      ELSE 'projection(occurrence_id,artifact_version_id,score,legacy_chunk_id,view_id)' END,
                 CASE WHEN target.scoped THEN 'projection(occurrence_id,artifact_version_id,score,legacy_chunk_id,view_id,canonical_document_id,'
                      ELSE 'projection(occurrence_id,artifact_version_id,score,legacy_chunk_id,view_id,' END
                 || E'\n                       ' || projection_fields || ')')
            ) AS edits(marker,replacement)
        LOOP
            IF (length(definition)-length(replace(definition,patch.marker,''))) <> length(patch.marker) THEN
                RAISE EXCEPTION 'copied fragment keys projection boundary differs: %, %', target.signature, patch.marker;
            END IF;
            definition := replace(definition,patch.marker,patch.replacement);
        END LOOP;

        first_position := strpos(definition, target_start);
        last_position := strpos(definition, target_end);
        IF first_position = 0 OR last_position <= first_position
           OR (length(definition)-length(replace(definition,target_start,''))) <> length(target_start)
           OR (length(definition)-length(replace(definition,target_end,''))) <> length(target_end) THEN
            RAISE EXCEPTION 'copied fragment keys target boundary differs: %', target.signature;
        END IF;
        EXECUTE overlay(definition PLACING target_keys FROM first_position FOR last_position-first_position);
        SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
          FROM pg_proc p
         WHERE p.oid = target.signature::REGPROCEDURE AND to_jsonb(p)-'prosrc' = metadata;
        UPDATE storage_v2_lexical_conversion_function_contract c SET definition_sha256 = installed
          WHERE c.signature = target.signature AND c.definition_sha256 = target.predecessor;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'copied fragment keys dependency differs: %', target.signature;
        END IF;
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                  WHERE tgrelid = 'storage_v2_lexical_conversion_contract_seal'::REGCLASS
                    AND tgname = 'storage_v2_lexical_conversion_seal_immutable'
                    AND tgenabled = 'O' AND tgtype = 27
                    AND tgfoid = 'storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'copied fragment keys requires the active lexical seal guard';
    END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE storage_v2_lexical_conversion_contract_seal s
       SET contract_sha256 = sha256(convert_to(jsonb_build_object(
           'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                          FROM storage_v2_lexical_conversion_function_contract c),
           'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                          FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s) = to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'copied fragment keys seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $copied_fragment_keys$;

SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
