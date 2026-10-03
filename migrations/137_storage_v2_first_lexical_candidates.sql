-- Reduce native matching segments before rank-reader materialization.
-- Preserve full lexical enumeration, exact predicates, authorization and rank ties.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $$ DECLARE signature TEXT; expected TEXT; owner_id REGROLE; caller_id REGROLE; BEGIN
    FOR signature,expected,owner_id IN SELECT * FROM (VALUES
        ('storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)',
         'e6f568994eba664dfa80fa2e720ddb91abd472a1e30dd2ff54730c8992357114',
         'mainrag_v2_lexical_rank_owner'::REGROLE),
        ('storage_v2_source_segment_rank_candidates(bigint[],text)',
         'e1c5046f3a347135454f6bb980330649212bbf27c47090e2e24951e47668b42e',
         'mainrag_v2_frontier_owner'::REGROLE),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',
         '094e30d64ba93de5495ae961efcadab72152b33e40730b62683c751ae7a09665',
         'mainrag_v2_frontier_owner'::REGROLE)
    ) required(signature,expected,owner_id) LOOP
        caller_id:=CASE WHEN signature='storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'
            THEN 'mainrag_v2_frontier_owner'::REGROLE ELSE 'mainrag'::REGROLE END;
        IF encode(sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8')),'hex')<>expected THEN
            RAISE EXCEPTION 'first lexical candidate definition differs: %',signature;
        END IF;
        IF (SELECT proowner FROM pg_proc WHERE oid=signature::REGPROCEDURE)<>owner_id
           OR NOT has_function_privilege(owner_id,signature,'EXECUTE')
           OR NOT has_function_privilege(caller_id,signature,'EXECUTE')
           OR EXISTS (SELECT required.role_id FROM unnest(ARRAY[owner_id,caller_id]) required(role_id)
                WHERE NOT EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                    aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                    WHERE routine.oid=signature::REGPROCEDURE AND permission.grantee=required.role_id
                      AND permission.privilege_type='EXECUTE'))
           OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                WHERE routine.oid=signature::REGPROCEDURE
                  AND (permission.grantee NOT IN (owner_id,caller_id)
                       OR permission.privilege_type<>'EXECUTE'
                       OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
            RAISE EXCEPTION 'first lexical candidate authority differs: %',signature;
        END IF;
    END LOOP;
END $$;

DO $$ BEGIN
    IF encode(sha256(convert_to(pg_get_functiondef(
        'storage_v2_lexical_block_orders_valid(bigint,bigint[])'::REGPROCEDURE),'UTF8')),'hex')
       <>'fe98de2c85dbefd649dfba374f44bde6dfa618d4f03d5cd4c375b3c7c8818653'
       OR NOT EXISTS (SELECT 1 FROM pg_attribute
            WHERE attrelid='storage_v2_compact_lexical_block'::REGCLASS
              AND attname='segment_orders' AND attnotnull AND NOT attisdropped)
       OR NOT EXISTS (SELECT 1 FROM pg_attribute
            WHERE attrelid='storage_v2_compact_lexical_block'::REGCLASS
              AND attname='block_order' AND attnotnull AND NOT attisdropped)
       OR NOT EXISTS (SELECT 1 FROM pg_constraint
            WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS
              AND contype='c' AND convalidated AND NOT condeferrable
              AND pg_get_constraintdef(oid)=
                  'CHECK (storage_v2_lexical_block_orders_valid(block_order, segment_orders))')
       OR NOT EXISTS (SELECT 1 FROM pg_constraint
            WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS
              AND contype='f' AND convalidated AND NOT condeferrable
              AND pg_get_constraintdef(oid)=
                  'FOREIGN KEY (occurrence_id, source_id, artifact_version_id) REFERENCES occurrence(id, source_id, artifact_version_id) ON DELETE RESTRICT') THEN
        RAISE EXCEPTION 'first lexical candidate compact identity constraints differ';
    END IF;
END $$;

DO $$ BEGIN
    IF to_regprocedure('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)') IS NOT NULL THEN
        RAISE EXCEPTION 'first lexical candidate helper already exists';
    END IF;
END $$;

-- Each representation emits its first exact match per provenance identity.
-- All lexical scores here are zero; the parent retains the score/order reduction
-- across mixed representations. Precise native scoring still evaluates hits.
CREATE OR REPLACE FUNCTION public.storage_v2_authorized_lexical_first_candidates(p_occurrence_ids bigint[], p_source_ids bigint[], p_query text)
 RETURNS TABLE(occurrence_id bigint, source_id bigint, artifact_version_id bigint, segment_order bigint, lexical_score real)
 LANGUAGE plpgsql
 STABLE STRICT SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET row_security TO 'on'
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
    v_source_ids BIGINT[];
    v_has_ordinary BOOLEAN;
    v_has_compact BOOLEAN;
    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
BEGIN
    IF cardinality(p_occurrence_ids)=0 OR cardinality(p_source_ids)=0 THEN
        RETURN;
    END IF;
    -- Resolve authorization before inspecting source-local physical metadata.
    SELECT array_agg(source.id ORDER BY source.id) INTO v_source_ids
      FROM public.sources source WHERE source.id=ANY(p_source_ids)
       AND public.storage_v2_can_access_source(source.id,'read');
    IF v_source_ids IS NULL THEN RETURN; END IF;
    SELECT EXISTS(SELECT 1 FROM public.storage_v2_lexical_segment segment
                   WHERE segment.source_id=ANY(v_source_ids)),
           EXISTS(SELECT 1 FROM public.storage_v2_compact_lexical_block block
                   WHERE block.source_id=ANY(v_source_ids))
      INTO v_has_ordinary,v_has_compact;
    IF NOT v_has_ordinary AND NOT v_has_compact THEN RETURN; END IF;
    IF v_has_ordinary THEN
    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED (
        SELECT source.id FROM public.sources source
         WHERE source.id=ANY(v_source_ids)
           AND public.storage_v2_can_access_source(source.id,'read')
    ), requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) AS input(id)
    ), matching AS MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               min(segment.segment_order) AS segment_order
          FROM public.storage_v2_lexical_segment segment
         WHERE segment.fts_vector@@v_query
           AND segment.source_id IN (SELECT id FROM authorized_sources)
           AND segment.source_id=ANY(v_source_ids)
         GROUP BY segment.occurrence_id,segment.source_id,segment.artifact_version_id
    ), eligible AS MATERIALIZED (
        SELECT matching.* FROM matching
          -- Avoid expanding a large requested set when exact native matches are absent.
          WHERE EXISTS (SELECT 1 FROM matching)
            AND EXISTS (SELECT 1 FROM requested WHERE requested.id=matching.occurrence_id)
    )
    SELECT eligible.occurrence_id,eligible.source_id,eligible.artifact_version_id,
           eligible.segment_order,0.0::REAL FROM eligible;
    END IF;

    IF v_has_compact THEN
        IF p_query ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
           AND lower(p_query) !~ '(^|[[:space:]])or([[:space:]]|$)' THEN
            -- A direct fingerprint predicate can use GIN. It is only a
            -- necessary block predicate: full per-segment vectors decide.
            RETURN QUERY
            WITH matching_blocks AS MATERIALIZED (
                SELECT block.* FROM public.storage_v2_compact_lexical_block block
                 WHERE block.source_id=ANY(v_source_ids)
                   AND block.fingerprints @> public.storage_v2_posting_fingerprints(
                       tsvector_to_array(to_tsvector('simple',p_query)))
            ), requested AS MATERIALIZED (
                SELECT id FROM unnest(p_occurrence_ids) input(id)
            ), eligible_blocks AS MATERIALIZED (
                SELECT block.* FROM matching_blocks block
                 WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
            )
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
                   min(item.segment_order),0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query
             GROUP BY block.occurrence_id,block.source_id,block.artifact_version_id;
        ELSE
            -- OR, phrases and negation keep the complete vector predicate.
            RETURN QUERY
            WITH requested AS MATERIALIZED (
                SELECT id FROM unnest(p_occurrence_ids) input(id)
            ), eligible_blocks AS MATERIALIZED (
                SELECT block.* FROM public.storage_v2_compact_lexical_block block
                 WHERE block.source_id=ANY(v_source_ids)
                   AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
            )
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
                   min(item.segment_order),0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query
             GROUP BY block.occurrence_id,block.source_id,block.artifact_version_id;
        END IF;
    END IF;
