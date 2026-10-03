-- Evaluate the simple-AND request shape once, and probe immutable segment
-- presence through keys. Existing validated constraints prove block identity
-- and nonempty segment orders without fetching their toasted arrays.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $$ DECLARE signature TEXT; expected TEXT; owner_id REGROLE; callers REGROLE[]; BEGIN
    FOR signature,expected,owner_id,callers IN SELECT * FROM (VALUES
        ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
         '2701a549f0a210b9f4ad6ebea6b7483e528711d9d47effe93048b5802091a0a6',
         'mainrag'::REGROLE,ARRAY['mainrag'::REGROLE,0::OID::REGROLE]),
        ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)',
         '6b6dfbc62add876aa498a3a8beb31c7c68d891206806c3b3a17d250112a266b7',
         'mainrag'::REGROLE,ARRAY['mainrag'::REGROLE]),
        ('storage_v2_source_segment_presence(bigint[])',
         '922d702bfd60605cdfc20c3ac792331bb86e1c4347feb1c434e1cf842d080d2c',
         'mainrag_v2_frontier_owner'::REGROLE,
         ARRAY['mainrag_v2_frontier_owner'::REGROLE,'mainrag'::REGROLE])
    ) required(signature,expected,owner_id,callers) LOOP
        IF encode(sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8')),'hex')<>expected THEN
            RAISE EXCEPTION 'bounded query definition differs: %',signature;
        END IF;
        IF (SELECT proowner FROM pg_proc WHERE oid=signature::REGPROCEDURE)<>owner_id
           OR NOT has_function_privilege(owner_id,signature,'EXECUTE')
           OR EXISTS (SELECT required.role_id FROM unnest(callers) required(role_id)
                WHERE required.role_id<>0::OID AND NOT EXISTS (
                    SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                        aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                     WHERE routine.oid=signature::REGPROCEDURE
                       AND permission.grantee=required.role_id AND permission.privilege_type='EXECUTE'))
           OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                 WHERE routine.oid=signature::REGPROCEDURE
                   AND (NOT permission.grantee=ANY(callers)
                        OR permission.privilege_type<>'EXECUTE'
                        OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
            RAISE EXCEPTION 'bounded query authority differs: %',signature;
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
        RAISE EXCEPTION 'bounded query compact identity constraints differ';
    END IF;
END $$;

DO $$ DECLARE signature TEXT; definition TEXT; BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,'storage_v2_simple_and_query(p_ast)','')))
            /length('storage_v2_simple_and_query(p_ast)')<>10
           OR strpos(definition,E'    v_result JSONB;\n')=0
           OR strpos(definition,E'    WITH RECURSIVE\n')=0 THEN
            RAISE EXCEPTION 'bounded query request block is unavailable';
        END IF;
        definition:=replace(definition,'storage_v2_simple_and_query(p_ast)','v_simple_and_query');
        definition:=replace(definition,E'    v_result JSONB;\n',
            E'    v_result JSONB;\n    v_simple_and_query TEXT;\n');
        definition:=replace(definition,E'    WITH RECURSIVE\n',
            E'    v_simple_and_query:=storage_v2_simple_and_query(p_ast);\n\n    WITH RECURSIVE\n');
        EXECUTE definition;
    END LOOP;
END $$;

DO $$ DECLARE definition TEXT; first_position INTEGER;
replacement TEXT := $presence$
       AND (EXISTS (
               SELECT 1 FROM storage_v2_legacy_rank_binding projection
                WHERE projection.occurrence_id=occurrence_row.id
           ) OR EXISTS (
               SELECT 1 FROM public.storage_v2_lexical_segment segment
                WHERE segment.occurrence_id=occurrence_row.id
                  AND segment.source_id=occurrence_row.source_id
                  AND segment.artifact_version_id=occurrence_row.artifact_version_id
           ) OR EXISTS (
               SELECT 1 FROM public.storage_v2_compact_lexical_block segment
                WHERE segment.occurrence_id=occurrence_row.id
           ));
END
$function$
$presence$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_source_segment_presence(bigint[])'::REGPROCEDURE);
    first_position:=strpos(definition,'       AND (EXISTS (');
    IF first_position=0 THEN
        RAISE EXCEPTION 'bounded query presence block is unavailable';
    END IF;
    EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement;
END $$;
COMMIT;
