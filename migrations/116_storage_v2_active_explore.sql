-- Migration 116: bounded Explore in one PostgreSQL statement snapshot.
-- Exact occurrence roots prevent overloads and same-name sources from being
-- substituted during traversal. Limits are visible, never reported as complete.

CREATE OR REPLACE FUNCTION storage_v2_note_matches_card(p_note JSONB,p_card JSONB)
RETURNS BOOLEAN LANGUAGE SQL IMMUTABLE SET search_path=pg_catalog,public AS $$
    SELECT COALESCE(
        (p_note->'read_provenance'->>'source_id' IS NULL
         OR p_note->'read_provenance'->>'source_id'=p_card->>'source_id')
        AND CASE p_note->'read_provenance'->>'symbols_namespace'
            WHEN 'storage_v2_symbol_key' THEN jsonb_typeof(p_note->'symbols')='array'
                AND p_note->'symbols' ? (p_card->>'symbol_key')
            WHEN 'legacy_symbol_reference' THEN
                (jsonb_typeof(p_note->'symbols')='array' AND p_note->'symbols' ? (p_card->>'name'))
                OR p_note->>'path_description'=p_card->>'name'
                OR (jsonb_typeof(p_note->'symbols')='array' AND p_note->'symbols' ? (p_card->>'qualified_name'))
            WHEN 'user_symbol_reference' THEN
                (jsonb_typeof(p_note->'symbols')='array' AND p_note->'symbols' ? (p_card->>'name'))
                OR p_note->>'path_description'=p_card->>'name'
                OR (jsonb_typeof(p_note->'symbols')='array' AND p_note->'symbols' ? (p_card->>'qualified_name'))
            ELSE false END,false)
$$;

