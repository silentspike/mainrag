-- Lossless posting locators for large newly materialized documents.
-- Retained documents and their materialization identities are never rewritten.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $guard$
DECLARE expected RECORD;
BEGIN
 FOR expected IN SELECT * FROM (VALUES
  ('storage_v2_put_search_document(text,text,bigint,text,text[])','6fd5792c91c7f1290811ce17e421a9fd5e4f63a36bd9a6f18b3341ee62af3b0e'),
  ('storage_v2_scoped_term_posting(bigint[],text)','682e01e264802e28ba6f5830427e50e5286a80150af32527a5647a49d0686a3d'),
  ('storage_v2_posting_probe(text,bigint)','bfdcd0e8f8e6f8191d84fe3b342e46e66318a5c4c5fb3176770a367750b60734'),
  ('storage_v2_document_posting(bigint,text)','993b3ede66350da2739d0cfcaa7d4a1baa844bffab97e90f7480ab2dcc5a78be'),
  ('storage_v2_scoped_query_posting(bigint[],text[])','17879f3d9181addc6bab0d18b71251a35b9d1c3c21246863440d1e980ac0bd2c'),
  ('storage_v2_document_word_identifiers(bigint)','e798c7d91f29795233fbf8d7bc4bcc55f2c4a412ec083aa137644c0b79f3202c')) x(signature,sha256) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(expected.signature::REGPROCEDURE),'UTF8')),'hex')
       IS DISTINCT FROM expected.sha256
     OR (SELECT proowner FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE)<>'mainrag'::REGROLE THEN
   RAISE EXCEPTION 'byte posting predecessor definition or authority differs: %',expected.signature;
  END IF;
 END LOOP;
END $guard$;

