-- Migration 043: use the complete component key when reusing search documents
--
-- IS NOT DISTINCT FROM on a non-null component parameter cannot supply a btree
-- equality condition. On a populated corpus the former reuse lookup therefore
-- scanned documents for every item, even when no materialization was needed.
-- Separate body/node lookups retain null-safe identity and collision checks
-- while using the existing unique component index, including generic plans.

CREATE OR REPLACE FUNCTION storage_v2_put_search_document(
    p_profile_id TEXT,
    p_component_kind TEXT,
    p_component_id BIGINT,
    p_search_text TEXT,
    p_exact_identifiers TEXT[] DEFAULT ARRAY[]::TEXT[]
) RETURNS storage_v2_search_document
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_component_digest BYTEA;
    v_exact TEXT[];
    v_token_count BIGINT;
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

    SELECT COUNT(*) INTO v_token_count
      FROM regexp_split_to_table(lower(p_search_text), '[^[:alnum:]_]+') AS token
     WHERE token <> '';
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

    INSERT INTO storage_v2_search_posting(document_id, term, term_frequency)
    SELECT v_document.id, token, COUNT(*)
      FROM (
          SELECT token
            FROM regexp_split_to_table(lower(p_search_text), '[^[:alnum:]_]+') AS token
           WHERE token <> ''
          UNION ALL
          SELECT token
            FROM regexp_split_to_table(lower(p_search_text), '[[:space:]]+') AS token
           WHERE token <> '' AND token !~ '^[[:alnum:]_]+$'
             AND token ~ '[[:alnum:]_]'
      ) searchable_tokens
     GROUP BY token;
    RETURN v_document;
END
$$;
