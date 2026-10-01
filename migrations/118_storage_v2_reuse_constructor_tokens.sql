-- Reuse complete word and punctuation-preserving token frequencies.
-- Materialization identity, token counts and compact block contents are retained.
BEGIN;
DO $guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_put_search_document(text,text,bigint,text,text[])'::regprocedure),'UTF8')),'hex') NOT IN ('b854c507104b0888d21ce202523bc60e7c1e2a38141955298465ae3a162b4e4a','a7fb6a8087ab4df30f13c06cbb3a498b341c25911b8b97d95b1c09b7403a34ee') THEN RAISE EXCEPTION 'search constructor identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_put_search_document(text,text,bigint,text,text[])'::regprocedure)<>'mainrag'::regrole THEN RAISE EXCEPTION 'search constructor authority differs'; END IF;
END $guard$;
CREATE OR REPLACE FUNCTION public.storage_v2_put_search_document(p_profile_id text, p_component_kind text, p_component_id bigint, p_search_text text, p_exact_identifiers text[] DEFAULT ARRAY[]::text[])
 RETURNS storage_v2_search_document
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'off'
AS $function$
DECLARE
    v_component_digest BYTEA;
    v_exact TEXT[];
    v_token_count BIGINT;
    v_terms TEXT[];
    v_frequencies BIGINT[];
    v_normalized_text TEXT;
    v_hash BYTEA;
    v_document storage_v2_search_document;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'search-document writes require administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_profile_id IS NULL OR p_profile_id = ''
       OR p_component_kind NOT IN ('body', 'node')
       OR p_component_id IS NULL OR p_search_text IS NULL
       OR p_exact_identifiers IS NULL THEN
        RAISE EXCEPTION 'valid search-document materialization required';
    END IF;
    IF p_component_kind = 'body' THEN
        SELECT digest INTO v_component_digest FROM content_body WHERE id = p_component_id;
    ELSE
        SELECT node_digest INTO v_component_digest FROM content_node WHERE id = p_component_id;
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'search-document component not found'; END IF;

    SELECT COALESCE(array_agg(value ORDER BY value), ARRAY[]::TEXT[])
      INTO v_exact
      FROM (
          SELECT DISTINCT lower(btrim(identifier)) AS value
            FROM unnest(p_exact_identifiers) AS identifier
           WHERE btrim(identifier) <> ''
      ) normalized;

    IF p_component_kind = 'body' THEN
        SELECT * INTO v_document
          FROM storage_v2_search_document
         WHERE profile_id = p_profile_id AND component_kind = 'body'
           AND body_id = p_component_id AND node_id IS NULL;
    ELSE
        SELECT * INTO v_document
          FROM storage_v2_search_document
         WHERE profile_id = p_profile_id AND component_kind = 'node'
           AND body_id IS NULL AND node_id = p_component_id;
    END IF;
    IF FOUND THEN
        IF (v_document.search_text, v_document.exact_identifiers)
           IS DISTINCT FROM (p_search_text, v_exact) THEN
            RAISE EXCEPTION 'search-document profile collision' USING ERRCODE = '22000';
        END IF;
        RETURN v_document;
    END IF;

    -- Tokenize each class once. The word class alone contributes to the
    -- document token count; punctuation-preserving terms still contribute to
    -- exact postings. Both classes retain the established tokenizer grammar.
    v_normalized_text := lower(p_search_text);
    WITH searchable_tokens AS (
        SELECT token,1::BIGINT AS word_count
          FROM regexp_split_to_table(v_normalized_text, '[^[:alnum:]_]+') token
         WHERE token <> ''
        UNION ALL
        SELECT token,0::BIGINT
          FROM regexp_split_to_table(v_normalized_text, '[[:space:]]+') token
         WHERE token <> '' AND token !~ '^[[:alnum:]_]+$'
           AND token ~ '[[:alnum:]_]'
    ), frequency AS (
        SELECT token,COUNT(*)::BIGINT AS frequency,SUM(word_count)::BIGINT AS word_count
          FROM searchable_tokens GROUP BY token
    )
    SELECT COALESCE(SUM(word_count),0)::BIGINT,
           COALESCE(array_agg(token ORDER BY token COLLATE "C"),ARRAY[]::TEXT[]),
           COALESCE(array_agg(frequency ORDER BY token COLLATE "C"),ARRAY[]::BIGINT[])
      INTO v_token_count,v_terms,v_frequencies FROM frequency;
    v_hash := storage_v2_hash_parts('mainrag.search-document.v1', ARRAY[
        convert_to(p_profile_id, 'UTF8'), convert_to(p_component_kind, 'UTF8'),
        v_component_digest, convert_to(p_search_text, 'UTF8'),
        convert_to(array_to_string(v_exact, E'\n'), 'UTF8')
    ]);

    INSERT INTO storage_v2_search_document(
        profile_id, component_kind, body_id, node_id, search_text, token_count,
        exact_identifiers, materialization_sha256
    ) VALUES (
        p_profile_id, p_component_kind,
        CASE WHEN p_component_kind = 'body' THEN p_component_id END,
        CASE WHEN p_component_kind = 'node' THEN p_component_id END,
        p_search_text, v_token_count, v_exact, v_hash
    ) ON CONFLICT ON CONSTRAINT uq_storage_v2_search_document_component DO NOTHING
    RETURNING * INTO v_document;
    IF NOT FOUND THEN
        IF p_component_kind = 'body' THEN
            SELECT * INTO STRICT v_document
              FROM storage_v2_search_document
             WHERE profile_id = p_profile_id AND component_kind = 'body'
               AND body_id = p_component_id AND node_id IS NULL;
        ELSE
            SELECT * INTO STRICT v_document
              FROM storage_v2_search_document
             WHERE profile_id = p_profile_id AND component_kind = 'node'
               AND body_id IS NULL AND node_id = p_component_id;
        END IF;
        IF (v_document.search_text, v_document.exact_identifiers)
           IS DISTINCT FROM (p_search_text, v_exact) THEN
            RAISE EXCEPTION 'search-document profile collision' USING ERRCODE = '22000';
        END IF;
        RETURN v_document;
    END IF;

    -- Aligned arrays are already in the canonical C order. Reuse that order
    -- to form the same complete blocks without retokenizing or a second sort.
    INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies)
    SELECT v_document.id,(posting.ordinal-1)/256,
           array_agg(posting.term ORDER BY posting.ordinal),
           array_agg(posting.frequency ORDER BY posting.ordinal)
      FROM unnest(v_terms,v_frequencies) WITH ORDINALITY posting(term,frequency,ordinal)
     GROUP BY (posting.ordinal-1)/256;
    RETURN v_document;
END
$function$
;
COMMIT;
