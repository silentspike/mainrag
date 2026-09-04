-- Migration 039: keep exact postings indexable for unbounded source tokens
--
-- PostgreSQL btree entries cannot contain arbitrarily large TEXT values. The
-- original (document_id, term) primary key therefore rejected valid source
-- documents containing a single multi-kilobyte token. Keep the source term for
-- exact collision-safe comparison, but bind document-local identity to a
-- fixed-width SHA-256 digest and use PostgreSQL's equality-only hash index for
-- term lookup.

ALTER TABLE storage_v2_search_posting
    ADD COLUMN IF NOT EXISTS term_sha256 BYTEA
    GENERATED ALWAYS AS (digest(term, 'sha256')) STORED;

ALTER TABLE storage_v2_search_posting
    ALTER COLUMN term_sha256 SET NOT NULL;

DO $$
DECLARE
    v_primary_key_name TEXT;
    v_primary_key_columns TEXT[];
BEGIN
    SELECT constraint_row.conname,
           array_agg(attribute.attname ORDER BY key_column.ordinality)
      INTO v_primary_key_name, v_primary_key_columns
      FROM pg_constraint constraint_row
      CROSS JOIN LATERAL unnest(constraint_row.conkey)
           WITH ORDINALITY AS key_column(attnum, ordinality)
      JOIN pg_attribute attribute
        ON attribute.attrelid = constraint_row.conrelid
       AND attribute.attnum = key_column.attnum
     WHERE constraint_row.conrelid = 'storage_v2_search_posting'::REGCLASS
       AND constraint_row.contype = 'p'
     GROUP BY constraint_row.conname;

    IF v_primary_key_name IS NOT NULL
       AND v_primary_key_columns IS DISTINCT FROM ARRAY['document_id', 'term_sha256'] THEN
        EXECUTE format(
            'ALTER TABLE storage_v2_search_posting DROP CONSTRAINT %I',
            v_primary_key_name
        );
        v_primary_key_name := NULL;
    END IF;

    IF v_primary_key_name IS NULL THEN
        ALTER TABLE storage_v2_search_posting
            ADD CONSTRAINT storage_v2_search_posting_pkey
            PRIMARY KEY (document_id, term_sha256);
    END IF;
END
$$;

DO $$
DECLARE
    v_access_method TEXT;
    v_index_columns TEXT[];
BEGIN
    SELECT access_method.amname,
           array_agg(attribute.attname ORDER BY key_column.ordinality)
      INTO v_access_method, v_index_columns
      FROM pg_class index_row
      JOIN pg_index index_definition ON index_definition.indexrelid = index_row.oid
      JOIN pg_am access_method ON access_method.oid = index_row.relam
      CROSS JOIN LATERAL unnest(index_definition.indkey)
           WITH ORDINALITY AS key_column(attnum, ordinality)
      LEFT JOIN pg_attribute attribute
        ON attribute.attrelid = index_definition.indrelid
       AND attribute.attnum = key_column.attnum
     WHERE index_row.oid = to_regclass('idx_storage_v2_search_posting_term')
     GROUP BY access_method.amname;

    IF v_access_method IS NOT NULL
       AND (v_access_method <> 'hash' OR v_index_columns IS DISTINCT FROM ARRAY['term']) THEN
        DROP INDEX idx_storage_v2_search_posting_term;
        v_access_method := NULL;
    END IF;

    IF v_access_method IS NULL THEN
        CREATE INDEX idx_storage_v2_search_posting_term
            ON storage_v2_search_posting USING HASH (term);
    END IF;
END
$$;

COMMENT ON COLUMN storage_v2_search_posting.term_sha256 IS
    'Fixed-width document-local posting identity; term remains authoritative for collision-safe equality.';
