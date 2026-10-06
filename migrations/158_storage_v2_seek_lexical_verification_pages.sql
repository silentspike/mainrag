-- Select the next visible page before resolving canonical document bindings.
-- The existing immutable segment/body checks and public page result are unchanged.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $seek$
DECLARE
    routine REGPROCEDURE := 'storage_v2_verify_lexical_segment_page(bigint,bigint,integer)'::REGPROCEDURE;
    definition TEXT;
    replacement TEXT;
    old_metadata JSONB;
    new_metadata JSONB;
    marker TEXT := $old$        SELECT occurrence_row.id, document.id AS document_id
          FROM source_generation generation
          JOIN generation_item_version membership
            ON membership.source_id = generation.source_id
           AND membership.valid_from_seq <= generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > generation.generation_seq)
          JOIN occurrence occurrence_row
            ON occurrence_row.source_id = generation.source_id
           AND occurrence_row.artifact_version_id = membership.artifact_version_id
          LEFT JOIN storage_v2_search_view_document binding
            ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
          LEFT JOIN storage_v2_search_document document ON document.id = binding.document_id
         WHERE generation.id = p_generation_id
           AND occurrence_row.id > p_after_occurrence_id
         ORDER BY occurrence_row.id LIMIT p_limit$old$;
    selection TEXT := $new$        WITH generation_scope AS MATERIALIZED (
            SELECT source_id, generation_seq FROM source_generation
             WHERE id = p_generation_id
        ), visible_page AS MATERIALIZED (
            SELECT page.id, page.view_id
              FROM generation_scope generation
              CROSS JOIN LATERAL (
                SELECT occurrence_row.id, occurrence_row.view_id
                  FROM occurrence occurrence_row
                 WHERE occurrence_row.source_id = generation.source_id
                   AND occurrence_row.id > p_after_occurrence_id
                   AND EXISTS (
                       SELECT 1 FROM artifact_version artifact
                       JOIN generation_item_version membership
                         ON membership.source_id = generation.source_id
                        AND membership.source_item_id = artifact.item_id
                        AND membership.artifact_version_id = artifact.id
                        AND membership.valid_from_seq <= generation.generation_seq
                        AND (membership.valid_to_seq IS NULL
                             OR membership.valid_to_seq > generation.generation_seq)
                       WHERE artifact.id = occurrence_row.artifact_version_id
                         AND artifact.source_id = generation.source_id
                       -- Keep this an indexed per-item existence probe. Flattening
                       -- it can restore the complete membership scan and sort.
                       OFFSET 0
                   )
                 ORDER BY occurrence_row.id LIMIT p_limit
              ) page
        )
        SELECT page.id, document.id AS document_id
          FROM visible_page page
          LEFT JOIN storage_v2_search_view_document binding
            ON binding.view_id = page.view_id AND binding.ordinal = 0
          LEFT JOIN storage_v2_search_document document ON document.id = binding.document_id
         ORDER BY page.id$new$;
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'lexical page migration requires the administrative schema operator';
    END IF;
    definition := pg_get_functiondef(routine);
    SELECT jsonb_build_object('owner', proowner, 'acl', proacl, 'config', proconfig,
        'definer', prosecdef, 'volatility', provolatile, 'strict', proisstrict,
        'parallel', proparallel, 'leakproof', proleakproof, 'language', prolang)
      INTO old_metadata FROM pg_proc WHERE oid = routine;
    IF encode(sha256(convert_to(definition, 'UTF8')), 'hex')
            IS DISTINCT FROM '084ebd93de9f702ac96400313ecd1bdce284ce57bf8b837ad51ba8f6a6f075ce'
       OR (SELECT proowner FROM pg_proc WHERE oid = routine)
            <> 'mainrag_v2_frontier_owner'::REGROLE
       OR (SELECT proconfig FROM pg_proc WHERE oid = routine)
            IS DISTINCT FROM ARRAY['search_path=pg_catalog, public', 'row_security=on',
                                  'work_mem=8MB', 'enable_nestloop=on']::TEXT[]
       OR (SELECT proacl FROM pg_proc WHERE oid = routine) IS DISTINCT FROM
            ARRAY['mainrag_v2_frontier_owner=X/mainrag_v2_frontier_owner'::ACLITEM,
                  'mainrag=X/mainrag_v2_frontier_owner'::ACLITEM]
       OR (length(definition) - length(replace(definition, marker, ''))) / length(marker) <> 1 THEN
        RAISE EXCEPTION 'lexical page predecessor or authority differs';
    END IF;
    -- The foreign key binds every artifact to exactly the membership item/source.
    -- Non-overlapping intervals give at most one visible membership per artifact;
    -- replacing its join with EXISTS therefore preserves occurrence multiplicity.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'generation_item_version'::REGCLASS
           AND conname = 'generation_item_version_artifact_version_id_source_item_i_fkey1'
           AND convalidated AND contype = 'f'
           AND pg_get_constraintdef(oid) = 'FOREIGN KEY (artifact_version_id, source_item_id, source_id) REFERENCES artifact_version(id, item_id, source_id) ON DELETE RESTRICT'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'generation_item_version'::REGCLASS
           AND conname = 'generation_item_version_source_id_source_item_id_int8range_excl'
           AND convalidated AND contype = 'x'
           AND pg_get_constraintdef(oid) = 'EXCLUDE USING gist (source_id WITH =, source_item_id WITH =, int8range(valid_from_seq, valid_to_seq, ''[)''::text) WITH &&)'
    ) THEN
        RAISE EXCEPTION 'validated lexical page membership identity and interval constraints required';
    END IF;
    replacement := replace(definition, marker, selection);
    EXECUTE replacement;
    SELECT jsonb_build_object('owner', proowner, 'acl', proacl, 'config', proconfig,
        'definer', prosecdef, 'volatility', provolatile, 'strict', proisstrict,
        'parallel', proparallel, 'leakproof', proleakproof, 'language', prolang)
      INTO new_metadata FROM pg_proc WHERE oid = routine;
    IF old_metadata IS DISTINCT FROM new_metadata
       OR pg_get_functiondef(routine) IS DISTINCT FROM replacement
       OR replace(pg_get_functiondef(routine), selection, '')
            IS DISTINCT FROM replace(definition, marker, '') THEN
        RAISE EXCEPTION 'lexical page migration changed authority or immutable verification criteria';
    END IF;
END $seek$;
COMMIT;
