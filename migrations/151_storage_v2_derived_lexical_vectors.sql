-- Reconstruct weighted lexical vectors from validated canonical byte slices.
-- Cached fingerprints prune blocks; exact vectors still decide every match.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $guard$
DECLARE expected RECORD;
BEGIN
 FOR expected IN SELECT * FROM (VALUES
  ('storage_v2_verify_generation(bigint,text)','fbb163aeb0f33863ba2a652bf11ad44cc38cc849d6958265ff47f1e82a82f9ff','mainrag'),
  ('storage_v2_requalify_generation(bigint,text)','0480858def9e8a96f6f17418ef6bfed512cfa09ea93203d89853dfbb62c8004a','mainrag'),
  ('storage_v2_source_segment_presence(bigint[])','7947e57eecb8ac8afbf147a38331a506ef014aaa1c2794f1a76c73c66312a843','mainrag_v2_presence_owner'),
  ('storage_v2_source_legacy_segment_matches(bigint,text)','7d7268a94880d36e15d932351d6fe5e09cbd66745b7003aac7b7fd5accb38e74','mainrag_v2_frontier_owner'),
  ('storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])','8b850ab6ae0227bbde08d6b0a8c324d273dbc8de7b29099de18d1d87c73c5832','mainrag_v2_frontier_owner'),
  ('storage_v2_authorized_lexical_matches(bigint[],bigint[],text)','f1f6d7420fb8df41288c28bb3f1424565497d55be2d06cd2c694e313b8eed6b0','mainrag_v2_lexical_rank_owner'),
  ('storage_v2_guard_flat_lexical_insert()','c86bf193fbbcfcd7d988c5ff6db6fedde7c3bb99b0bc83fa042f1109987635de','mainrag_v2_frontier_owner'),
  ('storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)','e6f568994eba664dfa80fa2e720ddb91abd472a1e30dd2ff54730c8992357114','mainrag_v2_lexical_rank_owner'),
  ('storage_v2_source_segment_rank_candidates(bigint[],text)','31c4665d1b7f168136053e16ebd8994278063812e144a0ae667b9cc4fc8bf6fd','mainrag_v2_frontier_owner'),
  ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','068ab0fac3b3371b84c7e2d8929a01f9cc49d20ba7fdebad492d5e3c4a9aef6d','mainrag_v2_frontier_owner'),
  ('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)','4f8ae777dc17b5ca3cc0a4b993905b037128d2ca0d2fda3c781a5bb50add85ea','mainrag_v2_lexical_rank_owner')) x(signature,sha256,owner_name) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(expected.signature::REGPROCEDURE),'UTF8')),'hex')
       IS DISTINCT FROM expected.sha256
     OR (SELECT proowner FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE)<>expected.owner_name::REGROLE THEN
   RAISE EXCEPTION 'derived lexical predecessor definition or authority differs: %',expected.signature;
  END IF;
 END LOOP;
END $guard$;

CREATE TABLE storage_v2_derived_lexical_block
    (LIKE storage_v2_compact_lexical_block INCLUDING CONSTRAINTS);
ALTER TABLE storage_v2_derived_lexical_block DROP COLUMN fts_vectors;
ALTER TABLE storage_v2_derived_lexical_block
    ADD COLUMN text_byte_starts INTEGER[] NOT NULL,
    ADD COLUMN text_byte_lengths INTEGER[] NOT NULL,
    ADD COLUMN segment_masks BIT(128)[],
    ADD PRIMARY KEY(occurrence_id,block_order),
    ADD FOREIGN KEY(occurrence_id,source_id,artifact_version_id)
        REFERENCES occurrence(id,source_id,artifact_version_id) ON DELETE RESTRICT,
    ADD CHECK(array_ndims(text_byte_starts)=1 AND array_lower(text_byte_starts,1)=1
        AND cardinality(text_byte_starts)=cardinality(segment_orders)
        AND array_position(text_byte_starts,NULL) IS NULL AND 0<ALL(text_byte_starts)),
    ADD CHECK(array_ndims(text_byte_lengths)=1 AND array_lower(text_byte_lengths,1)=1
        AND cardinality(text_byte_lengths)=cardinality(segment_orders)
        AND array_position(text_byte_lengths,NULL) IS NULL AND 0<ALL(text_byte_lengths)),
    ADD CHECK(segment_masks IS NULL OR (array_ndims(segment_masks)=1
        AND array_lower(segment_masks,1)=1 AND array_position(segment_masks,NULL) IS NULL
        AND cardinality(segment_masks)=cardinality(segment_orders)));
ALTER TABLE storage_v2_derived_lexical_block OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_derived_lexical_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_derived_lexical_block FORCE ROW LEVEL SECURITY;
CREATE POLICY storage_v2_derived_lexical_source ON storage_v2_derived_lexical_block
    USING(storage_v2_can_access_source(source_id,'read'))
    WITH CHECK(storage_v2_can_access_source(source_id,'write'));
CREATE POLICY storage_v2_derived_lexical_rank_reader ON storage_v2_derived_lexical_block
    FOR SELECT TO mainrag_v2_lexical_rank_owner USING(TRUE);
CREATE POLICY storage_v2_derived_lexical_presence_reader ON storage_v2_derived_lexical_block
    FOR SELECT TO mainrag_v2_presence_owner USING(TRUE);
CREATE INDEX idx_storage_v2_derived_lexical_fingerprint
    ON storage_v2_derived_lexical_block USING GIN(fingerprints);
