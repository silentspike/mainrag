-- Losslessly derive native word identifiers from their existing exact postings.
-- Profiles, materialization hashes and full identifier membership stay unchanged.
-- Existing documents retain their representation until bounded explicit conversion.
BEGIN;

DO $guard$
DECLARE expected RECORD; actual RECORD;
BEGIN
    FOR expected IN SELECT * FROM (VALUES
        ('storage_v2_put_search_document(text,text,bigint,text,text[])',
         'a7fb6a8087ab4df30f13c06cbb3a498b341c25911b8b97d95b1c09b7403a34ee'),
        ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
         'fce5dfa974b9fb6c768e667893396d6ec58b9983fbf7de734af0bf2f500bd893'),
        ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)',
         '9850527203a751cf5ed85d5b665c1c30af73e735dfae6098be0ba5533a0eac77')
    ) bound(signature,definition_sha256) LOOP
        IF encode(sha256(convert_to(pg_get_functiondef(expected.signature::REGPROCEDURE),'UTF8')),'hex')
           IS DISTINCT FROM expected.definition_sha256 THEN
            RAISE EXCEPTION 'posting-derived identifier definition differs';
        END IF;
        SELECT * INTO STRICT actual FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE;
        IF actual.proowner<>'mainrag'::REGROLE OR NOT actual.prosecdef THEN
            RAISE EXCEPTION 'posting-derived identifier authority differs';
        END IF;
    END LOOP;
    IF NOT EXISTS(SELECT 1 FROM pg_trigger
         WHERE tgrelid='storage_v2_search_document'::REGCLASS
           AND tgname='storage_v2_search_document_immutable' AND tgenabled='O' AND tgtype=27
           AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE)
       OR (SELECT relowner FROM pg_class WHERE oid='storage_v2_search_document'::REGCLASS)
          <>'mainrag'::REGROLE THEN
        RAISE EXCEPTION 'posting-derived identifier document boundary differs';
    END IF;
    IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_reject_retrieval_mutation()'::REGPROCEDURE),
          'UTF8')),'hex')<>'d11ce6d178bbe4989f674efbc0bcad839957c064152c673897a4f3758e84ac02'
       OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_search_posting'::REGCLASS
          AND tgname='storage_v2_search_posting_immutable' AND tgtype=27 AND tgenabled='O'
          AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE)
       OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_compact_posting_block'::REGCLASS
          AND tgname='storage_v2_compact_posting_immutable' AND tgtype=27 AND tgenabled='O'
          AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'posting-derived identifier posting immutability differs';
    END IF;
END $guard$;

ALTER TABLE storage_v2_search_document
    ADD COLUMN exact_identifiers_derived BOOLEAN NOT NULL DEFAULT FALSE;

-- A complete constructor seals its posting set before publication. Conversion
-- also seals older sets so later INSERTs cannot change derived membership.
CREATE TABLE storage_v2_document_postings_seal (
    document_id BIGINT PRIMARY KEY REFERENCES storage_v2_search_document(id) ON DELETE RESTRICT,
    materialization_sha256 BYTEA NOT NULL CHECK(octet_length(materialization_sha256)=32)
);
REVOKE ALL ON storage_v2_document_postings_seal FROM PUBLIC,mainrag;
CREATE TRIGGER document_postings_seal_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_document_postings_seal FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

CREATE FUNCTION storage_v2_seal_document_postings(p_document_id BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE identity BYTEA;
BEGIN
    IF storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'posting publication requires administrator authority' USING ERRCODE='42501';
    END IF;
    SELECT materialization_sha256 INTO STRICT identity FROM storage_v2_search_document
        WHERE id=p_document_id FOR UPDATE;
    INSERT INTO storage_v2_document_postings_seal VALUES(p_document_id,identity)
        ON CONFLICT(document_id) DO NOTHING;
    IF NOT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal
        WHERE document_id=p_document_id AND materialization_sha256=identity) THEN
        RAISE EXCEPTION 'posting publication identity differs';
    END IF;
