-- A legacy projection owns compatibility ranking only for queries it matches.
-- Partial legacy indexing must not hide matching immutable generated segments.
-- Keep the legacy body guard, score tiers, source authorization and result types.
DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := $old$    ), unprojected AS MATERIALIZED (
        SELECT requested.id FROM requested
         WHERE NOT EXISTS (
            SELECT 1 FROM storage_v2_legacy_lexical_segment projection
             WHERE projection.occurrence_id=requested.id
         )
    ), requested_kind AS MATERIALIZED ($old$;
    v_new TEXT := $new$    ), requested_sources AS MATERIALIZED (
        SELECT DISTINCT occurrence_row.source_id
          FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id
    ), matching_legacy AS MATERIALIZED (
        SELECT DISTINCT projection.occurrence_id
          FROM storage_v2_legacy_lexical_segment projection
         WHERE projection.fts_vector @@ v_query
           AND projection.source_id IN (SELECT source_id FROM requested_sources)
    ), unprojected AS MATERIALIZED (
        SELECT requested.id FROM requested
         WHERE NOT EXISTS (
            SELECT 1 FROM matching_legacy projection
             WHERE projection.occurrence_id=requested.id
         )
    ), requested_kind AS MATERIALIZED ($new$;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=1
           AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
           OR strpos(v_definition,v_new)>0 THEN
            RAISE EXCEPTION 'source rank definition differs before query-specific fallback';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;
