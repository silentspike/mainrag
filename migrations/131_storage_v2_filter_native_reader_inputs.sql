-- Resolve sparse physical candidates before broad requested scopes.
-- Keep full vectors, canonical-document checks, source policies and rank ties.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $$ DECLARE signature TEXT; expected TEXT; owner_id REGROLE; caller_id REGROLE; BEGIN
    FOR signature,expected,owner_id IN SELECT * FROM (VALUES
        ('storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)',
         '81519f5b4910efe0208c7a60878cc925043fc6d2dc529114ab899b51c8926f70',
         'mainrag_v2_lexical_rank_owner'::REGROLE),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',
         '90f14f82ffcff081c4d6896a299f14307dd1f8504224e8df468fb4a63adedabb',
         'mainrag_v2_frontier_owner'::REGROLE),
        ('storage_v2_source_segment_presence(bigint[])',
         '6f619ad4bc4cd7608fc090219c3e2701bf3d7f7d271dd5b2a42b638fc8b844b1',
         'mainrag_v2_frontier_owner'::REGROLE)
    ) required(signature,expected,owner_id) LOOP
        caller_id:=CASE WHEN signature='storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'
            THEN 'mainrag_v2_frontier_owner'::REGROLE ELSE 'mainrag'::REGROLE END;
        IF encode(sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8')),'hex')<>expected THEN
            RAISE EXCEPTION 'native reader input definition differs: %',signature;
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
            RAISE EXCEPTION 'native reader input authority differs: %',signature;
        END IF;
    END LOOP;
END $$;

DO $$ DECLARE definition TEXT; first_position INTEGER; last_position INTEGER;
replacement TEXT := $compact$
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
                   item.segment_order,0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query;
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
                   item.segment_order,0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query;
        END IF;
    END IF;
$compact$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
    first_position:=strpos(definition,'    IF v_has_compact THEN');
    last_position:=strpos(definition,E'END\n$function$');
    IF first_position=0 OR last_position<=first_position THEN
        RAISE EXCEPTION 'compact reader input block is unavailable';
    END IF;
    EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement
        ||substring(definition FROM last_position);
END $$;

DO $$ DECLARE definition TEXT; first_position INTEGER; last_position INTEGER;
replacement TEXT := $canonical$
    ), matching_document AS MATERIALIZED (
        -- Keep scope predicates indexable. A CASE around the ID predicate
        -- previously hid a tiny canonical-document set from the planner.
        SELECT document.id FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids)>=1024
           AND cardinality(v_projection_documents)<=8192
           AND document.id=ANY(v_projection_documents)
           AND document.fts_simple@@v_query
        UNION ALL
        SELECT document.id FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids)>=1024
           AND cardinality(v_projection_documents)>8192
           AND document.fts_simple@@v_query
           AND EXISTS (SELECT 1 FROM authorized_projection authorized
                       WHERE authorized.canonical_document_id=document.id)
$canonical$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::REGPROCEDURE);
    first_position:=strpos(definition,'    ), matching_document AS MATERIALIZED (');
    last_position:=strpos(definition,'    ), copied_result AS MATERIALIZED (');
    IF first_position=0 OR last_position<=first_position THEN
        RAISE EXCEPTION 'canonical reader input block is unavailable';
    END IF;
    EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement
        ||substring(definition FROM last_position);
END $$;

DO $$ DECLARE definition TEXT; BEGIN
    definition:=pg_get_functiondef('storage_v2_source_segment_presence(bigint[])'::REGPROCEDURE);
    IF strpos(definition,'FROM storage_v2_legacy_lexical_segment projection')=0 THEN
        RAISE EXCEPTION 'legacy binding presence block is unavailable';
    END IF;
    -- The complete association and source FK already prove payload presence.
    -- The unchanged outer source/occurrence authorization remains mandatory.
    EXECUTE replace(definition,'FROM storage_v2_legacy_lexical_segment projection',
        'FROM storage_v2_legacy_rank_binding projection');
END $$;
COMMIT;