END $$;
REVOKE ALL ON FUNCTION storage_v2_seal_document_postings(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_seal_document_postings(BIGINT) TO mainrag;

CREATE FUNCTION storage_v2_reject_sealed_posting_insert()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE document BIGINT;
BEGIN
    -- Statement-level transition rows amortize the guard across a whole block
    -- group. The row lock serializes publication with concurrent FK inserts.
    FOR document IN SELECT DISTINCT added.document_id FROM storage_v2_new_postings added LOOP
        PERFORM 1 FROM storage_v2_search_document WHERE id=document FOR KEY SHARE;
        IF EXISTS(SELECT 1 FROM storage_v2_document_postings_seal WHERE document_id=document) THEN
            RAISE EXCEPTION 'sealed search-document postings are immutable';
        END IF;
    END LOOP;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION storage_v2_reject_sealed_posting_insert() FROM PUBLIC;
CREATE TRIGGER storage_v2_search_posting_sealed AFTER INSERT ON storage_v2_search_posting
    REFERENCING NEW TABLE AS storage_v2_new_postings FOR EACH STATEMENT
    EXECUTE FUNCTION storage_v2_reject_sealed_posting_insert();
CREATE TRIGGER storage_v2_compact_posting_sealed AFTER INSERT ON storage_v2_compact_posting_block
    REFERENCING NEW TABLE AS storage_v2_new_postings FOR EACH STATEMENT
    EXECUTE FUNCTION storage_v2_reject_sealed_posting_insert();

CREATE FUNCTION storage_v2_document_word_identifiers(p_document_id BIGINT)
RETURNS TEXT[] LANGUAGE sql STABLE
SET search_path=pg_catalog,public AS $$
    SELECT COALESCE(array_agg(term ORDER BY term),ARRAY[]::TEXT[])
      FROM (
          SELECT DISTINCT term FROM (
              SELECT posting.term FROM storage_v2_search_posting posting
               WHERE posting.document_id=p_document_id
              UNION ALL
              SELECT term FROM storage_v2_compact_posting_block block
                CROSS JOIN LATERAL unnest(block.terms) term
               WHERE block.document_id=p_document_id
          ) complete
          WHERE term ~ '^[[:alnum:]_]+$' AND term ~ '[_0-9]'
      ) identifiers
$$;

CREATE FUNCTION storage_v2_document_exact_identifiers(p_document_id BIGINT)
RETURNS TEXT[] LANGUAGE sql STABLE
SET search_path=pg_catalog,public AS $$
    SELECT CASE WHEN document.exact_identifiers_derived
                THEN storage_v2_document_word_identifiers(document.id)
                ELSE document.exact_identifiers END
      FROM storage_v2_search_document document WHERE document.id=p_document_id
$$;

CREATE FUNCTION storage_v2_document_has_exact_identifier(p_document_id BIGINT,p_identifier TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE
SET search_path=pg_catalog,public AS $$
    SELECT COALESCE((SELECT CASE WHEN document.exact_identifiers_derived
        THEN p_identifier ~ '^[[:alnum:]_]+$' AND p_identifier ~ '[_0-9]'
             AND EXISTS(SELECT 1 FROM storage_v2_document_posting(document.id,p_identifier)
                         WHERE term=p_identifier AND term_frequency>0)
        ELSE p_identifier=ANY(document.exact_identifiers) END
      FROM storage_v2_search_document document WHERE document.id=p_document_id),FALSE)
$$;

ALTER FUNCTION storage_v2_document_word_identifiers(BIGINT) OWNER TO mainrag;
ALTER FUNCTION storage_v2_document_exact_identifiers(BIGINT) OWNER TO mainrag;
ALTER FUNCTION storage_v2_document_has_exact_identifier(BIGINT,TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_document_word_identifiers(BIGINT),
    storage_v2_document_exact_identifiers(BIGINT),
    storage_v2_document_has_exact_identifier(BIGINT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_document_word_identifiers(BIGINT),
    storage_v2_document_exact_identifiers(BIGINT),
    storage_v2_document_has_exact_identifier(BIGINT,TEXT) TO mainrag;

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
    v_word_exact TEXT[];
    v_stored_exact TEXT[];
    v_derived BOOLEAN;
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
        IF (v_document.search_text, storage_v2_document_exact_identifiers(v_document.id))
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
           COALESCE(array_agg(frequency ORDER BY token COLLATE "C"),ARRAY[]::BIGINT[]),
           COALESCE(array_agg(token ORDER BY token)
               FILTER(WHERE word_count>0 AND token ~ '[_0-9]'),ARRAY[]::TEXT[])
      INTO v_token_count,v_terms,v_frequencies,v_word_exact FROM frequency;
    v_hash := storage_v2_hash_parts('mainrag.search-document.v1', ARRAY[
        convert_to(p_profile_id, 'UTF8'), convert_to(p_component_kind, 'UTF8'),
        v_component_digest, convert_to(p_search_text, 'UTF8'),
        convert_to(array_to_string(v_exact, E'\n'), 'UTF8')
    ]);

    v_derived:=v_exact IS NOT DISTINCT FROM v_word_exact;
    v_stored_exact:=CASE WHEN v_derived THEN ARRAY[]::TEXT[] ELSE v_exact END;

    INSERT INTO storage_v2_search_document(
        profile_id, component_kind, body_id, node_id, search_text, token_count,
        exact_identifiers, materialization_sha256, exact_identifiers_derived
    ) VALUES (
        p_profile_id, p_component_kind,
        CASE WHEN p_component_kind = 'body' THEN p_component_id END,
        CASE WHEN p_component_kind = 'node' THEN p_component_id END,
        p_search_text, v_token_count, v_stored_exact, v_hash, v_derived
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
        IF (v_document.search_text, storage_v2_document_exact_identifiers(v_document.id))
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
    PERFORM storage_v2_seal_document_postings(v_document.id);
    RETURN v_document;
END
$function$
;

DO $readers$
DECLARE signature TEXT; definition TEXT;
    old_projection TEXT := 'SELECT scope.occurrence_id, document.exact_identifiers';
    old_match TEXT := 'exact.value = ANY(binding.exact_identifiers)';
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,old_projection,'')))/length(old_projection)<>1
           OR (length(definition)-length(replace(definition,old_match,'')))/length(old_match)<>1 THEN
            RAISE EXCEPTION 'posting-derived identifier reader projection differs';
        END IF;
        definition:=replace(definition,old_projection,
            'SELECT scope.occurrence_id, document.id AS document_id');
        definition:=replace(definition,old_match,
            'storage_v2_document_has_exact_identifier(binding.document_id,exact.value)');
        EXECUTE definition;
    END LOOP;
END $readers$;

-- Only this equivalent representation transition is allowed. Generated FTS is
-- excluded from BEFORE-trigger comparison because its NEW value is not computed
-- yet; the unchanged search_text and unchanged generation expression determine it.
CREATE FUNCTION storage_v2_reject_document_mutation() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE derived TEXT[];
BEGIN
    IF TG_OP<>'UPDATE' THEN
        RAISE EXCEPTION 'storage-v2 retrieval projections are immutable';
    END IF;
    IF (to_jsonb(NEW)-ARRAY['exact_identifiers','exact_identifiers_derived','fts_simple'])
          IS DISTINCT FROM
          (to_jsonb(OLD)-ARRAY['exact_identifiers','exact_identifiers_derived','fts_simple'])
       OR (NOT OLD.exact_identifiers_derived AND NOT NEW.exact_identifiers_derived) THEN
        RAISE EXCEPTION 'storage-v2 retrieval projections are immutable';
    END IF;
    derived:=storage_v2_document_word_identifiers(OLD.id);
    IF (NOT OLD.exact_identifiers_derived OR cardinality(OLD.exact_identifiers)>0)
       AND OLD.exact_identifiers IS DISTINCT FROM derived THEN
        RAISE EXCEPTION 'posting-derived identifiers do not match the complete original set';
    END IF;
    IF (NEW.exact_identifiers_derived AND NEW.exact_identifiers IS DISTINCT FROM OLD.exact_identifiers
        AND NEW.exact_identifiers IS DISTINCT FROM ARRAY[]::TEXT[])
       OR (NOT NEW.exact_identifiers_derived AND NEW.exact_identifiers IS DISTINCT FROM derived) THEN
        RAISE EXCEPTION 'storage-v2 retrieval projections are immutable unless the complete original set is restored';
    END IF;
    IF NEW.exact_identifiers_derived AND NOT EXISTS(
        SELECT 1 FROM storage_v2_document_postings_seal
         WHERE document_id=OLD.id AND materialization_sha256=OLD.materialization_sha256) THEN
        RAISE EXCEPTION 'posting-derived identifiers require sealed postings';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION storage_v2_reject_document_mutation() FROM PUBLIC;
DROP TRIGGER storage_v2_search_document_immutable ON storage_v2_search_document;
CREATE TRIGGER storage_v2_search_document_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_search_document FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_document_mutation();

CREATE FUNCTION storage_v2_derive_document_identifiers(
    p_after_id BIGINT,p_limit INTEGER,p_drop_values BOOLEAN DEFAULT FALSE
) RETURNS TABLE(scanned BIGINT,converted BIGINT,last_document_id BIGINT,
                removed_logical_identifier_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE document RECORD; derived TEXT[];
BEGIN
    IF storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'identifier representation conversion requires administrator authority'
            USING ERRCODE='42501';
    END IF;
    IF p_after_id IS NULL OR p_after_id<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 256
       OR p_drop_values IS NULL THEN
        RAISE EXCEPTION 'bounded identifier conversion cursor required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('storage-v2-identifier-representation-v1',0));
    scanned:=0; converted:=0; last_document_id:=p_after_id; removed_logical_identifier_bytes:=0;
    FOR document IN SELECT * FROM storage_v2_search_document
         WHERE id>p_after_id ORDER BY id LIMIT p_limit FOR UPDATE LOOP
        scanned:=scanned+1; last_document_id:=document.id;
        IF NOT document.exact_identifiers_derived THEN
            derived:=storage_v2_document_word_identifiers(document.id);
            IF document.exact_identifiers IS DISTINCT FROM derived THEN CONTINUE; END IF;
        ELSIF NOT p_drop_values OR cardinality(document.exact_identifiers)=0 THEN
            CONTINUE;
        END IF;
        IF p_drop_values THEN
            removed_logical_identifier_bytes:=removed_logical_identifier_bytes
                +octet_length(array_to_string(document.exact_identifiers,E'\n'));
        END IF;
        PERFORM storage_v2_seal_document_postings(document.id);
        UPDATE storage_v2_search_document
           SET exact_identifiers_derived=TRUE,
               exact_identifiers=CASE WHEN p_drop_values THEN ARRAY[]::TEXT[] ELSE exact_identifiers END
         WHERE id=document.id;
        converted:=converted+1;
    END LOOP;
    RETURN NEXT;
END $$;
ALTER FUNCTION storage_v2_derive_document_identifiers(BIGINT,INTEGER,BOOLEAN) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_derive_document_identifiers(BIGINT,INTEGER,BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_derive_document_identifiers(BIGINT,INTEGER,BOOLEAN) TO mainrag;

CREATE FUNCTION storage_v2_restore_document_identifiers(p_after_id BIGINT,p_limit INTEGER)
RETURNS TABLE(scanned BIGINT,restored BIGINT,last_document_id BIGINT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE document RECORD;
BEGIN
    IF storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'identifier representation restoration requires administrator authority'
            USING ERRCODE='42501';
    END IF;
    IF p_after_id IS NULL OR p_after_id<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION 'bounded identifier restoration cursor required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('storage-v2-identifier-representation-v1',0));
    scanned:=0; restored:=0; last_document_id:=p_after_id;
    FOR document IN SELECT id,exact_identifiers_derived FROM storage_v2_search_document
         WHERE id>p_after_id ORDER BY id LIMIT p_limit FOR UPDATE LOOP
        scanned:=scanned+1; last_document_id:=document.id;
        IF NOT document.exact_identifiers_derived THEN CONTINUE; END IF;
        UPDATE storage_v2_search_document
           SET exact_identifiers=storage_v2_document_word_identifiers(document.id),
               exact_identifiers_derived=FALSE WHERE id=document.id;
        restored:=restored+1;
    END LOOP;
    RETURN NEXT;
END $$;
ALTER FUNCTION storage_v2_restore_document_identifiers(BIGINT,INTEGER) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_restore_document_identifiers(BIGINT,INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_restore_document_identifiers(BIGINT,INTEGER) TO mainrag;

COMMENT ON COLUMN storage_v2_search_document.exact_identifiers_derived IS
    'The full original word-identifier set is reconstructed exactly from immutable postings.';
COMMENT ON FUNCTION storage_v2_derive_document_identifiers(BIGINT,INTEGER,BOOLEAN) IS
    'Bounded equivalent native representation conversion; logical bytes removed are not physical reclamation.';
COMMIT;