-- Byte offsets refer to the established lowercased UTF-8 representation.
-- The default collation is required by the original identifier classifier.
CREATE FUNCTION storage_v2_decode_term_locations(
    p_text TEXT,p_starts INTEGER[],p_lengths INTEGER[]
) RETURNS TEXT[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $$
    WITH bytes AS MATERIALIZED (SELECT convert_to(lower(p_text),'UTF8') AS value)
    SELECT ARRAY(SELECT convert_from(substring(bytes.value FROM item.start
                  FOR item.length),'UTF8') COLLATE "default"
        FROM bytes CROSS JOIN unnest(p_starts,p_lengths) WITH ORDINALITY
             item(start,length,ordinal) ORDER BY item.ordinal)
$$;
ALTER FUNCTION storage_v2_decode_term_locations(TEXT,INTEGER[],INTEGER[]) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_decode_term_locations(TEXT,INTEGER[],INTEGER[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_decode_term_locations(TEXT,INTEGER[],INTEGER[]) TO mainrag;

CREATE TABLE storage_v2_byte_posting_block (
    document_id BIGINT NOT NULL REFERENCES storage_v2_search_document(id) ON DELETE RESTRICT,
    block_order BIGINT NOT NULL CHECK(block_order>=0),
    cached_terms TEXT[] NOT NULL,
    text_byte_starts INTEGER[] NOT NULL,
    text_byte_lengths INTEGER[] NOT NULL,
    term_frequencies BIGINT[] NOT NULL,
    fingerprints INTEGER[] NOT NULL,
    cache_max_term_bytes SMALLINT NOT NULL DEFAULT 128
        CHECK(cache_max_term_bytes BETWEEN 1 AND 128),
    PRIMARY KEY(document_id,block_order),
    CHECK(array_ndims(cached_terms)=1 AND array_lower(cached_terms,1)=1
        AND cardinality(cached_terms)=cardinality(text_byte_starts)),
    CHECK(array_ndims(text_byte_starts)=1 AND array_lower(text_byte_starts,1)=1
        AND cardinality(text_byte_starts) BETWEEN 1 AND 256
        AND array_position(text_byte_starts,NULL) IS NULL AND 0<ALL(text_byte_starts)),
    CHECK(array_ndims(text_byte_lengths)=1 AND array_lower(text_byte_lengths,1)=1
        AND cardinality(text_byte_lengths)=cardinality(text_byte_starts)
        AND array_position(text_byte_lengths,NULL) IS NULL AND 0<ALL(text_byte_lengths)),
    CHECK(array_ndims(term_frequencies)=1 AND array_lower(term_frequencies,1)=1
        AND cardinality(term_frequencies)=cardinality(text_byte_starts)
        AND array_position(term_frequencies,NULL) IS NULL AND 0<ALL(term_frequencies)),
    CHECK(array_ndims(fingerprints)=1 AND array_lower(fingerprints,1)=1
        AND cardinality(fingerprints) BETWEEN 1 AND cardinality(text_byte_starts)
        AND array_position(fingerprints,NULL) IS NULL)
);
ALTER TABLE storage_v2_byte_posting_block ALTER COLUMN cached_terms SET COMPRESSION lz4;
ALTER TABLE storage_v2_byte_posting_block OWNER TO mainrag;
ALTER TABLE storage_v2_byte_posting_block ENABLE ROW LEVEL SECURITY;
CREATE POLICY storage_v2_byte_posting_admin ON storage_v2_byte_posting_block
    USING(storage_v2_is_admin()) WITH CHECK(storage_v2_is_admin());
CREATE INDEX idx_storage_v2_byte_posting_fingerprint
    ON storage_v2_byte_posting_block USING GIN(fingerprints);
REVOKE ALL ON storage_v2_byte_posting_block FROM PUBLIC;
CREATE TRIGGER storage_v2_byte_posting_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_byte_posting_block FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
CREATE TRIGGER storage_v2_byte_posting_sealed AFTER INSERT ON storage_v2_byte_posting_block
    REFERENCING NEW TABLE AS storage_v2_new_postings FOR EACH STATEMENT
    EXECUTE FUNCTION storage_v2_reject_sealed_posting_insert();

-- A statement checks the complete incoming block set using one canonical
-- decode per document. Cached fingerprints cannot hide real exact terms.
CREATE FUNCTION storage_v2_validate_byte_posting_insert()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE document RECORD; block RECORD; bytes BYTEA; terms TEXT[];
BEGIN
 FOR document IN SELECT DISTINCT added.document_id FROM storage_v2_new_postings added LOOP
  SELECT convert_to(lower(search_text),'UTF8') INTO STRICT bytes
    FROM storage_v2_search_document WHERE id=document.document_id;
  IF EXISTS(SELECT 1 FROM storage_v2_compact_posting_block
             WHERE document_id=document.document_id)
     OR EXISTS(SELECT 1 FROM storage_v2_search_posting
                WHERE document_id=document.document_id) THEN
   RAISE EXCEPTION 'posting representations cannot overlap';
  END IF;
  FOR block IN SELECT * FROM storage_v2_new_postings
                WHERE document_id=document.document_id LOOP
   IF EXISTS(SELECT 1 FROM unnest(block.text_byte_starts,block.text_byte_lengths) p(start,length)
              WHERE start::BIGINT+length-1>octet_length(bytes)) THEN
    RAISE EXCEPTION 'byte posting locator exceeds canonical text';
   END IF;
   SELECT array_agg(convert_from(substring(bytes FROM p.start FOR p.length),'UTF8')
                        COLLATE "default" ORDER BY p.ordinal) INTO terms
     FROM unnest(block.text_byte_starts,block.text_byte_lengths) WITH ORDINALITY p(start,length,ordinal);
   IF block.cached_terms IS DISTINCT FROM
        ARRAY(SELECT CASE WHEN octet_length(term)<=block.cache_max_term_bytes THEN term ELSE NULL END
                FROM unnest(terms) WITH ORDINALITY t(term,n) ORDER BY n) THEN
    RAISE EXCEPTION 'bounded posting term cache differs from canonical text';
   END IF;
   IF ''=ANY(terms) OR storage_v2_posting_fingerprints(terms) IS DISTINCT FROM block.fingerprints THEN
    RAISE EXCEPTION 'byte posting fingerprint differs from complete exact terms';
   END IF;
  END LOOP;
 END LOOP;
 RETURN NULL;
END $$;
ALTER FUNCTION storage_v2_validate_byte_posting_insert() OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_validate_byte_posting_insert() FROM PUBLIC;
CREATE TRIGGER storage_v2_byte_posting_validated AFTER INSERT ON storage_v2_byte_posting_block
    REFERENCING NEW TABLE AS storage_v2_new_postings FOR EACH STATEMENT
    EXECUTE FUNCTION storage_v2_validate_byte_posting_insert();

CREATE VIEW storage_v2_posting_block_all WITH(security_invoker=true) AS
 SELECT document_id,block_order,terms,term_frequencies,fingerprints
   FROM storage_v2_compact_posting_block
 UNION ALL
 SELECT block.document_id,block.block_order,
        storage_v2_decode_term_locations(document.search_text,
            block.text_byte_starts,block.text_byte_lengths),
        block.term_frequencies,block.fingerprints
   FROM storage_v2_byte_posting_block block
   JOIN storage_v2_search_document document ON document.id=block.document_id;
ALTER VIEW storage_v2_posting_block_all OWNER TO mainrag;
REVOKE ALL ON storage_v2_posting_block_all FROM PUBLIC;
GRANT SELECT ON storage_v2_posting_block_all TO mainrag;

-- Common short queries never decode the canonical document. Long terms retain
-- exact byte rechecks after fingerprint, document and byte-length pruning.
CREATE FUNCTION storage_v2_byte_posting_matches(
 p_document_ids BIGINT[],p_terms TEXT[],p_limit BIGINT DEFAULT NULL
) RETURNS TABLE(document_id BIGINT,term TEXT,term_frequency BIGINT)
LANGUAGE plpgsql STABLE
SET search_path=pg_catalog,public,pg_temp SET plan_cache_mode=force_custom_plan SET jit=off AS $$
DECLARE v_terms TEXT[]; v_fingerprints INTEGER[]; v_min BIGINT; v_max BIGINT;
 v_block RECORD; v_term TEXT; v_position INTEGER; v_count BIGINT:=0;
 v_bytes BYTEA; v_bytes_document BIGINT;
BEGIN
 IF (p_limit IS NOT NULL AND p_limit NOT BETWEEN 1 AND 4097)
    OR (p_document_ids IS NOT NULL AND cardinality(p_document_ids)=0) THEN RETURN; END IF;
 SELECT array_agg(DISTINCT value ORDER BY value) INTO v_terms
   FROM unnest(p_terms) input(value) WHERE value IS NOT NULL;
 IF v_terms IS NULL THEN RETURN; END IF;
 v_fingerprints:=storage_v2_posting_fingerprints(v_terms);
 SELECT min(id),max(id) INTO v_min,v_max FROM unnest(p_document_ids) input(id);
 IF p_document_ids IS NOT NULL AND v_min IS NULL THEN RETURN; END IF;
 FOR v_block IN
  WITH requested AS MATERIALIZED (
   SELECT DISTINCT id FROM unnest(p_document_ids) input(id)
  ), matching AS MATERIALIZED (
   SELECT b.document_id,b.block_order FROM storage_v2_byte_posting_block b
    WHERE b.fingerprints && v_fingerprints
      AND (p_document_ids IS NULL OR b.document_id BETWEEN v_min AND v_max)
  ), scoped AS MATERIALIZED (
   SELECT candidate.* FROM matching candidate
    WHERE p_document_ids IS NULL
       OR (NOT EXISTS(SELECT 1 FROM matching OFFSET 32)
           AND candidate.document_id=ANY((SELECT p_document_ids OFFSET 0)::BIGINT[]))
       OR (EXISTS(SELECT 1 FROM matching OFFSET 32)
           AND EXISTS(SELECT 1 FROM requested WHERE requested.id=candidate.document_id))
  )
  SELECT b.* FROM scoped candidate CROSS JOIN LATERAL (
    SELECT b.document_id,b.cached_terms,b.text_byte_starts,b.text_byte_lengths,b.term_frequencies,
           b.cache_max_term_bytes
      FROM storage_v2_byte_posting_block b
     WHERE b.document_id=candidate.document_id AND b.block_order=candidate.block_order OFFSET 0
  ) b ORDER BY candidate.document_id,candidate.block_order
 LOOP
  FOREACH v_term IN ARRAY v_terms LOOP
   FOREACH v_position IN ARRAY array_positions(v_block.cached_terms,v_term) LOOP
    document_id:=v_block.document_id;term:=v_term;
    term_frequency:=v_block.term_frequencies[v_position];RETURN NEXT;
    v_count:=v_count+1;IF v_count=p_limit THEN RETURN;END IF;
   END LOOP;
   IF octet_length(v_term)<=v_block.cache_max_term_bytes THEN CONTINUE; END IF;
   FOREACH v_position IN ARRAY array_positions(v_block.text_byte_lengths,octet_length(v_term)) LOOP
    IF v_bytes_document IS DISTINCT FROM v_block.document_id THEN
     SELECT convert_to(lower(d.search_text),'UTF8') INTO STRICT v_bytes
       FROM storage_v2_search_document d WHERE d.id=v_block.document_id;
     v_bytes_document:=v_block.document_id;
    END IF;
    IF convert_from(substring(v_bytes FROM v_block.text_byte_starts[v_position]
                                FOR v_block.text_byte_lengths[v_position]),'UTF8')
          COLLATE "default"=v_term THEN
     document_id:=v_block.document_id;term:=v_term;
     term_frequency:=v_block.term_frequencies[v_position];RETURN NEXT;
     v_count:=v_count+1;IF v_count=p_limit THEN RETURN;END IF;
    END IF;
   END LOOP;
  END LOOP;
 END LOOP;
END $$;
ALTER FUNCTION storage_v2_byte_posting_matches(BIGINT[],TEXT[],BIGINT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_byte_posting_matches(BIGINT[],TEXT[],BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_byte_posting_matches(BIGINT[],TEXT[],BIGINT) TO mainrag;

-- The sparse cached path must not invoke the byte reader once per document.
-- Preserve its exact predecessor as a private invoker with the same authority.
DO $cached_point$
DECLARE definition TEXT;
        signature TEXT:='CREATE OR REPLACE FUNCTION public.storage_v2_document_posting(';
BEGIN
 definition:=pg_get_functiondef('storage_v2_document_posting(bigint,text)'::REGPROCEDURE);
 IF strpos(definition,signature)<>1 THEN RAISE EXCEPTION 'cached posting reader boundary differs'; END IF;
 EXECUTE replace(definition,signature,
    'CREATE FUNCTION public.storage_v2_cached_document_posting(');
END $cached_point$;
ALTER FUNCTION storage_v2_cached_document_posting(BIGINT,TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_cached_document_posting(BIGINT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_cached_document_posting(BIGINT,TEXT) TO mainrag;

DO $readers$
DECLARE patch RECORD; definition TEXT;
BEGIN
 FOR patch IN SELECT * FROM (VALUES
  ('storage_v2_document_posting(bigint,text)',
   $old$AND item.term=p_term$old$,
   $new$AND item.term=p_term
    UNION ALL SELECT posting.term,posting.term_frequency
     FROM public.storage_v2_byte_posting_matches(ARRAY[p_document_id],ARRAY[p_term],NULL) posting$new$),
  ('storage_v2_posting_probe(text,bigint)',
   $old$) complete WHERE p_limit BETWEEN 1 AND 4097 LIMIT p_limit$old$,
   $new$ UNION ALL SELECT posting.document_id,posting.term,posting.term_frequency
         FROM public.storage_v2_byte_posting_matches(NULL,ARRAY[p_term],p_limit) posting
    ) complete WHERE p_limit BETWEEN 1 AND 4097 LIMIT p_limit$new$),
  ('storage_v2_scoped_term_posting(bigint[],text)',
   $old$WHERE item.term=p_term;$old$,
   $new$WHERE item.term=p_term
        UNION ALL SELECT posting.document_id,posting.term,posting.term_frequency
         FROM public.storage_v2_byte_posting_matches(p_document_ids,ARRAY[p_term],NULL) posting;$new$),
  ('storage_v2_scoped_query_posting(bigint[],text[])',
   $old$IF cardinality(p_document_ids)=0 OR v_terms IS NULL THEN RETURN; END IF;$old$,
   $new$IF cardinality(p_document_ids)=0 OR v_terms IS NULL THEN RETURN; END IF;
    RETURN QUERY SELECT posting.document_id,posting.term,posting.term_frequency
     FROM public.storage_v2_byte_posting_matches(p_document_ids,v_terms,NULL) posting;$new$),
  ('storage_v2_scoped_query_posting(bigint[],text[])',
   $old$public.storage_v2_document_posting(requested.id,requested_term.value)$old$,
   $new$public.storage_v2_cached_document_posting(requested.id,requested_term.value)$new$)
 ) expected(signature,old_text,new_text) LOOP
  definition:=pg_get_functiondef(patch.signature::REGPROCEDURE);
  IF (length(definition)-length(replace(definition,patch.old_text,'')))/length(patch.old_text)<>1 THEN
   RAISE EXCEPTION 'cached byte posting reader replacement boundary differs: %',patch.signature;
  END IF;
  EXECUTE replace(definition,patch.old_text,patch.new_text);
 END LOOP;
END $readers$;

-- Whole-document export and identifier derivation amortize canonical decoding
-- across every block. They must not lowercase the same body per term block.
CREATE FUNCTION storage_v2_complete_document_posting_blocks(p_document_id BIGINT)
RETURNS TABLE(block_order BIGINT,terms TEXT[],term_frequencies BIGINT[],fingerprints INTEGER[])
LANGUAGE plpgsql STABLE STRICT
SET search_path=pg_catalog,public AS $$
DECLARE bytes BYTEA;
BEGIN
 RETURN QUERY SELECT b.block_order,b.terms,b.term_frequencies,b.fingerprints
   FROM storage_v2_compact_posting_block b WHERE b.document_id=p_document_id;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_byte_posting_block WHERE document_id=p_document_id) THEN
  RETURN;
 END IF;
 SELECT convert_to(lower(d.search_text),'UTF8') INTO STRICT bytes
   FROM storage_v2_search_document d WHERE d.id=p_document_id;
 RETURN QUERY SELECT b.block_order,
   ARRAY(SELECT convert_from(substring(bytes FROM p.start FOR p.length),'UTF8') COLLATE "default"
           FROM unnest(b.text_byte_starts,b.text_byte_lengths) WITH ORDINALITY p(start,length,ordinal)
          ORDER BY p.ordinal),b.term_frequencies,b.fingerprints
   FROM storage_v2_byte_posting_block b WHERE b.document_id=p_document_id;
END $$;
ALTER FUNCTION storage_v2_complete_document_posting_blocks(BIGINT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_complete_document_posting_blocks(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_complete_document_posting_blocks(BIGINT) TO mainrag;

CREATE OR REPLACE FUNCTION storage_v2_document_word_identifiers(p_document_id BIGINT)
RETURNS TEXT[] LANGUAGE sql STABLE
SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(array_agg(term ORDER BY term),ARRAY[]::TEXT[])
   FROM (
    SELECT DISTINCT term FROM (
     SELECT posting.term FROM storage_v2_search_posting posting WHERE posting.document_id=p_document_id
     UNION ALL
     SELECT term FROM storage_v2_complete_document_posting_blocks(p_document_id) block
       CROSS JOIN LATERAL unnest(block.terms) term
    ) complete WHERE term ~ '^[[:alnum:]_]+$' AND term ~ '[_0-9]'
   ) identifiers
$$;

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
    v_byte_starts INTEGER[];
    v_byte_lengths INTEGER[];
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
    WITH word_parts AS MATERIALIZED (
        SELECT p.parts[2] AS token,1::BIGINT AS word_count,
               COALESCE(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
                   ORDER BY p.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)
                   +octet_length(p.parts[1])+1 AS byte_start
          FROM regexp_matches(v_normalized_text COLLATE "default",
                   '([^[:alnum:]_]*)([[:alnum:]_]+)','g') WITH ORDINALITY p(parts,n)
    ), whitespace_parts AS MATERIALIZED (
        SELECT p.parts[2] AS token,0::BIGINT AS word_count,
               COALESCE(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
                   ORDER BY p.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)
                   +octet_length(p.parts[1])+1 AS byte_start
          FROM regexp_matches(v_normalized_text COLLATE "default",
                   '([[:space:]]*)([^[:space:]]+)','g') WITH ORDINALITY p(parts,n)
    ), searchable_tokens AS (
        SELECT * FROM word_parts
        UNION ALL SELECT * FROM whitespace_parts
         WHERE token !~ '^[[:alnum:]_]+$' AND token ~ '[[:alnum:]_]'
    ), frequency AS (
        SELECT token,count(*)::BIGINT AS frequency,sum(word_count)::BIGINT AS word_count,
               min(byte_start)::INTEGER AS byte_start,octet_length(token) AS byte_length
          FROM searchable_tokens GROUP BY token
    )
    SELECT COALESCE(sum(word_count),0)::BIGINT,
           COALESCE(array_agg(token ORDER BY token COLLATE "C"),ARRAY[]::TEXT[]),
           COALESCE(array_agg(frequency ORDER BY token COLLATE "C"),ARRAY[]::BIGINT[]),
           COALESCE(array_agg(token ORDER BY token)
               FILTER(WHERE word_count>0 AND token ~ '[_0-9]'),ARRAY[]::TEXT[]),
           COALESCE(array_agg(byte_start ORDER BY token COLLATE "C"),ARRAY[]::INTEGER[]),
           COALESCE(array_agg(byte_length ORDER BY token COLLATE "C"),ARRAY[]::INTEGER[])
      INTO v_token_count,v_terms,v_frequencies,v_word_exact,v_byte_starts,v_byte_lengths
      FROM frequency;
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

    -- Small documents retain the established compact representation. Large
    -- new documents retain bounded short terms and exact byte locators for all terms.
    IF octet_length(p_search_text)>=262144 THEN
        INSERT INTO storage_v2_byte_posting_block(
            document_id,block_order,cached_terms,text_byte_starts,text_byte_lengths,term_frequencies,
            fingerprints,cache_max_term_bytes)
        SELECT v_document.id,(posting.ordinal-1)/256,
               array_agg(CASE WHEN octet_length(posting.term)<=16 THEN posting.term ELSE NULL END ORDER BY posting.ordinal),
               array_agg(posting.byte_start ORDER BY posting.ordinal),
               array_agg(posting.byte_length ORDER BY posting.ordinal),
               array_agg(posting.frequency ORDER BY posting.ordinal),
               storage_v2_posting_fingerprints(array_agg(posting.term ORDER BY posting.ordinal)),16::SMALLINT
          FROM unnest(v_terms,v_frequencies,v_byte_starts,v_byte_lengths)
               WITH ORDINALITY posting(term,frequency,byte_start,byte_length,ordinal)
         GROUP BY (posting.ordinal-1)/256;
    ELSE
        INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies)
        SELECT v_document.id,(posting.ordinal-1)/256,
               array_agg(posting.term ORDER BY posting.ordinal),
               array_agg(posting.frequency ORDER BY posting.ordinal)
          FROM unnest(v_terms,v_frequencies) WITH ORDINALITY posting(term,frequency,ordinal)
         GROUP BY (posting.ordinal-1)/256;
    END IF;
    PERFORM storage_v2_seal_document_postings(v_document.id);
    RETURN v_document;
END
$function$

;
COMMIT;
