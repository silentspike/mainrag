-- Migration 114: generation-bound multi-hop call paths. Traverse only resolved
-- stable identities; unresolved names and hidden targets remain terminal facts.
-- Work and output have separate request bounds, without reading legacy tables.

CREATE OR REPLACE FUNCTION storage_v2_chain_node(
    p_source BIGINT,p_selector TEXT,p_occurrence BIGINT
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off
AS $$
DECLARE v_symbol JSONB; v_card JSONB; v_annotations JSONB;
BEGIN
    v_symbol := storage_v2_symbol_command(p_source,p_selector,'symbols',
        jsonb_build_object('occurrence_id',p_occurrence,'limit',1))->0;
    IF v_symbol IS NULL THEN RETURN NULL; END IF;
    v_card := storage_v2_intelligence_command(p_source,p_selector,'card',
        jsonb_build_object('occurrence_id',p_occurrence,'limit',1))->0;
    -- Structural symbols remain inspectable even without an enrichment card.
    IF v_card IS NULL THEN
        SELECT v_symbol||jsonb_build_object('symbol_id',v_symbol->'id',
            'qualified_name',stable.qualified_name,'signature',visible.signature,
            'visibility',visible.visibility,'source_name',source.name)
          INTO v_card FROM storage_v2_symbol_occurrence visible
          JOIN storage_v2_symbol stable ON stable.id=visible.symbol_id
          JOIN sources source ON source.id=visible.source_id WHERE visible.id=p_occurrence;
    END IF;
    SELECT COALESCE(jsonb_agg(value ORDER BY annotation_type,id),'[]'::JSONB)
      INTO v_annotations FROM (
        SELECT annotation_type,id,jsonb_build_object('annotation_type',annotation_type,
            'value',CASE WHEN jsonb_typeof(value)='string' THEN value#>>'{}' ELSE value::TEXT END,
            'confidence',CASE WHEN jsonb_typeof(provenance->'confidence')='number'
                             THEN provenance->'confidence' END,
            'provenance',provenance,'author_kind',author_kind,
            'profile_id',profile_id,'profile_version',profile_version,
            'metadata_scope',CASE WHEN symbol_occurrence_id IS NULL THEN 'symbol' ELSE 'occurrence' END) value
          FROM storage_v2_symbol_annotation
         WHERE source_id=p_source AND symbol_id=(v_symbol->>'stable_symbol_id')::BIGINT
           AND (symbol_occurrence_id IS NULL OR symbol_occurrence_id=p_occurrence)
         ORDER BY annotation_type,id LIMIT 50
      ) selected;
    RETURN jsonb_build_object('symbol',v_symbol,'card',v_card,'annotations',v_annotations,
        'annotations_complete',jsonb_array_length(v_annotations)<50);
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_intelligence_chain(
    p_source BIGINT,p_selector TEXT,p_query JSONB DEFAULT '{}'::JSONB
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off
AS $$
DECLARE
    v_generation source_generation; v_depth BIGINT := 6; v_limit BIGINT := 100;
    v_direction TEXT := COALESCE(p_query->>'direction','callees');
    v_roots JSONB; v_root JSONB; v_node JSONB; v_target JSONB;
    v_queue JSONB := '[]'; v_index BIGINT := 0; v_work JSONB; v_call JSONB;
    v_calls JSONB; v_targets JSONB; v_path JSONB; v_step JSONB;
    v_entries JSONB := '[]'; v_paths JSONB := '[]'; v_nodes BIGINT := 0;
    v_remaining BIGINT; v_complete BOOLEAN := true; v_cycle BOOLEAN;
    v_next_query JSONB; v_target_key TEXT; v_reason TEXT;
BEGIN
    v_generation := storage_v2_resolve_generation(p_source,p_selector);
    IF p_query IS NULL OR jsonb_typeof(p_query)<>'object'
       OR NULLIF(btrim(p_query->>'name'),'') IS NULL
       OR v_direction NOT IN ('callers','callees') THEN
        RAISE EXCEPTION 'call-chain name and valid direction required' USING ERRCODE='22023';
    END IF;
    IF p_query->'max_depth' IS NOT NULL AND p_query->'max_depth'<>'null'::JSONB THEN
        IF jsonb_typeof(p_query->'max_depth')<>'number' OR (p_query->>'max_depth') !~ '^[0-9]{1,2}$' THEN
            RAISE EXCEPTION 'call-chain depth must be from 1 to 10' USING ERRCODE='22023';
        END IF;
        v_depth := (p_query->>'max_depth')::BIGINT;
    END IF;
    IF p_query->'limit' IS NOT NULL AND p_query->'limit'<>'null'::JSONB THEN
        IF jsonb_typeof(p_query->'limit')<>'number' OR (p_query->>'limit') !~ '^[0-9]{1,3}$' THEN
            RAISE EXCEPTION 'call-chain limit must be from 1 to 200' USING ERRCODE='22023';
        END IF;
        v_limit := (p_query->>'limit')::BIGINT;
    END IF;
    IF v_depth<1 OR v_depth>10 OR v_limit<1 OR v_limit>200 THEN
        RAISE EXCEPTION 'call-chain depth or limit out of range' USING ERRCODE='22023';
    END IF;
    v_roots := storage_v2_symbol_command(p_source,p_selector,'symbols',
        jsonb_build_object('name',p_query->>'name','exact_name',p_query->'exact_name',
                           'occurrence_id',p_query->'occurrence_id',
                           'limit',LEAST(10,v_limit)));
    IF jsonb_array_length(v_roots)=LEAST(10,v_limit) THEN v_complete := false; END IF;
    FOR v_root IN SELECT value FROM jsonb_array_elements(v_roots) LOOP
        v_node := storage_v2_chain_node(p_source,p_selector,-(v_root->>'id')::BIGINT);
        v_queue := v_queue||jsonb_build_array(jsonb_build_object('node',v_node,
            'root',v_node,'steps','[]'::JSONB,'visited',jsonb_build_array(v_root->>'symbol_key')));
        v_nodes := v_nodes+1;
    END LOOP;
    WHILE v_index<jsonb_array_length(v_queue) LOOP
        v_work := v_queue->v_index::INTEGER; v_index := v_index+1;
        v_node := v_work->'node'; v_path := v_work->'steps';
        v_remaining := v_limit-jsonb_array_length(v_entries);
        v_reason := NULL;
        IF jsonb_array_length(v_path)>=v_depth THEN v_reason := 'depth_limit';
        ELSIF v_remaining<=0 THEN v_reason := 'result_limit'; END IF;
        IF v_reason IS NOT NULL THEN
            v_complete := false;
            v_paths := v_paths||jsonb_build_array(jsonb_build_object('root',v_work->'root',
                'steps',v_path,'termination',v_reason));
            CONTINUE;
        END IF;
        v_next_query := CASE WHEN v_direction='callees'
            THEN jsonb_build_object('caller_occurrence_id',-(v_node->'symbol'->>'id')::BIGINT,'limit',v_remaining)
            ELSE jsonb_build_object('callee_symbol_key',v_node->'symbol'->>'symbol_key','limit',v_remaining) END;
        v_calls := storage_v2_symbol_command(p_source,p_selector,v_direction,v_next_query);
        IF jsonb_array_length(v_calls)=v_remaining THEN v_complete := false; END IF;
        IF jsonb_array_length(v_calls)=0 THEN
            v_paths := v_paths||jsonb_build_array(jsonb_build_object('root',v_work->'root',
                'steps',v_path,'termination','leaf'));
        END IF;
        FOR v_call IN SELECT value FROM jsonb_array_elements(v_calls) LOOP
            v_entries := v_entries||jsonb_build_array(v_call||jsonb_build_object(
                'depth',jsonb_array_length(v_path)+1,'from_name',v_call->'caller_name',
                'to_name',v_call->'callee_name','line',COALESCE(NULLIF(v_call->'call_line','null'::JSONB),v_call->'line'),
                'root_symbol_id',v_work->'root'->'symbol'->'id','direction',v_direction));
            v_reason := NULL;
            IF v_call->>'proven'<>'true' THEN v_reason := 'unresolved';
            ELSIF v_nodes>=v_limit THEN v_reason := 'work_limit'; v_complete := false; END IF;
            IF v_reason IS NOT NULL THEN
                v_paths := v_paths||jsonb_build_array(jsonb_build_object('root',v_work->'root',
                    'steps',v_path,'termination',v_reason,'terminal_evidence',v_call));
                CONTINUE;
            END IF;
            v_target_key := CASE WHEN v_direction='callees' THEN v_call->>'callee_symbol_key'
                                ELSE v_call->>'caller_symbol_key' END;
            v_targets := storage_v2_symbol_command(p_source,p_selector,'symbols',
                CASE WHEN v_direction='callees'
                     THEN jsonb_build_object('symbol_key',v_target_key,'limit',LEAST(200,v_limit-v_nodes))
                     ELSE jsonb_build_object('occurrence_id',-(v_call->>'caller_id')::BIGINT,'limit',1) END);
            IF jsonb_array_length(v_targets)=0 THEN
                v_complete := false;
                v_paths := v_paths||jsonb_build_array(jsonb_build_object('root',v_work->'root',
                    'steps',v_path,'termination','target_not_visible','terminal_evidence',v_call));
                CONTINUE;
            END IF;
            IF v_direction='callees' AND jsonb_array_length(v_targets)=v_limit-v_nodes THEN v_complete := false; END IF;
            FOR v_target IN SELECT value FROM jsonb_array_elements(v_targets) LOOP
                v_target := storage_v2_chain_node(p_source,p_selector,-(v_target->>'id')::BIGINT);
                v_step := v_target||jsonb_build_object('edge',v_call);
                v_cycle := (v_work->'visited') ? v_target_key;
                IF v_cycle THEN
                    v_paths := v_paths||jsonb_build_array(jsonb_build_object('root',v_work->'root',
                        'steps',v_path||jsonb_build_array(v_step),'termination','cycle','terminal_evidence',v_call));
                ELSE
                    v_queue := v_queue||jsonb_build_array(jsonb_build_object('root',v_work->'root',
                        'node',v_target,'steps',v_path||jsonb_build_array(v_step),
                        'visited',(v_work->'visited')||to_jsonb(v_target_key)));
                END IF;
                v_nodes := v_nodes+1;
            END LOOP;
        END LOOP;
    END LOOP;
    RETURN jsonb_build_object('entries',v_entries,'paths',v_paths,'roots',v_roots,
        'complete',v_complete,'work_nodes',v_nodes,'max_depth',v_depth,'limit',v_limit,
        'source_id',p_source,'generation_seq',v_generation.generation_seq);
END
$$;

DO $$
DECLARE v_owner TEXT; v_signature TEXT;
BEGIN
 SELECT pg_get_userbyid(proowner) INTO v_owner FROM pg_proc
  WHERE oid='storage_v2_intelligence_command(bigint,text,text,jsonb)'::REGPROCEDURE;
 FOREACH v_signature IN ARRAY ARRAY['storage_v2_chain_node(bigint,text,bigint)',
                                    'storage_v2_intelligence_chain(bigint,text,jsonb)'] LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',v_signature,v_owner);
  EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',v_signature);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO mainrag',v_signature);
 END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_intelligence_command(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_command TEXT,
    p_query JSONB DEFAULT '{}'::JSONB
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_symbol_key TEXT;
    v_name TEXT;
    v_result JSONB;
    v_limit BIGINT;
BEGIN
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);
    IF p_command IN ('symbols', 'callers', 'callees') THEN
        RETURN storage_v2_symbol_command(p_source_id,p_generation_selector,p_command,p_query);
    ELSIF p_command IN ('card', 'layers') THEN
        -- A missing/null limit retains complete named-generation reads. Runtime
        -- callers supply a bounded limit; reject malformed values, never coerce.
        IF p_query -> 'limit' IS NOT NULL AND p_query -> 'limit' <> 'null'::JSONB THEN
            IF jsonb_typeof(p_query -> 'limit') <> 'number'
               OR (p_query ->> 'limit') !~ '^[0-9]{1,3}$' THEN
                RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                    USING ERRCODE = '22023';
            END IF;
            v_limit := (p_query ->> 'limit')::BIGINT;
            IF v_limit < 1 OR v_limit > 200 THEN
                RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                    USING ERRCODE = '22023';
            END IF;
        END IF;
        SELECT COALESCE(jsonb_agg(value ORDER BY value ->> 'symbol_key',
                                                value ->> 'analysis_profile_id',
                                                item_key, occurrence_id), '[]'::JSONB)
          INTO v_result
          FROM (
            SELECT jsonb_build_object(
                'symbol_key', stable_symbol.symbol_key, 'language', stable_symbol.language,
                'symbol_id', -visible.id,
                'name', COALESCE(card.generic_card->>'name',stable_symbol.qualified_name),
                'symbol_type', stable_symbol.symbol_kind,
                'file_path', COALESCE(artifact.witness->>'path',source_item.item_key),
                'line_start', visible.source_span->'line_start',
                'line_end', visible.source_span->'line_end',
                'source_name', (SELECT name FROM sources WHERE id=p_source_id),
                'layer', card.domain_fields->'layer',
                'side_effect_type', card.domain_fields->'side_effect',
                'affected_resource', card.domain_fields->'resource',
                'delegation_targets', CASE WHEN card.domain_fields->>'delegation_target'<>'unknown'
                                          THEN card.domain_fields->'delegation_target' END,
                'domain_profile', card.domain_profile_id,
                'symbol_kind', stable_symbol.symbol_kind, 'qualified_name', stable_symbol.qualified_name,
                'item_key', source_item.item_key, 'content_hash', artifact.expected_content_hash,
                'signature', visible.signature, 'documentation', visible.documentation,
                'visibility', visible.visibility, 'structure', visible.structure,
                'source_span', visible.source_span, 'analysis_profile_id', card.analysis_profile_id,
                'domain_profile_id', card.domain_profile_id,
                'domain_profile_version', card.domain_profile_version,
                'generic_card', card.generic_card, 'domain_fields', card.domain_fields,
                'field_provenance', card.field_provenance
            ) AS value, source_item.item_key, visible.id AS occurrence_id
              FROM storage_v2_symbol_occurrence visible
              JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = visible.symbol_id
              JOIN artifact_version artifact ON artifact.id = visible.artifact_version_id
              JOIN source_item ON source_item.id = artifact.item_id
              JOIN generation_item_version membership
                ON membership.source_id = p_source_id
               AND membership.source_item_id = artifact.item_id
               AND membership.artifact_version_id = artifact.id
              JOIN storage_v2_symbol_card card ON card.symbol_occurrence_id = visible.id
             WHERE visible.source_id = p_source_id
               AND membership.valid_from_seq <= v_generation.generation_seq
               AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
               AND (p_query->>'occurrence_id' IS NULL OR visible.id=(p_query->>'occurrence_id')::BIGINT)
               AND (COALESCE(p_query ->> 'name', '') = ''
                    OR card.generic_card ->> 'name' ILIKE '%' || (p_query ->> 'name') || '%')
               AND (COALESCE(p_query ->> 'layer', '') = ''
                    OR card.domain_fields ->> 'layer' = p_query ->> 'layer')
               AND (COALESCE(p_query ->> 'resource', '') = ''
                    OR card.domain_fields ->> 'resource' = p_query ->> 'resource')
               AND (COALESCE(p_query ->> 'side_effect', '') = ''
                    OR card.domain_fields ->> 'side_effect' = p_query ->> 'side_effect')
             ORDER BY stable_symbol.symbol_key, card.analysis_profile_id,
                      source_item.item_key, visible.id
             LIMIT v_limit
          ) cards;
        RETURN v_result;
    ELSIF p_command = 'explain' THEN
        RETURN storage_v2_intelligence_chain(p_source_id,p_generation_selector,p_query);
    ELSIF p_command = 'ownership' THEN
        RETURN storage_v2_ownership_command(p_source_id,p_generation_selector,p_query->>'name',
            COALESCE((p_query->>'limit')::BIGINT,50));
    END IF;
    RAISE EXCEPTION 'unsupported storage-v2 intelligence command';
END
$$;



CREATE OR REPLACE FUNCTION storage_v2_active_intelligence_command(
    p_manifest_sha256 TEXT,
    p_command TEXT,
    p_query JSONB DEFAULT '{}'::JSONB,
    p_source_id BIGINT DEFAULT NULL,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source RECORD;
    v_results JSONB := '[]'::JSONB;
    v_remaining BIGINT;
    v_query JSONB := p_query;
    v_value JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    IF p_command IS NULL OR p_command NOT IN ('card', 'explain', 'layers', 'ownership', 'symbols', 'callers', 'callees')
       OR p_query IS NULL OR jsonb_typeof(p_query) <> 'object'
       OR p_include_test IS NULL
       OR (p_source_id IS NOT NULL AND p_source_id <= 0) THEN
        RAISE EXCEPTION 'valid active intelligence request required';
    END IF;
    IF p_include_test AND NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'test scope requires administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_source_id IS NOT NULL THEN
        PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
    END IF;
    IF p_command IN ('symbols', 'callers', 'callees', 'ownership', 'explain')
       AND (p_query -> 'limit' IS NULL OR p_query -> 'limit' = 'null'::JSONB) THEN
        v_remaining := CASE WHEN p_command='explain' THEN 100 ELSE 50 END;
        v_query := jsonb_set(p_query, '{limit}', to_jsonb(v_remaining));
    END IF;
    IF p_command IN ('card', 'layers', 'symbols', 'callers', 'callees', 'ownership', 'explain') AND p_query -> 'limit' IS NOT NULL
       AND p_query -> 'limit' <> 'null'::JSONB THEN
        IF jsonb_typeof(p_query -> 'limit') <> 'number'
           OR (p_query ->> 'limit') !~ '^[0-9]{1,3}$' THEN
            RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                USING ERRCODE = '22023';
        END IF;
        v_remaining := (p_query ->> 'limit')::BIGINT;
        IF v_remaining < 1 OR v_remaining > 200 THEN
            RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                USING ERRCODE = '22023';
        END IF;
    END IF;
    FOR v_source IN
        SELECT source.id, source.is_test FROM sources source
         WHERE (p_source_id IS NULL OR source.id = p_source_id)
           AND (p_include_test OR NOT source.is_test)
         ORDER BY source.id
    LOOP
        IF storage_v2_can_access_source(v_source.id, 'read') THEN
            IF v_remaining = 0 THEN
                -- Preserve authorized source envelopes without reading another
                -- card collection after the request-wide budget is exhausted.
                v_value := CASE WHEN p_command='explain' THEN jsonb_build_object(
                    'roots','[]'::JSONB,'entries','[]'::JSONB,'paths','[]'::JSONB,
                    'complete',false,'work_nodes',0,'termination','result_limit') ELSE '[]'::JSONB END;
            ELSE
                IF v_remaining IS NOT NULL THEN
                    v_query := jsonb_set(p_query, '{limit}', to_jsonb(v_remaining));
                END IF;
                v_value := storage_v2_intelligence_command(
                    v_source.id, 'current', p_command, v_query
                );
                IF v_remaining IS NOT NULL THEN
                    v_remaining := v_remaining - CASE WHEN p_command='explain'
                        THEN GREATEST(jsonb_array_length(v_value->'entries'),(v_value->>'work_nodes')::BIGINT)
                        ELSE jsonb_array_length(v_value) END;
                END IF;
            END IF;
            v_results := v_results || jsonb_build_array(jsonb_build_object(
                'source_id', v_source.id, 'value', v_value
            ));
        END IF;
    END LOOP;
    RETURN jsonb_build_object(
        'read_path', 'storage_v2_active',
        'activation_manifest_sha256', p_manifest_sha256,
        'command', p_command,
        'source_count', jsonb_array_length(v_results),
        'results', v_results
    );
END
$$;