CREATE OR REPLACE FUNCTION storage_v2_active_explore(p_manifest TEXT,p_query JSONB,p_source BIGINT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_queries JSONB; v_term TEXT; v_response JSONB; v_source JSONB; v_card JSONB;
    v_cards JSONB:='[]'; v_notes JSONB; v_seeds JSONB; v_seed JSONB; v_chain JSONB;
    v_chains JSONB:='[]'; v_card_budget BIGINT:=100; v_chain_budget BIGINT:=100; v_term_limit BIGINT;
    v_cards_complete BOOLEAN:=true; v_chains_complete BOOLEAN:=true;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest);
    IF p_query IS NULL OR jsonb_typeof(p_query)<>'object'
       OR jsonb_typeof(p_query->'concept') IS DISTINCT FROM 'string'
       OR NULLIF(btrim(p_query->>'concept'),'') IS NULL OR octet_length(p_query::TEXT)>1048576
       OR jsonb_typeof(p_query->'queries') IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_query->'queries') NOT BETWEEN 1 AND 12
       OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_query->'queries') term
                  WHERE jsonb_typeof(term)<>'string' OR NULLIF(btrim(term#>>'{}'),'') IS NULL
                     OR octet_length(term#>>'{}')>8192)
       OR jsonb_typeof(p_query->'operation_symbols') IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_query->'operation_symbols')>100
       OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_query->'operation_symbols') sym
                  WHERE jsonb_typeof(sym)<>'string') THEN
        RAISE EXCEPTION 'bounded Explore query and expansions required' USING ERRCODE='22023';
    END IF;
    IF p_query->'intent' IS NOT NULL AND jsonb_typeof(p_query->'intent') NOT IN ('string','null') THEN
        RAISE EXCEPTION 'invalid Explore intent' USING ERRCODE='22023';
    END IF;
    IF p_source IS NOT NULL THEN
        IF NOT storage_v2_can_access_source(p_source,'read') THEN
            RAISE EXCEPTION 'Explore source access denied' USING ERRCODE='42501';
        END IF;
        PERFORM storage_v2_require_test_scope(p_source,false);
    END IF;
    v_queries:=p_query->'queries';
    v_notes:=storage_v2_search_intelligence_notes(p_manifest,p_query->>'concept',p_source,20);
    FOR v_term IN SELECT value FROM jsonb_array_elements_text(v_queries) LOOP
        IF v_card_budget=0 THEN v_cards_complete:=false; EXIT; END IF;
        v_term_limit:=LEAST(10,v_card_budget);
        v_response:=storage_v2_active_intelligence_command(p_manifest,'card',
            jsonb_build_object('name',v_term,'limit',v_term_limit),p_source,false);
        FOR v_source IN SELECT value FROM jsonb_array_elements(v_response->'results') LOOP
            FOR v_card IN SELECT value FROM jsonb_array_elements(v_source->'value') LOOP
                v_cards:=v_cards||jsonb_build_array(v_card||jsonb_build_object('source_id',v_source->'source_id'));
                v_card_budget:=v_card_budget-1;
            END LOOP;
        END LOOP;
        IF (SELECT sum(jsonb_array_length(value->'value')) FROM jsonb_array_elements(v_response->'results'))
            =v_term_limit THEN v_cards_complete:=false; END IF;
    END LOOP;
    -- Rank intent/operation matches, delegation candidates, then other cards.
    -- Retain one known dead end at the end rather than silently discarding it.
    WITH distinct_cards AS (
        SELECT DISTINCT ON (value->>'symbol_id') value
          FROM jsonb_array_elements(v_cards)
         ORDER BY value->>'symbol_id',value->>'analysis_profile_id',value::TEXT
    ), ranked AS (
        SELECT value, EXISTS(SELECT 1 FROM jsonb_array_elements(v_notes) note
            WHERE storage_v2_note_matches_card(note,value)) dead_end,
            CASE WHEN value->>'side_effect_type'=p_query->>'intent'
                      AND p_query->'operation_symbols' ? lower(value->>'name') THEN 0
                 WHEN value->>'side_effect_type'=p_query->>'intent' THEN 1
                 WHEN value->'delegation_targets' IS NOT NULL
                      AND value->'delegation_targets'<>'null'::JSONB
                      AND value->'delegation_targets'<>'[]'::JSONB THEN 2 ELSE 3 END priority,
            CASE WHEN jsonb_typeof(value->'classification_confidence')='number'
                 THEN (value->>'classification_confidence')::NUMERIC END confidence
          FROM distinct_cards
    ), selected AS (
        (SELECT * FROM ranked WHERE NOT dead_end
          ORDER BY priority,confidence DESC NULLS LAST,(value->>'source_id')::BIGINT,
            (value->>'symbol_id')::BIGINT LIMIT 3)
        UNION ALL
        (SELECT * FROM ranked WHERE dead_end
          ORDER BY priority,confidence DESC NULLS LAST,(value->>'source_id')::BIGINT,
            (value->>'symbol_id')::BIGINT LIMIT 1)
    ) SELECT COALESCE(jsonb_agg(value ORDER BY dead_end,priority,confidence DESC NULLS LAST,
        (value->>'source_id')::BIGINT,(value->>'symbol_id')::BIGINT),'[]'::JSONB) INTO v_seeds FROM selected;
    FOR v_seed IN SELECT value FROM jsonb_array_elements(v_seeds) LOOP
        IF v_chain_budget=0 THEN
            v_chains_complete:=false;
            v_chain:=jsonb_build_object('generation_seq',NULL,'paths',jsonb_build_array(jsonb_build_object(
                'root',jsonb_build_object('card',v_seed,'symbol',jsonb_build_object('symbol_key',v_seed->'symbol_key'),
                    'annotations','[]'::JSONB,'annotations_complete',false),
                'steps','[]'::JSONB,'termination','result_limit')),
                'entries','[]'::JSONB,'work_nodes',0,'complete',false);
        ELSE
            v_chain:=storage_v2_intelligence_chain((v_seed->>'source_id')::BIGINT,'current',
                jsonb_build_object('name',v_seed->>'name','occurrence_id',-(v_seed->>'symbol_id')::BIGINT,
                    'exact_name',true,'max_depth',6,'limit',v_chain_budget));
            v_chain_budget:=v_chain_budget-GREATEST(jsonb_array_length(v_chain->'entries'),
                (v_chain->>'work_nodes')::BIGINT);
            v_chains_complete:=v_chains_complete AND (v_chain->>'complete')::BOOLEAN;
        END IF;
        v_chains:=v_chains||jsonb_build_array(jsonb_build_object('source_id',v_seed->'source_id','value',v_chain));
    END LOOP;
    RETURN jsonb_build_object('read_path','storage_v2_active','activation_manifest_sha256',p_manifest,
        'results',v_chains,'negative_evidence',v_notes,'candidate_count',jsonb_array_length(v_seeds),
        'cards_complete',v_cards_complete,'chains_complete',v_chains_complete,
        'card_rows_read',100-v_card_budget,'chain_work',100-v_chain_budget);
END $$;

DO $$
DECLARE v_owner TEXT; v_signature TEXT;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO v_owner FROM pg_proc
        WHERE oid='storage_v2_intelligence_command(bigint,text,text,jsonb)'::REGPROCEDURE;
    FOREACH v_signature IN ARRAY ARRAY['storage_v2_note_matches_card(jsonb,jsonb)',
        'storage_v2_active_explore(text,jsonb,bigint)'] LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I',v_signature,v_owner);
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',v_signature);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO mainrag',v_signature);
    END LOOP;
END $$;
