-- Probe immutable lexical matches inside the authorized requested sources.
-- A query with no generated match must not visit every segment of every
-- requested occurrence. Preserve complete ranks, source/artifact provenance,
-- generated/copied tiers, immutable body guards and the total tie order.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_storage_v2_lexical_segment_source
    ON storage_v2_lexical_segment(source_id,occurrence_id);
DO $index$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index index_row
        JOIN pg_class index_class ON index_class.oid=index_row.indexrelid
        JOIN pg_namespace namespace ON namespace.oid=index_class.relnamespace
        JOIN pg_am access_method ON access_method.oid=index_class.relam
        WHERE namespace.nspname='public'
          AND index_class.relname='idx_storage_v2_lexical_segment_source'
          AND index_row.indrelid='storage_v2_lexical_segment'::REGCLASS
          AND access_method.amname='btree'
          AND index_row.indisvalid AND index_row.indisready
          AND NOT index_row.indisunique AND index_row.indpred IS NULL
          AND index_row.indexprs IS NULL
          AND index_row.indnatts=2 AND index_row.indnkeyatts=2
          AND ARRAY(SELECT unnest(index_row.indoption))=ARRAY[0,0]::SMALLINT[]
          AND ARRAY(SELECT unnest(index_row.indcollation))=ARRAY[0,0]::OID[]
          AND ARRAY(SELECT unnest(index_row.indclass))=ARRAY[
              (SELECT class.oid FROM pg_opclass class
                JOIN pg_namespace class_namespace ON class_namespace.oid=class.opcnamespace
               WHERE class_namespace.nspname='pg_catalog' AND class.opcname='int8_ops'
                 AND class.opcmethod=access_method.oid AND class.opcdefault),
              (SELECT class.oid FROM pg_opclass class
                JOIN pg_namespace class_namespace ON class_namespace.oid=class.opcnamespace
               WHERE class_namespace.nspname='pg_catalog' AND class.opcname='int8_ops'
                 AND class.opcmethod=access_method.oid AND class.opcdefault)
          ]::OID[]
          AND ARRAY(SELECT unnest(index_row.indkey))=ARRAY[
              (SELECT attnum FROM pg_attribute WHERE attrelid=index_row.indrelid
                AND attname='source_id'),
              (SELECT attnum FROM pg_attribute WHERE attrelid=index_row.indrelid
                AND attname='occurrence_id')
          ]::SMALLINT[]
    ) THEN
        RAISE EXCEPTION 'source lexical input index definition differs';
    END IF;
END
$index$;

DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT[] := ARRAY[
        '    ), matching_legacy AS MATERIALIZED (',
        $old$        SELECT requested.id FROM requested
         WHERE NOT EXISTS (
            SELECT 1 FROM matching_legacy projection
             WHERE projection.occurrence_id=requested.id
         )$old$,
        '      JOIN storage_v2_lexical_segment segment',
        $old$     WHERE segment.fts_vector @@ v_query
     ORDER BY segment.occurrence_id, 2 DESC, segment.segment_order;$old$
    ];
    v_new TEXT[] := ARRAY[
        $new$    ), matching_segment AS NOT MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               segment.segment_order,segment.fts_vector
         FROM storage_v2_lexical_segment segment
         WHERE segment.fts_vector @@ v_query
           AND segment.source_id IN (SELECT source_id FROM requested_sources)
    ), matching_legacy AS MATERIALIZED ($new$,
        $new$        SELECT requested.id FROM requested
         WHERE EXISTS (
            SELECT 1 FROM matching_segment segment
             WHERE segment.occurrence_id=requested.id
         ) AND NOT EXISTS (
            SELECT 1 FROM matching_legacy projection
             WHERE projection.occurrence_id=requested.id
         )$new$,
        '      JOIN matching_segment segment',
        $new$     -- Matching lexical inputs already proved the query predicate.
     ORDER BY segment.occurrence_id, 2 DESC, segment.segment_order;$new$
    ];
    v_index INTEGER;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        FOR v_index IN 1..cardinality(v_old) LOOP
            IF (length(v_definition)-length(replace(v_definition,v_new[v_index],'')))
                    /length(v_new[v_index])=1
               AND (CASE WHEN v_index=1 THEN
                    (length(v_definition)-length(replace(v_definition,v_old[v_index],'')))
                        /length(v_old[v_index])=1
                    ELSE strpos(v_definition,v_old[v_index])=0 END) THEN
                CONTINUE;
            END IF;
            IF (length(v_definition)-length(replace(v_definition,v_old[v_index],'')))
                    /length(v_old[v_index])<>1
               OR strpos(v_definition,v_new[v_index])>0 THEN
                RAISE EXCEPTION 'source lexical input definition differs';
            END IF;
            v_definition := replace(v_definition,v_old[v_index],v_new[v_index]);
        END LOOP;
        EXECUTE v_definition;
    END LOOP;
END
$migration$;

COMMIT;