CREATE INDEX idx_storage_v2_derived_lexical_source
    ON storage_v2_derived_lexical_block(source_id,occurrence_id);
REVOKE ALL ON storage_v2_derived_lexical_block FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_derived_lexical_block
    TO mainrag,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;
CREATE TRIGGER storage_v2_derived_lexical_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_derived_lexical_block FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

-- Two bits per complete lexical fingerprint form a bounded necessary test.
-- Collisions only admit extra exact work. No mask ever establishes a match.
CREATE FUNCTION storage_v2_lexical_segment_mask(p_fingerprints INTEGER[])
RETURNS BIT(128) LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $$
DECLARE mask BIT(128):=B'0'::BIT(128); fingerprint INTEGER;
BEGIN
 FOREACH fingerprint IN ARRAY p_fingerprints LOOP
  mask:=set_bit(set_bit(mask,(fingerprint::BIGINT&127)::INTEGER,1),
                          ((fingerprint::BIGINT>>7)&127)::INTEGER,1);
 END LOOP;
 RETURN mask;
END $$;
ALTER FUNCTION storage_v2_lexical_segment_mask(INTEGER[]) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_lexical_segment_mask(INTEGER[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_lexical_segment_mask(INTEGER[])
    TO mainrag,mainrag_v2_lexical_rank_owner;

CREATE FUNCTION storage_v2_derived_lexical_vectors(
    p_occurrence_id BIGINT,p_starts INTEGER[],p_lengths INTEGER[],
    p_prefixes TEXT[],p_types TEXT[]
) RETURNS TSVECTOR[] LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE bytes BYTEA; source BIGINT; vectors TSVECTOR[];
BEGIN
 -- A view can project a vector before its caller's authorization join runs.
 -- Return no vector for a hidden source; missing authorized data still fails.
 SELECT occurrence.source_id INTO source FROM occurrence WHERE id=p_occurrence_id;
 IF NOT FOUND OR storage_v2_can_access_source(source,'read') IS DISTINCT FROM TRUE THEN
  RETURN NULL;
 END IF;
 SELECT convert_to(document.search_text,'UTF8') INTO bytes
   FROM occurrence
   JOIN artifact_version artifact ON artifact.id=occurrence.artifact_version_id
   JOIN storage_v2_search_view_document binding ON binding.view_id=occurrence.view_id AND binding.ordinal=0
   JOIN storage_v2_search_document document ON document.id=binding.document_id
    AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
  WHERE occurrence.id=p_occurrence_id;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'authorized canonical lexical document required' USING ERRCODE='42501';
 END IF;
 SELECT array_agg(setweight(to_tsvector('simple',convert_from(
            substring(bytes FROM item.start FOR item.length),'UTF8') COLLATE "default"),'A')
        ||setweight(to_tsvector('simple',item.prefix),'B')
        ||setweight(to_tsvector('simple',item.kind),'C') ORDER BY item.ordinal) INTO vectors
   FROM unnest(p_starts,p_lengths,p_prefixes,p_types) WITH ORDINALITY item(start,length,prefix,kind,ordinal);
 RETURN vectors;
END $$;
ALTER FUNCTION storage_v2_derived_lexical_vectors(BIGINT,INTEGER[],INTEGER[],TEXT[],TEXT[])
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_derived_lexical_vectors(BIGINT,INTEGER[],INTEGER[],TEXT[],TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_derived_lexical_vectors(BIGINT,INTEGER[],INTEGER[],TEXT[],TEXT[])
    TO mainrag,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;

-- Validate physical pruning metadata at verification/requalification, before
-- metadata publication. Search still reconstructs exact full vectors, without
-- repeating an immutable cache validation for every ranked block.
CREATE FUNCTION storage_v2_verify_derived_lexical_masks(p_occurrence_id BIGINT)
RETURNS VOID LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on SET jit=off AS $$
DECLARE source BIGINT; bytes BYTEA; block RECORD; expected BIT(128)[];
BEGIN
 SELECT occurrence.source_id INTO source FROM public.occurrence WHERE id=p_occurrence_id;
 IF NOT FOUND OR storage_v2_can_access_source(source,'write') IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'lexical mask verification requires source write authority' USING ERRCODE='42501';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.storage_v2_derived_lexical_block stored
       WHERE stored.occurrence_id=p_occurrence_id AND stored.segment_masks IS NOT NULL) THEN RETURN; END IF;
 SELECT convert_to(document.search_text,'UTF8') INTO bytes FROM public.occurrence occurrence
 JOIN public.artifact_version artifact ON artifact.id=occurrence.artifact_version_id
 JOIN public.storage_v2_search_view_document binding ON binding.view_id=occurrence.view_id AND binding.ordinal=0
 JOIN public.storage_v2_search_document document ON document.id=binding.document_id
  AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
 WHERE occurrence.id=p_occurrence_id;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'authorized canonical lexical document required' USING ERRCODE='42501';
 END IF;
 FOR block IN SELECT * FROM public.storage_v2_derived_lexical_block stored
   WHERE stored.occurrence_id=p_occurrence_id AND stored.segment_masks IS NOT NULL ORDER BY block_order LOOP
  SELECT array_agg(storage_v2_lexical_segment_mask(storage_v2_posting_fingerprints(tsvector_to_array(
       setweight(to_tsvector('simple',convert_from(substring(bytes FROM item.start FOR item.length),'UTF8') COLLATE "default"),'A')
       ||setweight(to_tsvector('simple',item.prefix),'B')||setweight(to_tsvector('simple',item.kind),'C')))) ORDER BY item.ordinal)
   INTO expected FROM unnest(block.text_byte_starts,block.text_byte_lengths,block.context_prefixes,block.chunk_types)
       WITH ORDINALITY item(start,length,prefix,kind,ordinal);
  IF block.segment_masks IS DISTINCT FROM expected THEN
   RAISE EXCEPTION 'derived lexical segment mask differs from complete vector';
  END IF;
 END LOOP;
END $$;
ALTER FUNCTION storage_v2_verify_derived_lexical_masks(BIGINT) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_verify_derived_lexical_masks(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_verify_derived_lexical_masks(BIGINT) TO mainrag;

-- The existing trusted rank owner has a SELECT-only block policy. Keep its
-- access behind an explicit source and complete occurrence identity check,
-- evaluated once per invocation instead of once per candidate block.
CREATE FUNCTION storage_v2_derived_lexical_candidate_blocks(
    p_occurrence_id BIGINT,p_source_id BIGINT,p_artifact_version_id BIGINT,
    p_fingerprints INTEGER[]
) RETURNS SETOF storage_v2_derived_lexical_block
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on
SET plan_cache_mode=force_generic_plan SET jit=off AS $$
 SELECT stored.* FROM public.storage_v2_derived_lexical_block stored
 WHERE (SELECT storage_v2_can_access_source(p_source_id,'read'))
   AND stored.occurrence_id=p_occurrence_id AND stored.source_id=p_source_id
   AND stored.artifact_version_id=p_artifact_version_id
   AND (p_fingerprints IS NULL OR stored.fingerprints @> p_fingerprints)
 ORDER BY stored.block_order
$$;
ALTER FUNCTION storage_v2_derived_lexical_candidate_blocks(BIGINT,BIGINT,BIGINT,INTEGER[])
    OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_derived_lexical_candidate_blocks(BIGINT,BIGINT,BIGINT,INTEGER[])
    FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_derived_lexical_candidate_blocks(BIGINT,BIGINT,BIGINT,INTEGER[])
    TO mainrag_v2_frontier_owner;

-- Resolve the authorized occurrence set before opening canonical documents.
-- Sparse/dense occurrence operands remain deferred; repeated and null IDs do
-- not multiply identities. Sources without derived matches never enter a loop.
CREATE FUNCTION storage_v2_derived_lexical_candidate_occurrences(
    p_occurrence_ids BIGINT[],p_source_ids BIGINT[],p_fingerprints INTEGER[]
) RETURNS TABLE(occurrence_id BIGINT,source_id BIGINT,artifact_version_id BIGINT,
                first_block storage_v2_derived_lexical_block)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on
SET plan_cache_mode=force_custom_plan SET jit=off AS $$
 WITH authorized_sources AS MATERIALIZED (
  SELECT DISTINCT id FROM unnest(p_source_ids) input(id)
   WHERE storage_v2_can_access_source(id,'read')
 )
 SELECT DISTINCT ON(stored.occurrence_id,stored.source_id,stored.artifact_version_id)
   stored.occurrence_id,stored.source_id,stored.artifact_version_id,stored AS first_block
 FROM public.storage_v2_derived_lexical_block stored
 JOIN authorized_sources source ON source.id=stored.source_id
 WHERE stored.occurrence_id=ANY((SELECT p_occurrence_ids OFFSET 0)::BIGINT[])
   AND (p_fingerprints IS NULL OR stored.fingerprints @> p_fingerprints)
 ORDER BY stored.occurrence_id,stored.source_id,stored.artifact_version_id,stored.block_order
$$;
ALTER FUNCTION storage_v2_derived_lexical_candidate_occurrences(BIGINT[],BIGINT[],INTEGER[])
    OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_derived_lexical_candidate_occurrences(BIGINT[],BIGINT[],INTEGER[])
    FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_derived_lexical_candidate_occurrences(BIGINT[],BIGINT[],INTEGER[])
    TO mainrag_v2_frontier_owner;

-- Continue only after the batched first block has no exact match. The primary
-- key bounds each next lookup; no earlier block is decoded again.
CREATE FUNCTION storage_v2_derived_lexical_next_block(
    p_occurrence_id BIGINT,p_source_id BIGINT,p_artifact_version_id BIGINT,
    p_fingerprints INTEGER[],p_after_block BIGINT
) RETURNS SETOF storage_v2_derived_lexical_block
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on
SET plan_cache_mode=force_generic_plan SET jit=off AS $$
 SELECT stored.* FROM public.storage_v2_derived_lexical_block stored
 WHERE (SELECT storage_v2_can_access_source(p_source_id,'read'))
   AND stored.occurrence_id=p_occurrence_id AND stored.source_id=p_source_id
   AND stored.artifact_version_id=p_artifact_version_id AND stored.block_order>p_after_block
   AND (p_fingerprints IS NULL OR stored.fingerprints @> p_fingerprints)
 ORDER BY stored.block_order LIMIT 1
$$;
ALTER FUNCTION storage_v2_derived_lexical_next_block(BIGINT,BIGINT,BIGINT,INTEGER[],BIGINT)
    OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_derived_lexical_next_block(BIGINT,BIGINT,BIGINT,INTEGER[],BIGINT)
    FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_derived_lexical_next_block(BIGINT,BIGINT,BIGINT,INTEGER[],BIGINT)
    TO mainrag_v2_frontier_owner;

-- First-candidate ranking needs the earliest matching segment, not every
-- reconstructed vector. Grow a bounded canonical prefix per occurrence and
-- stop after that first exact weighted match.
CREATE FUNCTION storage_v2_derived_lexical_first_candidates(
    p_occurrence_ids BIGINT[],p_source_ids BIGINT[],p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT,source_id BIGINT,artifact_version_id BIGINT,
                segment_order BIGINT,lexical_score REAL)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on
SET plan_cache_mode=force_custom_plan SET jit=off AS $$
DECLARE sources BIGINT[]; requested RECORD; block RECORD; item RECORD;
        bytes BYTEA; vector TSVECTOR; query TSQUERY:=websearch_to_tsquery('simple',p_query);
        v_fingerprints INTEGER[]; query_mask BIT(128);
        canonical_document_id BIGINT; canonical_document_bytes BIGINT;
        prefix_characters BIGINT; required_bytes BIGINT;
        cached_prefix TEXT; cached_kind TEXT; metadata_cached BOOLEAN:=FALSE;
        prefix_vector TSVECTOR; kind_vector TSVECTOR; matched BOOLEAN;
BEGIN
 IF cardinality(p_occurrence_ids)=0 OR cardinality(p_source_ids)=0 THEN RETURN; END IF;
 SELECT array_agg(source.id) INTO sources FROM public.sources source
  WHERE source.id=ANY(p_source_ids) AND storage_v2_can_access_source(source.id,'read');
 IF sources IS NULL THEN RETURN; END IF;
 IF p_query ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
    AND lower(p_query) !~ '(^|[[:space:]])or([[:space:]]|$)' THEN
  v_fingerprints:=storage_v2_posting_fingerprints(tsvector_to_array(to_tsvector('simple',p_query)));
  query_mask:=storage_v2_lexical_segment_mask(v_fingerprints);
 END IF;
 -- Batch the earliest fingerprint-compatible block and its canonical window.
 -- Metadata-only positive matches still avoid reading any canonical body;
 -- later blocks are fetched only when the first has no exact segment match.
 FOR requested IN SELECT occurrence.id,occurrence.source_id,occurrence.artifact_version_id,
                         document.id AS document_id,
                         octet_length(document.search_text) AS document_bytes,
                         candidate.first_block,
                         CASE WHEN v_fingerprints IS NOT NULL AND (
                           setweight(to_tsvector('simple',(candidate.first_block).context_prefixes[1]),'B')
                           ||setweight(to_tsvector('simple',(candidate.first_block).chunk_types[1]),'C'))@@query
                          THEN NULL ELSE convert_to(substring(document.search_text FROM 1
                           FOR least(octet_length(document.search_text),(
                            SELECT max(window_bounds.start::BIGINT+window_bounds.length-1)
                             FROM unnest((candidate.first_block).text_byte_starts,
                                         (candidate.first_block).text_byte_lengths) window_bounds(start,length)))::INTEGER),
                           'UTF8') END AS initial_bytes
  FROM public.storage_v2_derived_lexical_candidate_occurrences(
      p_occurrence_ids,sources,v_fingerprints) candidate
  JOIN public.occurrence occurrence ON occurrence.id=candidate.occurrence_id
    AND occurrence.source_id=candidate.source_id
    AND occurrence.artifact_version_id=candidate.artifact_version_id
  JOIN public.artifact_version artifact
    ON artifact.id=occurrence.artifact_version_id
  LEFT JOIN public.storage_v2_search_view_document binding
    ON binding.view_id=occurrence.view_id AND binding.ordinal=0
  LEFT JOIN public.storage_v2_search_document document ON document.id=binding.document_id
    AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
 LOOP
  IF requested.document_id IS NULL THEN
   RAISE EXCEPTION 'authorized canonical lexical document required' USING ERRCODE='42501';
  END IF;
  bytes:=requested.initial_bytes;canonical_document_id:=requested.document_id;
  canonical_document_bytes:=requested.document_bytes;prefix_characters:=coalesce(octet_length(bytes),0);
  block:=requested.first_block;
  <<blocks>>
  LOOP
   FOR item IN SELECT * FROM unnest(block.segment_orders,block.text_byte_starts,
      block.text_byte_lengths,block.context_prefixes,block.chunk_types,block.segment_masks)
      segment(segment_order,start,length,prefix,kind,mask) ORDER BY segment.segment_order
   LOOP
    required_bytes:=item.start+item.length-1;
    IF required_bytes>canonical_document_bytes THEN
     RAISE EXCEPTION 'canonical lexical byte range exceeds document';
    END IF;
    CONTINUE WHEN query_mask IS NOT NULL AND item.mask IS NOT NULL
                  AND (item.mask&query_mask)<>query_mask;
    IF NOT metadata_cached OR (cached_prefix,cached_kind) IS DISTINCT FROM (item.prefix,item.kind) THEN
     prefix_vector:=setweight(to_tsvector('simple',item.prefix),'B');
     kind_vector:=setweight(to_tsvector('simple',item.kind),'C');
     cached_prefix:=item.prefix;cached_kind:=item.kind;metadata_cached:=TRUE;
    END IF;
    -- Adding body lexemes cannot invalidate a positive, position-free
    -- conjunction already matched by context/type. Complex predicates still
    -- evaluate the complete vector, with the original concatenation order.
    matched:=v_fingerprints IS NOT NULL AND ((prefix_vector||kind_vector)@@query);
    IF matched IS DISTINCT FROM TRUE THEN
     IF bytes IS NULL OR octet_length(bytes)<required_bytes THEN
     -- A UTF-8 byte end is also a safe upper bound on the character prefix.
     -- PostgreSQL substring can fetch/decompress a bounded TOAST slice. Grow
     -- geometrically for later matches while retaining the original byte offsets.
     prefix_characters:=greatest(8192::BIGINT,required_bytes,prefix_characters*2);
     SELECT convert_to(substring(document.search_text FROM 1
          FOR least(prefix_characters,canonical_document_bytes)::INTEGER),'UTF8') INTO bytes
      FROM public.storage_v2_search_document document
      WHERE document.id=canonical_document_id;
     IF NOT FOUND THEN
      RAISE EXCEPTION 'authorized canonical lexical document required' USING ERRCODE='42501';
     END IF;
     IF octet_length(bytes)<required_bytes THEN
      RAISE EXCEPTION 'canonical lexical byte range exceeds document';
     END IF;
     END IF;
     vector:=setweight(to_tsvector('simple',convert_from(
        substring(bytes FROM item.start FOR item.length),'UTF8') COLLATE "default"),'A')
       ||prefix_vector||kind_vector;
     matched:=vector@@query;
    END IF;
    IF matched THEN
     occurrence_id:=requested.id;source_id:=requested.source_id;
     artifact_version_id:=requested.artifact_version_id;
     segment_order:=item.segment_order;lexical_score:=0.0;
     RETURN NEXT;
     EXIT blocks;
    END IF;
   END LOOP;
   SELECT stored.* INTO block FROM public.storage_v2_derived_lexical_next_block(
       requested.id,requested.source_id,requested.artifact_version_id,v_fingerprints,block.block_order) stored;
   IF NOT FOUND THEN EXIT blocks; END IF;
  END LOOP blocks;
 END LOOP;
END $$;
ALTER FUNCTION storage_v2_derived_lexical_first_candidates(BIGINT[],BIGINT[],TEXT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_derived_lexical_first_candidates(BIGINT[],BIGINT[],TEXT)
    FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_derived_lexical_first_candidates(BIGINT[],BIGINT[],TEXT)
    TO mainrag_v2_lexical_rank_owner;

CREATE VIEW storage_v2_lexical_block_all WITH(security_invoker=true) AS
 SELECT occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
        text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fts_vectors,fingerprints
   FROM storage_v2_compact_lexical_block
 UNION ALL
 SELECT occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
        text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,
        storage_v2_derived_lexical_vectors(occurrence_id,text_byte_starts,text_byte_lengths,
            context_prefixes,chunk_types),fingerprints
   FROM storage_v2_derived_lexical_block;
ALTER VIEW storage_v2_lexical_block_all OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON storage_v2_lexical_block_all FROM PUBLIC;
GRANT SELECT ON storage_v2_lexical_block_all
    TO mainrag,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;

-- Preserve the existing logical segment view and every old vector row.
CREATE OR REPLACE VIEW storage_v2_lexical_segment_all WITH(security_invoker=true) AS
 SELECT * FROM storage_v2_lexical_segment
 UNION ALL
 SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
        item.segment_order,item.text_start::BIGINT,item.text_length::BIGINT,
        item.text_sha256,item.context_prefix,item.chunk_type,item.fts_vector
   FROM storage_v2_lexical_block_all block
   CROSS JOIN LATERAL unnest(block.segment_orders,block.text_starts,block.text_lengths,
       block.text_hashes,block.context_prefixes,block.chunk_types,block.fts_vectors)
       item(segment_order,text_start,text_length,text_sha256,context_prefix,chunk_type,fts_vector);

DO $readers$
DECLARE signature TEXT; definition TEXT;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_source_segment_presence(bigint[])',
  'storage_v2_source_legacy_segment_matches(bigint,text)',
  'storage_v2_authorized_lexical_matches(bigint[],bigint[],text)',
  'storage_v2_guard_flat_lexical_insert()',
  'storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);
  IF strpos(definition,'storage_v2_compact_lexical_block')=0 THEN
   RAISE EXCEPTION 'derived lexical reader replacement boundary differs';
  END IF;
  EXECUTE replace(definition,'storage_v2_compact_lexical_block','storage_v2_lexical_block_all');
 END LOOP;
END $readers$;

DO $first_reader$
DECLARE definition TEXT;
        signature TEXT:='CREATE OR REPLACE FUNCTION public.storage_v2_authorized_lexical_first_candidates(';
BEGIN
 definition:=pg_get_functiondef('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
 IF strpos(definition,signature)=0 OR strpos(definition,'storage_v2_compact_lexical_block')=0 THEN
  RAISE EXCEPTION 'derived lexical first-candidate boundary differs';
 END IF;
 EXECUTE replace(definition,signature,
  'CREATE OR REPLACE FUNCTION public.storage_v2_authorized_cached_lexical_first_candidates(');
END $first_reader$;
ALTER FUNCTION storage_v2_authorized_cached_lexical_first_candidates(BIGINT[],BIGINT[],TEXT)
    OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_authorized_cached_lexical_first_candidates(BIGINT[],BIGINT[],TEXT)
    FROM PUBLIC,mainrag;

-- An occurrence can retain older cached blocks alongside new derived blocks.
-- Reduce their first matches together, preserving one exact minimum identity.
CREATE OR REPLACE FUNCTION storage_v2_authorized_lexical_first_candidates(
    p_occurrence_ids BIGINT[],p_source_ids BIGINT[],p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT,source_id BIGINT,artifact_version_id BIGINT,
                segment_order BIGINT,lexical_score REAL)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on
SET plan_cache_mode=force_custom_plan AS $$
 SELECT candidate.occurrence_id,candidate.source_id,candidate.artifact_version_id,
        min(candidate.segment_order),0.0::REAL
 FROM (
  SELECT * FROM storage_v2_authorized_cached_lexical_first_candidates(p_occurrence_ids,p_source_ids,p_query)
  UNION ALL
  SELECT * FROM storage_v2_derived_lexical_first_candidates(p_occurrence_ids,p_source_ids,p_query)
 ) candidate
 GROUP BY candidate.occurrence_id,candidate.source_id,candidate.artifact_version_id
$$;

CREATE OR REPLACE FUNCTION public.storage_v2_put_lexical_segments_located(p_occurrence_id bigint, p_artifact_version_id bigint, p_segment_orders bigint[], p_texts text[], p_context_prefixes text[], p_chunk_types text[], p_character_starts bigint[], p_byte_starts bigint[])
 RETURNS bigint
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'on'
AS $function$
DECLARE
    v_source_id BIGINT;
    v_search_text TEXT;
    v_source_bytes BYTEA;
    v_byte_window_start BIGINT;
    v_byte_window_end BIGINT;
    v_window_start BIGINT;
    v_window_end BIGINT;
    v_count INTEGER;
    v_distinct INTEGER;
    v_valid BOOLEAN;
    v_inserted INTEGER;
    v_starts INTEGER[];
    v_lengths INTEGER[];
    v_hashes BYTEA[];
    v_vectors TSVECTOR[];
    v_masks BIT(128)[];
    v_existing_derived public.storage_v2_derived_lexical_block;
    v_byte_lengths INTEGER[];
    v_low INTEGER;
    v_high INTEGER;
    v_existing_block public.storage_v2_compact_lexical_block;
BEGIN
    v_count := cardinality(p_segment_orders);
    IF v_count IS NULL OR v_count < 1 OR v_count > 256
       OR cardinality(p_texts) IS DISTINCT FROM v_count
       OR cardinality(p_context_prefixes) IS DISTINCT FROM v_count
       OR cardinality(p_chunk_types) IS DISTINCT FROM v_count
       OR cardinality(p_character_starts) IS DISTINCT FROM v_count
       OR cardinality(p_byte_starts) IS DISTINCT FROM v_count THEN
        RAISE EXCEPTION 'bounded lexical segment group required';
    END IF;
    SELECT occurrence_row.source_id, document.search_text
      INTO v_source_id, v_search_text
      FROM occurrence occurrence_row
      JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
      JOIN storage_v2_search_document document ON document.id=binding.document_id
       AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
     WHERE occurrence_row.id=p_occurrence_id
       AND occurrence_row.artifact_version_id=p_artifact_version_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write') THEN
        RAISE EXCEPTION 'authorized source-backed lexical segment required'
            USING ERRCODE='42501';
    END IF;
    v_source_bytes := convert_to(v_search_text, 'UTF8');
    SELECT min(character_start), max(character_start + char_length(segment_text) - 1),
           min(byte_start), max(byte_start + octet_length(segment_text) - 1),
           bool_and(COALESCE(character_start BETWEEN 1 AND 2147483647
                    AND byte_start BETWEEN 1 AND 2147483647
                    AND segment_text <> '', FALSE))
      INTO v_window_start, v_window_end, v_byte_window_start, v_byte_window_end, v_valid
      FROM unnest(p_character_starts, p_byte_starts, p_texts)
           AS bounds(character_start, byte_start, segment_text);
    IF v_valid IS NOT TRUE OR v_window_start IS NULL
       OR v_window_end > char_length(v_search_text)
       OR v_window_end - v_window_start + 1 > 8388608
       OR v_byte_window_end > octet_length(v_source_bytes)
       OR v_byte_window_end - v_byte_window_start + 1 > 33554432 THEN
        RAISE EXCEPTION 'bounded source character and byte window required';
    END IF;
    -- Each disjoint prefix interval is decoded once. This proves the supplied
    -- byte-to-character mapping independently, including duplicate locators.
    -- A byte inside a UTF-8 code point fails convert_from instead of being
    -- rounded or accepted. Byte substrings avoid repeated character-prefix scans.
    WITH boundaries AS MATERIALIZED (
        SELECT DISTINCT byte_start FROM unnest(p_byte_starts) AS input(byte_start)
    ), intervals AS MATERIALIZED (
        SELECT byte_start, lag(byte_start, 1, 1::BIGINT)
                   OVER (ORDER BY byte_start) AS previous_byte_start
          FROM boundaries
    ), mapped AS MATERIALIZED (
        SELECT byte_start,
               1 + sum(char_length(convert_from(substring(v_source_bytes
                   FROM previous_byte_start::INTEGER
                   FOR (byte_start - previous_byte_start)::INTEGER), 'UTF8')))
                   OVER (ORDER BY byte_start) AS character_start
          FROM intervals
    ), prepared AS MATERIALIZED (
        SELECT input.*, input.character_start::INTEGER AS text_start,
               char_length(segment_text) AS text_length,
               byte_start::INTEGER AS text_byte_start,
               octet_length(segment_text) AS text_byte_length,
               mapped.character_start AS verified_character_start,
               sha256(convert_to(segment_text, 'UTF8')) AS text_sha256,
               setweight(to_tsvector('simple', segment_text), 'A')
               || setweight(to_tsvector('simple', context_prefix), 'B')
               || setweight(to_tsvector('simple', chunk_type), 'C') AS fts_vector
          FROM unnest(p_segment_orders, p_texts, p_context_prefixes, p_chunk_types,
                      p_character_starts, p_byte_starts)
               WITH ORDINALITY AS input(segment_order, segment_text, context_prefix,
                   chunk_type, character_start, byte_start, ordinal)
          JOIN mapped USING (byte_start)
    )
    SELECT count(DISTINCT segment_order),
           bool_and(COALESCE(segment_order >= 0 AND segment_text <> ''
                    AND context_prefix IS NOT NULL AND chunk_type IS NOT NULL
                    AND chunk_type <> '' AND text_start = verified_character_start
                    AND substring(v_source_bytes FROM text_byte_start
                                  FOR text_byte_length) = convert_to(segment_text, 'UTF8'), FALSE)),
           array_agg(text_start ORDER BY ordinal), array_agg(text_length ORDER BY ordinal),
           array_agg(text_sha256 ORDER BY ordinal), array_agg(fts_vector ORDER BY ordinal)
      INTO v_distinct, v_valid, v_starts, v_lengths, v_hashes, v_vectors FROM prepared;
    IF v_distinct <> v_count OR v_valid IS NOT TRUE THEN
        RAISE EXCEPTION 'valid source-backed lexical segment group required';
    END IF;
    -- Reuse all preceding independent byte/character/text validations.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'mainrag.lexical-segment:'||p_occurrence_id::TEXT,0));
    IF octet_length(v_search_text)>=262144
       AND array_lower(p_segment_orders,1)=1 AND p_segment_orders[1]%64=0
       AND p_segment_orders=ARRAY(SELECT generate_series(p_segment_orders[1],p_segment_orders[1]+v_count-1))
       AND NOT EXISTS(SELECT 1 FROM public.storage_v2_lexical_segment WHERE occurrence_id=p_occurrence_id)
       AND NOT EXISTS(SELECT 1 FROM public.storage_v2_compact_lexical_block WHERE occurrence_id=p_occurrence_id) THEN
        SELECT array_agg(octet_length(value) ORDER BY ordinal) INTO v_byte_lengths
          FROM unnest(p_texts) WITH ORDINALITY item(value,ordinal);
        SELECT array_agg(public.storage_v2_lexical_segment_mask(
                   public.storage_v2_posting_fingerprints(tsvector_to_array(value))) ORDER BY ordinal)
          INTO v_masks FROM unnest(v_vectors) WITH ORDINALITY vector(value,ordinal);
        FOR v_low IN SELECT generate_series(1,v_count,64) LOOP
            v_high:=LEAST(v_low+63,v_count);
            INSERT INTO public.storage_v2_derived_lexical_block(
                occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
                text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,
                fingerprints,text_byte_starts,text_byte_lengths,segment_masks
            ) VALUES(p_occurrence_id,v_source_id,p_artifact_version_id,p_segment_orders[v_low]/64,
                p_segment_orders[v_low:v_high],v_starts[v_low:v_high],v_lengths[v_low:v_high],
                v_hashes[v_low:v_high],p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],
                public.storage_v2_lexical_block_fingerprints(v_vectors[v_low:v_high]),
                p_byte_starts[v_low:v_high]::INTEGER[],v_byte_lengths[v_low:v_high],v_masks[v_low:v_high])
            ON CONFLICT(occurrence_id,block_order) DO NOTHING;
            SELECT * INTO STRICT v_existing_derived FROM public.storage_v2_derived_lexical_block
             WHERE occurrence_id=p_occurrence_id AND block_order=p_segment_orders[v_low]/64;
            IF (v_existing_derived.source_id,v_existing_derived.artifact_version_id,
                v_existing_derived.segment_orders,v_existing_derived.text_starts,
                v_existing_derived.text_lengths,v_existing_derived.text_hashes,
                v_existing_derived.context_prefixes,v_existing_derived.chunk_types,
                v_existing_derived.fingerprints,v_existing_derived.text_byte_starts,
                v_existing_derived.text_byte_lengths) IS DISTINCT FROM
               (v_source_id,p_artifact_version_id,p_segment_orders[v_low:v_high],
                v_starts[v_low:v_high],v_lengths[v_low:v_high],v_hashes[v_low:v_high],
                p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],
                public.storage_v2_lexical_block_fingerprints(v_vectors[v_low:v_high]),
                p_byte_starts[v_low:v_high]::INTEGER[],v_byte_lengths[v_low:v_high])
               OR (v_existing_derived.segment_masks IS NOT NULL
                   AND v_existing_derived.segment_masks IS DISTINCT FROM v_masks[v_low:v_high]) THEN
                RAISE EXCEPTION 'lexical segment identity collision';
            END IF;
        END LOOP;
        RETURN v_count;
    END IF;
    -- All payload values were independently validated above.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'mainrag.lexical-segment-source:'||v_source_id::TEXT,0));
    IF NOT EXISTS(SELECT 1 FROM public.storage_v2_derived_lexical_block
                    WHERE occurrence_id=p_occurrence_id
                      AND block_order BETWEEN p_segment_orders[1]/64 AND p_segment_orders[v_count]/64)
       AND array_lower(p_segment_orders,1)=1 AND p_segment_orders[1]%64=0
       AND p_segment_orders=ARRAY(SELECT generate_series(p_segment_orders[1],
                                     p_segment_orders[1]+v_count-1))
       AND NOT EXISTS (SELECT 1 FROM public.storage_v2_lexical_segment old
                        WHERE old.occurrence_id=p_occurrence_id
                          AND old.segment_order=ANY(p_segment_orders))
       AND NOT EXISTS (
           SELECT 1 FROM public.storage_v2_compact_lexical_block block
            WHERE block.occurrence_id=p_occurrence_id
              AND block.block_order BETWEEN p_segment_orders[1]/64
                                        AND p_segment_orders[v_count]/64
              AND cardinality(block.segment_orders)<>
                  LEAST(64,v_count-(block.block_order-p_segment_orders[1]/64)*64)
       ) THEN
        FOR v_low IN SELECT generate_series(1,v_count,64) LOOP
            v_high:=LEAST(v_low+63,v_count);
            INSERT INTO public.storage_v2_compact_lexical_block(
                occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
                text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fts_vectors
            ) VALUES(p_occurrence_id,v_source_id,p_artifact_version_id,p_segment_orders[v_low]/64,
                p_segment_orders[v_low:v_high],v_starts[v_low:v_high],v_lengths[v_low:v_high],
                v_hashes[v_low:v_high],p_context_prefixes[v_low:v_high],
                p_chunk_types[v_low:v_high],v_vectors[v_low:v_high])
            ON CONFLICT(occurrence_id,block_order) DO NOTHING;
            SELECT * INTO STRICT v_existing_block FROM public.storage_v2_compact_lexical_block
             WHERE occurrence_id=p_occurrence_id AND block_order=p_segment_orders[v_low]/64;
            IF (v_existing_block.source_id,v_existing_block.artifact_version_id,
                v_existing_block.segment_orders,v_existing_block.text_starts,
                v_existing_block.text_lengths,v_existing_block.text_hashes,
                v_existing_block.context_prefixes,v_existing_block.chunk_types,
                v_existing_block.fts_vectors) IS DISTINCT FROM
               (v_source_id,p_artifact_version_id,p_segment_orders[v_low:v_high],
                v_starts[v_low:v_high],v_lengths[v_low:v_high],v_hashes[v_low:v_high],
                p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],v_vectors[v_low:v_high]) THEN
                RAISE EXCEPTION 'lexical segment identity collision';
            END IF;
        END LOOP;
        RETURN v_count;
    END IF;
    INSERT INTO storage_v2_lexical_segment (
        occurrence_id, source_id, artifact_version_id, segment_order,
        text_start, text_length, text_sha256, context_prefix, chunk_type, fts_vector
    )
    SELECT p_occurrence_id, v_source_id, p_artifact_version_id, input.*
      FROM unnest(p_segment_orders, v_starts, v_lengths, v_hashes,
                  p_context_prefixes, p_chunk_types, v_vectors) AS input
    ON CONFLICT (occurrence_id, segment_order) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted <> v_count AND EXISTS (
        SELECT 1
          FROM unnest(p_segment_orders, v_starts, v_lengths, v_hashes,
                      p_context_prefixes, p_chunk_types, v_vectors)
               AS input(segment_order, text_start, text_length, text_sha256,
                        context_prefix, chunk_type, fts_vector)
          LEFT JOIN public.storage_v2_lexical_segment_all stored
            ON stored.occurrence_id=p_occurrence_id AND stored.segment_order=input.segment_order
         WHERE (stored.source_id, stored.artifact_version_id, stored.text_start,
                stored.text_length, stored.text_sha256, stored.context_prefix,
                stored.chunk_type, stored.fts_vector) IS DISTINCT FROM
               (v_source_id, p_artifact_version_id, input.text_start, input.text_length,
                input.text_sha256, input.context_prefix, input.chunk_type, input.fts_vector)
    ) THEN
        RAISE EXCEPTION 'lexical segment identity collision';
    END IF;
    RETURN v_count;
END
$function$

;
DO $mask_lifecycle$
DECLARE signature TEXT; definition TEXT;
        boundary TEXT:='    PERFORM public.storage_v2_materialize_reader_metadata(p_generation_id);';
BEGIN
 FOREACH signature IN ARRAY ARRAY['storage_v2_verify_generation(bigint,text)',
                                   'storage_v2_requalify_generation(bigint,text)'] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);
  IF (length(definition)-length(replace(definition,boundary,'')))/length(boundary)<>1 THEN
   RAISE EXCEPTION 'derived lexical mask verification lifecycle boundary differs';
  END IF;
  EXECUTE replace(definition,boundary,
   '    PERFORM public.storage_v2_verify_derived_lexical_masks(o.id) FROM public.occurrence o '
   ||'WHERE o.source_id=v_generation.source_id AND EXISTS(SELECT 1 FROM public.generation_item_version membership '
   ||'WHERE membership.source_id=v_generation.source_id AND membership.artifact_version_id=o.artifact_version_id '
   ||'AND membership.valid_from_seq<=v_generation.generation_seq '
   ||'AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>v_generation.generation_seq));'
   ||E'\n'||boundary);
 END LOOP;
END $mask_lifecycle$;
COMMIT;