END
$function$;

ALTER FUNCTION storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text) OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text) TO mainrag_v2_frontier_owner;
DO $$ DECLARE signature TEXT; definition TEXT; first_position INTEGER; last_position INTEGER;
replacement TEXT := $kind$
    ), requested_kind AS MATERIALIZED (
        SELECT unprojected.id, EXISTS (
            SELECT 1 FROM (
                SELECT marker.segment_order
                  FROM public.storage_v2_lexical_segment marker
                 WHERE marker.occurrence_id=unprojected.id AND marker.segment_order=0 OFFSET 0
            ) ordinary
            UNION ALL
            SELECT 1 FROM (
                SELECT marker.block_order
                  FROM public.storage_v2_compact_lexical_block marker
                 WHERE marker.occurrence_id=unprojected.id AND marker.block_order=0 OFFSET 0
            ) compact
        ) AS generated
          FROM unprojected

$kind$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_source_segment_rank_candidates(bigint[],text)',
        'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,
            'public.storage_v2_authorized_lexical_candidates(','')))
            /length('public.storage_v2_authorized_lexical_candidates(')<>1 THEN
            RAISE EXCEPTION 'first lexical candidate call differs: %',signature;
        END IF;
        definition:=replace(definition,'public.storage_v2_authorized_lexical_candidates(',
            'public.storage_v2_authorized_lexical_first_candidates(');
        first_position:=strpos(definition,'    ), requested_kind AS MATERIALIZED (');
        last_position:=strpos(definition,'    ), eligible AS MATERIALIZED (');
        IF first_position=0 OR last_position<=first_position THEN
            RAISE EXCEPTION 'first lexical candidate marker differs: %',signature;
        END IF;
        EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement||
            substring(definition FROM last_position);
    END LOOP;
END $$;
COMMIT;
