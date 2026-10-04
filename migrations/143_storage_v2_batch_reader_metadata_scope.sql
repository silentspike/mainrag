-- Batch large unfiltered metadata scopes and bound legacy provenance lookups.
-- Keep source authorization, complete corpus statistics and scoring unchanged.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE signature TEXT; expected TEXT; owner_id REGROLE; callers REGROLE[];
BEGIN
    FOR signature,expected,owner_id,callers IN SELECT * FROM (VALUES
        ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
         'a14bee41ca444dc29d28796095c269ba0ef0e6f95ae6120037db6c804134026d',
         'mainrag'::REGROLE,ARRAY['mainrag'::REGROLE,0::OID::REGROLE]),
        ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)',
         '2a39e9e98d01e41d39a78c9d949a848dc2e21ca4eab768c8ac9d2362bca10725',
         'mainrag'::REGROLE,ARRAY['mainrag'::REGROLE]),
        ('storage_v2_source_legacy_segment_matches(bigint,text)',
         '0ed4eaaaeaa70be3d838bd16d2a49339452d0b189f411e06564603b72ecce5d3',
         'mainrag_v2_frontier_owner'::REGROLE,
         ARRAY['mainrag_v2_frontier_owner'::REGROLE,'mainrag'::REGROLE])
    ) required(signature,expected,owner_id,callers) LOOP
        IF encode(sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8')),'hex')<>expected THEN
            RAISE EXCEPTION 'batched metadata reader definition differs: %',signature;
        END IF;
        IF (SELECT proowner FROM pg_proc WHERE oid=signature::REGPROCEDURE)<>owner_id
           OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                WHERE routine.oid=signature::REGPROCEDURE
                  AND (NOT permission.grantee=ANY(callers)
                    OR permission.privilege_type<>'EXECUTE'
                    OR (permission.is_grantable AND permission.grantee<>routine.proowner)))
           OR EXISTS (SELECT 1 FROM unnest(callers) required(role_id)
                WHERE NOT EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                    aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                    WHERE routine.oid=signature::REGPROCEDURE
                      AND permission.grantee=required.role_id AND permission.privilege_type='EXECUTE')) THEN
            RAISE EXCEPTION 'batched metadata reader authority differs: %',signature;
        END IF;
    END LOOP;
END $guard$;

DO $scope$
DECLARE signature TEXT; definition TEXT; replacement TEXT;
old_scope TEXT := $old$    scoped_binding AS MATERIALIZED (
        SELECT visible.id AS occurrence_id,
               binding.ordinal AS component_ordinal, binding.document_id,
               binding.role_weight, document.token_count
          FROM visible_occurrence visible
          JOIN storage_v2_search_view_document binding ON binding.view_id = visible.view_id
          JOIN storage_v2_search_document document ON document.id = binding.document_id
    ),$old$;
new_scope TEXT := $new$    scoped_binding AS MATERIALIZED (
        SELECT visible.id AS occurrence_id,
               binding.ordinal AS component_ordinal, binding.document_id,
               binding.role_weight, document.token_count
          FROM visible_occurrence visible
          JOIN storage_v2_search_view_document binding ON binding.view_id = visible.view_id
          JOIN storage_v2_search_document document ON document.id = binding.document_id
         WHERE NOT v_batch_metadata
        UNION ALL
        SELECT visible.id AS occurrence_id,
               binding.ordinal AS component_ordinal, binding.document_id,
               binding.role_weight, document.token_count
          FROM visible_occurrence visible
          JOIN (SELECT view_id,ordinal,document_id,role_weight
                  FROM storage_v2_search_view_document OFFSET 0) binding
            ON binding.view_id = visible.view_id
          JOIN (SELECT id,token_count FROM storage_v2_search_document OFFSET 0) document
            ON document.id = binding.document_id
         WHERE v_batch_metadata
    ),$new$;
old_start TEXT := $old$    v_simple_and_query:=storage_v2_simple_and_query(p_ast);$old$;
new_exact TEXT := $new$    v_simple_and_query:=storage_v2_simple_and_query(p_ast);
    v_batch_metadata:=COALESCE(v_generation.item_count,0)>=4096
        AND NOT (p_filters ?| ARRAY['path_prefix','role','occurred_from','occurred_to']);$new$;
new_active TEXT := $new$    v_simple_and_query:=storage_v2_simple_and_query(p_ast);
    IF NOT (p_filters ?| ARRAY['path_prefix','role','occurred_from','occurred_to']) THEN
        SELECT COALESCE(sum(generation.item_count),0)>=4096 INTO v_batch_metadata
          FROM sources source JOIN logical_source pointer ON pointer.id=source.id
          JOIN source_generation generation ON generation.id=pointer.active_generation_id
           AND generation.source_id=source.id AND generation.status='active'
         WHERE (p_source_id IS NULL OR source.id=p_source_id)
           AND storage_v2_can_access_source(source.id,'read')
           AND (p_include_test OR NOT source.is_test);
    END IF;$new$;
marker TEXT;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        FOREACH marker IN ARRAY ARRAY[old_scope,old_start,'    v_simple_and_query TEXT;'] LOOP
            IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
                RAISE EXCEPTION 'batched metadata reader boundary differs: %',signature;
            END IF;
        END LOOP;
        replacement:=CASE WHEN signature LIKE 'storage_v2_search_exact(%'
            THEN new_exact ELSE new_active END;
        definition:=replace(definition,'    v_simple_and_query TEXT;',
            E'    v_simple_and_query TEXT;\n    v_batch_metadata BOOLEAN := FALSE;');
        definition:=replace(definition,old_start,replacement);
        definition:=replace(definition,old_scope,new_scope);
        EXECUTE definition;
    END LOOP;
END $scope$;

DO $legacy$
DECLARE definition TEXT;
old_metadata TEXT := $old$        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
        JOIN storage_v2_search_view_document binding
          ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
        JOIN storage_v2_search_document document ON document.id=binding.document_id
         AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id$old$;
new_metadata TEXT := $new$        SELECT 1 FROM (
            SELECT row.* FROM occurrence row WHERE row.id=p_occurrence_id OFFSET 0
        ) occurrence_row
        JOIN LATERAL (
            SELECT row.* FROM artifact_version row
             WHERE row.id=occurrence_row.artifact_version_id OFFSET 0
        ) artifact ON TRUE
        JOIN LATERAL (
            SELECT row.* FROM storage_v2_search_view_document row
             WHERE row.view_id=occurrence_row.view_id AND row.ordinal=0 OFFSET 0
        ) binding ON TRUE
        JOIN LATERAL (
            SELECT row.* FROM storage_v2_search_document row
             WHERE row.id=binding.document_id AND row.component_kind='node'
               AND row.node_id=artifact.content_root_node_id OFFSET 0
        ) document ON TRUE$new$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_source_legacy_segment_matches(bigint,text)'::REGPROCEDURE);
    IF (length(definition)-length(replace(definition,old_metadata,'')))/length(old_metadata)<>1 THEN
        RAISE EXCEPTION 'batched legacy metadata boundary differs';
    END IF;
    EXECUTE replace(definition,old_metadata,new_metadata);
END $legacy$;
COMMIT;
