-- Migration 042: keep oversized search documents complete and searchable
--
-- PostgreSQL limits one tsvector value to 1 MiB. Source text, exact values,
-- and term postings remain unbounded and authoritative; only the optional
-- phrase accelerator may be absent. Oversized documents use a complete,
-- token-boundary fallback for phrase evaluation instead of truncating input.

CREATE OR REPLACE FUNCTION storage_v2_safe_tsvector(p_search_text TEXT)
RETURNS TSVECTOR
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, public
AS $$
BEGIN
    RETURN to_tsvector('simple'::REGCONFIG, p_search_text);
EXCEPTION
    WHEN program_limit_exceeded THEN
        RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_normalize_phrase_text(p_search_text TEXT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, public
AS $$
    SELECT chr(31)
        || COALESCE(string_agg(token, chr(31)), '')
        || chr(31)
      FROM regexp_split_to_table(lower(p_search_text), '[^[:alnum:]_]+') AS token
     WHERE token <> ''
$$;

CREATE OR REPLACE FUNCTION storage_v2_phrase_matches(
    p_fts_simple TSVECTOR,
    p_search_text TEXT,
    p_phrase TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_normalized_phrase TEXT;
BEGIN
    IF p_search_text IS NULL OR p_phrase IS NULL THEN
        RETURN FALSE;
    END IF;
    IF p_fts_simple IS NOT NULL THEN
        RETURN p_fts_simple @@ phraseto_tsquery('simple'::REGCONFIG, p_phrase);
    END IF;
    v_normalized_phrase := storage_v2_normalize_phrase_text(p_phrase);
    IF v_normalized_phrase = chr(31) || chr(31) THEN
        RETURN FALSE;
    END IF;
    RETURN strpos(
        storage_v2_normalize_phrase_text(p_search_text),
        v_normalized_phrase
    ) > 0;
END
$$;

DO $$
DECLARE
    v_expression TEXT;
BEGIN
    SELECT pg_get_expr(attribute_default.adbin, attribute_default.adrelid)
      INTO v_expression
      FROM pg_attribute attribute_row
      JOIN pg_attrdef attribute_default
        ON attribute_default.adrelid = attribute_row.attrelid
       AND attribute_default.adnum = attribute_row.attnum
     WHERE attribute_row.attrelid = 'storage_v2_search_document'::REGCLASS
       AND attribute_row.attname = 'fts_simple';
    IF v_expression IS NULL
       OR strpos(v_expression, 'storage_v2_safe_tsvector') = 0 THEN
        DROP INDEX IF EXISTS idx_storage_v2_search_document_fts;
        ALTER TABLE storage_v2_search_document DROP COLUMN fts_simple;
        ALTER TABLE storage_v2_search_document
            ADD COLUMN fts_simple TSVECTOR
            GENERATED ALWAYS AS (storage_v2_safe_tsvector(search_text)) STORED;
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS idx_storage_v2_search_document_fts
    ON storage_v2_search_document USING GIN (fts_simple);

DO $$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    v_definition TEXT;
    v_old TEXT :=
        'binding.fts_simple @@ phraseto_tsquery(''simple'', phrase.value)';
    v_new TEXT :=
        'storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)';
BEGIN
    v_definition := pg_get_functiondef(v_signature);
    IF strpos(v_definition, v_new) > 0 THEN
        RETURN;
    END IF;
    IF strpos(v_definition, v_old) = 0 THEN
        RAISE EXCEPTION 'storage-v2 phrase predicate differs from the reviewed definition';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    EXECUTE v_definition;
END
$$;

ALTER FUNCTION storage_v2_safe_tsvector(TEXT) OWNER TO mainrag;
ALTER FUNCTION storage_v2_normalize_phrase_text(TEXT) OWNER TO mainrag;
ALTER FUNCTION storage_v2_phrase_matches(TSVECTOR, TEXT, TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_safe_tsvector(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_normalize_phrase_text(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_phrase_matches(TSVECTOR, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_safe_tsvector(TEXT) TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_normalize_phrase_text(TEXT) TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_phrase_matches(TSVECTOR, TEXT, TEXT) TO mainrag;

COMMENT ON COLUMN storage_v2_search_document.fts_simple IS
    'Optional GIN phrase accelerator; NULL only when the complete tsvector exceeds PostgreSQL limits.';
COMMENT ON FUNCTION storage_v2_phrase_matches(TSVECTOR, TEXT, TEXT) IS
    'Uses GIN-compatible phrase semantics normally and a complete token-boundary fallback for oversized documents.';
