-- Transaction-local document lexical ingest. Existing located API is unchanged.
-- Limits bound this optimization only; callers use the preceding canonical API
-- for larger bodies, scratch payloads, segment counts or unbounded groups.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $predecessor$
DECLARE routine RECORD;
BEGIN
 SELECT * INTO STRICT routine FROM pg_proc WHERE oid=
  'public.storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])'::REGPROCEDURE;
 IF encode(sha256(convert_to(routine.prosrc,'UTF8')),'hex') <>
       '2b0df8f6585cb549e1565ae2b547ef44d2ce9c7c67222560a1fe51005a3a0795'
    OR routine.proowner<>'mainrag_v2_frontier_owner'::REGROLE
    OR NOT routine.prosecdef
    OR routine.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, public','row_security=on']::TEXT[]
    OR EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
              WHERE a.grantee NOT IN (routine.proowner,'mainrag'::REGROLE)
                 OR a.privilege_type<>'EXECUTE' OR a.is_grantable)
    OR pg_has_role('mainrag','mainrag_v2_frontier_owner','MEMBER')
    OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mainrag_v2_frontier_owner'
               AND (rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)) THEN
  RAISE EXCEPTION 'document lexical predecessor or authority differs';
 END IF;
END $predecessor$;

-- Validate catalog authority before reading any caller-addressable temp data.
-- A temp schema owner can drop/replace relations: a same-name table is never
-- proof. Dedicated relation ownership plus exact shape/ACL and absence of
-- executable hooks reject forged tables, views, extra columns and triggers.
CREATE FUNCTION storage_v2_require_lexical_scratch()
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE name TEXT; relation RECORD; names TEXT[]; types OID[]; attributes RECORD;
        wanted_owner OID := 'mainrag_v2_frontier_owner'::REGROLE;
BEGIN
 FOREACH name IN ARRAY ARRAY['mainrag_v2_lexical_context','mainrag_v2_lexical_batches',
                              'mainrag_v2_lexical_anchors'] LOOP
  SELECT * INTO relation FROM pg_class
   WHERE relnamespace=pg_my_temp_schema() AND relname=name;
  IF NOT FOUND OR relation.relkind<>'r' OR relation.relpersistence<>'t'
     OR relation.relowner<>wanted_owner OR relation.relrowsecurity
     OR relation.relforcerowsecurity OR relation.relhasrules
     OR EXISTS(SELECT 1 FROM aclexplode(coalesce(relation.relacl,
                               acldefault('r',relation.relowner))) a
                WHERE a.grantee<>wanted_owner OR a.is_grantable)
     OR EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=relation.oid)
     OR EXISTS(SELECT 1 FROM pg_rewrite WHERE ev_class=relation.oid)
     -- PostgreSQL18 records column NOT NULL in pg_constraint as well. Those
     -- exact column properties are checked below; every other constraint fails.
     OR EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=relation.oid AND contype<>'n')
     OR EXISTS(SELECT 1 FROM pg_policy WHERE polrelid=relation.oid)
     OR EXISTS(SELECT 1 FROM pg_attrdef WHERE adrelid=relation.oid) THEN
   RAISE EXCEPTION 'untrusted lexical scratch relation: %',name USING ERRCODE='42501';
  END IF;
  IF name='mainrag_v2_lexical_context' THEN
   names:=ARRAY['transaction_id','backend_id','actor','run_id','generation_id','source_id',
     'occurrence_id','artifact_version_id','view_id','document_id','content_root_node_id',
     'content_identity_sha256','materialization_sha256','document_profile','body_bytes',
     'expected_count','staged_count','staged_bytes','batch_count'];
   types:=ARRAY['xid8'::REGTYPE,'integer'::REGTYPE,'text'::REGTYPE,
     'bigint'::REGTYPE,'bigint'::REGTYPE,'bigint'::REGTYPE,'bigint'::REGTYPE,
     'bigint'::REGTYPE,'bigint'::REGTYPE,'bigint'::REGTYPE,'bigint'::REGTYPE,
     'bytea'::REGTYPE,'bytea'::REGTYPE,'text'::REGTYPE,'integer'::REGTYPE,
     'integer'::REGTYPE,'integer'::REGTYPE,'bigint'::REGTYPE,'integer'::REGTYPE]::OID[];
  ELSIF name='mainrag_v2_lexical_batches' THEN
   names:=ARRAY['batch_order','segment_orders','texts','context_prefixes','chunk_types',
                'character_starts','byte_starts'];
   types:=ARRAY['integer'::REGTYPE,'bigint[]'::REGTYPE,'text[]'::REGTYPE,
     'text[]'::REGTYPE,'text[]'::REGTYPE,'bigint[]'::REGTYPE,'bigint[]'::REGTYPE]::OID[];
  ELSE
   names:=ARRAY['byte_start','character_start'];
   types:=ARRAY['bigint'::REGTYPE,'bigint'::REGTYPE]::OID[];
  END IF;
  SELECT array_agg(a.attname::TEXT ORDER BY a.attnum) AS names,
         array_agg(a.atttypid ORDER BY a.attnum) AS types,
         bool_and(a.attnotnull AND NOT a.attisdropped AND a.attgenerated=''
              AND a.attidentity='' AND a.atttypmod=-1
              AND a.attcollation=(SELECT t.typcollation FROM pg_type t WHERE t.oid=a.atttypid)) AS valid
    INTO attributes FROM pg_attribute a WHERE a.attrelid=relation.oid AND a.attnum>0;
  IF attributes.names IS DISTINCT FROM names OR attributes.types IS DISTINCT FROM types
     OR attributes.valid IS NOT TRUE OR relation.relnatts<>cardinality(names)
     OR (name<>'mainrag_v2_lexical_anchors'
         AND EXISTS(SELECT 1 FROM pg_index WHERE indrelid=relation.oid))
     OR (name='mainrag_v2_lexical_anchors' AND (
         (SELECT count(*) FROM pg_index WHERE indrelid=relation.oid)<>1
         OR NOT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
           JOIN pg_am am ON am.oid=idx.relam
           WHERE i.indrelid=relation.oid AND i.indisunique AND i.indisvalid AND i.indisready
            AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indkey::TEXT='1'
            AND i.indexprs IS NULL AND i.indpred IS NULL AND am.amname='btree'
            AND idx.relowner=wanted_owner AND idx.relnamespace=pg_my_temp_schema()))) THEN
   RAISE EXCEPTION 'lexical scratch shape differs: %',name USING ERRCODE='42501';
  END IF;
 END LOOP;
END $$;

-- Recheck the original server-bound document and current authority without
-- fetching search_text. Immutable canonical content is fetched only by finish.
CREATE FUNCTION storage_v2_require_lexical_document_context(p_occurrence_id BIGINT,p_artifact_version_id BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE context RECORD;
BEGIN
 PERFORM public.storage_v2_require_lexical_scratch();
 IF (SELECT count(*) FROM pg_temp.mainrag_v2_lexical_context)<>1 THEN
  RAISE EXCEPTION 'one active lexical document context required';
 END IF;
 SELECT * INTO STRICT context FROM pg_temp.mainrag_v2_lexical_context;
 IF context.transaction_id IS DISTINCT FROM pg_current_xact_id()
    OR context.backend_id<>pg_backend_pid()
    OR context.actor IS DISTINCT FROM current_setting('app.user_id',true)
    OR context.occurrence_id IS DISTINCT FROM p_occurrence_id
    OR context.artifact_version_id IS DISTINCT FROM p_artifact_version_id
    OR NOT public.storage_v2_can_access_source(context.source_id,'write')
    OR NOT EXISTS(
      SELECT 1 FROM public.storage_v2_ingest_run run
      JOIN public.storage_v2_ingest_run_item item ON item.run_id=run.id AND item.source_id=run.source_id
      JOIN public.occurrence occurrence_row ON occurrence_row.id=item.occurrence_id
       AND occurrence_row.source_id=item.source_id AND occurrence_row.artifact_version_id=item.artifact_version_id
      JOIN public.artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
      JOIN public.storage_v2_search_view_document binding
        ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
      JOIN public.storage_v2_search_document document ON document.id=binding.document_id
       AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
      WHERE run.id=context.run_id AND run.generation_id=context.generation_id
       AND run.source_id=context.source_id AND run.status='building'
       AND occurrence_row.id=context.occurrence_id
       AND artifact.id=context.artifact_version_id AND occurrence_row.view_id=context.view_id
       AND document.id=context.document_id AND artifact.content_root_node_id=context.content_root_node_id
       AND item.content_identity_sha256=context.content_identity_sha256
       AND document.materialization_sha256=context.materialization_sha256
       AND document.profile_id=context.document_profile
       AND octet_length(document.search_text)=context.body_bytes) THEN
  RAISE EXCEPTION 'authorized bound lexical document context required' USING ERRCODE='42501';
 END IF;
END $$;

CREATE FUNCTION storage_v2_begin_lexical_document_context(
 p_run_id BIGINT,p_occurrence_id BIGINT,p_artifact_version_id BIGINT,p_expected_count INTEGER
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE identity RECORD;
BEGIN
 IF p_expected_count IS NULL OR p_expected_count NOT BETWEEN 1 AND 65536 THEN
  RAISE EXCEPTION 'document lexical optimization segment bound exceeded' USING ERRCODE='54000';
 END IF;
 -- No source body is read or placed in a TOAST-backed temporary relation.
 SELECT run.id AS run_id,run.generation_id,run.source_id,occurrence_row.id AS occurrence_id,
        artifact.id AS artifact_version_id,occurrence_row.view_id,document.id AS document_id,
        artifact.content_root_node_id,item.content_identity_sha256,document.materialization_sha256,
        document.profile_id,octet_length(document.search_text) AS body_bytes
   INTO identity FROM public.storage_v2_ingest_run run
   JOIN public.storage_v2_ingest_run_item item ON item.run_id=run.id AND item.source_id=run.source_id
   JOIN public.occurrence occurrence_row ON occurrence_row.id=item.occurrence_id
    AND occurrence_row.source_id=item.source_id AND occurrence_row.artifact_version_id=item.artifact_version_id
   JOIN public.artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
   JOIN public.storage_v2_search_view_document binding ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
   JOIN public.storage_v2_search_document document ON document.id=binding.document_id
    AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
  WHERE run.id=p_run_id AND run.status='building'
   AND occurrence_row.id=p_occurrence_id AND artifact.id=p_artifact_version_id;
 IF NOT FOUND OR NOT public.storage_v2_can_access_source(identity.source_id,'write')
    OR current_setting('app.user_id',true) IS NULL THEN
  RAISE EXCEPTION 'authorized bound lexical document required' USING ERRCODE='42501';
 END IF;
 IF identity.body_bytes>134217728 THEN
  RAISE EXCEPTION 'document lexical optimization body bound exceeded' USING ERRCODE='54000';
 END IF;
 IF to_regclass('pg_temp.mainrag_v2_lexical_context') IS NULL
    AND to_regclass('pg_temp.mainrag_v2_lexical_batches') IS NULL
    AND to_regclass('pg_temp.mainrag_v2_lexical_anchors') IS NULL THEN
  CREATE TEMP TABLE pg_temp.mainrag_v2_lexical_context(
   transaction_id XID8 NOT NULL,backend_id INTEGER NOT NULL,actor TEXT NOT NULL,
   run_id BIGINT NOT NULL,generation_id BIGINT NOT NULL,source_id BIGINT NOT NULL,
   occurrence_id BIGINT NOT NULL,artifact_version_id BIGINT NOT NULL,view_id BIGINT NOT NULL,
   document_id BIGINT NOT NULL,content_root_node_id BIGINT NOT NULL,
   content_identity_sha256 BYTEA NOT NULL,materialization_sha256 BYTEA NOT NULL,
   document_profile TEXT NOT NULL,body_bytes INTEGER NOT NULL,expected_count INTEGER NOT NULL,
   staged_count INTEGER NOT NULL,staged_bytes BIGINT NOT NULL,batch_count INTEGER NOT NULL
  ) ON COMMIT DROP;
  CREATE TEMP TABLE pg_temp.mainrag_v2_lexical_batches(
   batch_order INTEGER NOT NULL,segment_orders BIGINT[] NOT NULL,texts TEXT[] NOT NULL,
   context_prefixes TEXT[] NOT NULL,chunk_types TEXT[] NOT NULL,
   character_starts BIGINT[] NOT NULL,byte_starts BIGINT[] NOT NULL
  ) ON COMMIT DROP;
  CREATE TEMP TABLE pg_temp.mainrag_v2_lexical_anchors(
   byte_start BIGINT NOT NULL,character_start BIGINT NOT NULL
  ) ON COMMIT DROP;
  CREATE UNIQUE INDEX mainrag_v2_lexical_anchors_byte_start
   ON pg_temp.mainrag_v2_lexical_anchors(byte_start);
  REVOKE ALL ON pg_temp.mainrag_v2_lexical_context,pg_temp.mainrag_v2_lexical_batches,
                pg_temp.mainrag_v2_lexical_anchors FROM PUBLIC,mainrag;
 END IF;
 PERFORM public.storage_v2_require_lexical_scratch();
 IF EXISTS(SELECT 1 FROM pg_temp.mainrag_v2_lexical_context)
    OR EXISTS(SELECT 1 FROM pg_temp.mainrag_v2_lexical_batches)
    OR EXISTS(SELECT 1 FROM pg_temp.mainrag_v2_lexical_anchors) THEN
  RAISE EXCEPTION 'one active lexical document context required';
 END IF;
 INSERT INTO pg_temp.mainrag_v2_lexical_context VALUES(
  pg_current_xact_id(),pg_backend_pid(),current_setting('app.user_id',true),
  identity.run_id,identity.generation_id,identity.source_id,identity.occurrence_id,
  identity.artifact_version_id,identity.view_id,identity.document_id,identity.content_root_node_id,
  identity.content_identity_sha256,identity.materialization_sha256,identity.profile_id,
  identity.body_bytes,p_expected_count,0,0,0);
END $$;

CREATE FUNCTION storage_v2_stage_lexical_document_context(
 p_occurrence_id BIGINT,p_artifact_version_id BIGINT,p_segment_orders BIGINT[],p_texts TEXT[],
 p_context_prefixes TEXT[],p_chunk_types TEXT[],p_character_starts BIGINT[],p_byte_starts BIGINT[]
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE context RECORD; v_count INTEGER:=cardinality(p_segment_orders);
        payload_bytes BIGINT; valid BOOLEAN;
BEGIN
 PERFORM public.storage_v2_require_lexical_document_context(p_occurrence_id,p_artifact_version_id);
 SELECT * INTO STRICT context FROM pg_temp.mainrag_v2_lexical_context;
 IF v_count IS NULL OR v_count NOT BETWEEN 1 AND 256
    OR cardinality(p_texts) IS DISTINCT FROM v_count
    OR cardinality(p_context_prefixes) IS DISTINCT FROM v_count
    OR cardinality(p_chunk_types) IS DISTINCT FROM v_count
    OR cardinality(p_character_starts) IS DISTINCT FROM v_count
    OR cardinality(p_byte_starts) IS DISTINCT FROM v_count
    OR array_ndims(p_segment_orders) IS DISTINCT FROM 1 OR array_lower(p_segment_orders,1)<>1
    OR array_ndims(p_texts) IS DISTINCT FROM 1 OR array_lower(p_texts,1)<>1
    OR array_ndims(p_context_prefixes) IS DISTINCT FROM 1 OR array_lower(p_context_prefixes,1)<>1
    OR array_ndims(p_chunk_types) IS DISTINCT FROM 1 OR array_lower(p_chunk_types,1)<>1
    OR array_ndims(p_character_starts) IS DISTINCT FROM 1 OR array_lower(p_character_starts,1)<>1
    OR array_ndims(p_byte_starts) IS DISTINCT FROM 1 OR array_lower(p_byte_starts,1)<>1 THEN
  RAISE EXCEPTION 'bounded lexical segment group required';
 END IF;
 SELECT sum(octet_length(segment_text)::BIGINT+octet_length(prefix)+octet_length(chunk_type)),
        bool_and(coalesce(segment_order>=0 AND character_start BETWEEN 1 AND 2147483647
          AND byte_start BETWEEN 1 AND 2147483647 AND segment_text<>'' AND prefix IS NOT NULL
          AND chunk_type IS NOT NULL AND chunk_type<>'',FALSE))
          AND count(DISTINCT segment_order)=v_count
          AND max(character_start+char_length(segment_text)-1)
               -min(character_start)+1<=8388608
          AND max(byte_start+octet_length(segment_text)-1)<=context.body_bytes
          AND max(byte_start+octet_length(segment_text)-1)-min(byte_start)+1<=33554432
   INTO payload_bytes,valid
   FROM unnest(p_segment_orders,p_texts,p_context_prefixes,p_chunk_types,p_character_starts,p_byte_starts)
         item(segment_order,segment_text,prefix,chunk_type,character_start,byte_start);
 IF valid IS NOT TRUE THEN RAISE EXCEPTION 'valid bounded lexical segment inputs required'; END IF;
 -- A document context counts unique semantic segment identities, unlike the
 -- older cross-call replay API. Compare flattened bigint sets rather than
 -- repeating pairwise overlap checks on the transported256-element arrays.
 IF EXISTS(
  SELECT 1 FROM pg_temp.mainrag_v2_lexical_batches previous_batch
   CROSS JOIN LATERAL unnest(previous_batch.segment_orders) previous(segment_order)
   JOIN unnest(p_segment_orders) incoming(segment_order)
     ON incoming.segment_order=previous.segment_order
 ) THEN
  RAISE EXCEPTION 'globally distinct lexical document segment orders required';
 END IF;
 IF v_count>context.expected_count-context.staged_count
    OR payload_bytes>134217728-context.staged_bytes THEN
  RAISE EXCEPTION 'document lexical optimization scratch bound exceeded' USING ERRCODE='54000';
 END IF;
 INSERT INTO pg_temp.mainrag_v2_lexical_batches VALUES(context.batch_count,p_segment_orders,
   p_texts,p_context_prefixes,p_chunk_types,p_character_starts,p_byte_starts);
 UPDATE pg_temp.mainrag_v2_lexical_context
  SET staged_count=staged_count+v_count,staged_bytes=staged_bytes+payload_bytes,batch_count=batch_count+1;
 RETURN v_count;
END $$;

-- Factored SQL151 validator/inserter. Its insertion/replay branches retain
-- their preceding 64-row representation selection and exact comparisons.
CREATE FUNCTION storage_v2_put_lexical_segments_document_private(
 p_occurrence_id BIGINT,p_artifact_version_id BIGINT,p_segment_orders BIGINT[],p_texts TEXT[],
 p_context_prefixes TEXT[],p_chunk_types TEXT[],p_character_starts BIGINT[],p_byte_starts BIGINT[],
 p_source_id BIGINT,p_source_bytes BYTEA,p_source_character_count INTEGER,p_source_byte_count INTEGER
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $function$
DECLARE
    v_source_id BIGINT := p_source_id;
    v_source_bytes BYTEA := p_source_bytes;
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
    v_first_terms TEXT[];
    v_first_ordinals SMALLINT[];
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
    -- Only finish can invoke this private helper. Bytes and anchors are
    -- independently obtained from its bound canonical document in this xact.
    IF NOT storage_v2_can_access_source(v_source_id, 'write') THEN
        RAISE EXCEPTION 'authorized source-backed lexical segment required'
            USING ERRCODE='42501';
    END IF;
    SELECT min(character_start), max(character_start + char_length(segment_text) - 1),
           min(byte_start), max(byte_start + octet_length(segment_text) - 1),
           bool_and(COALESCE(character_start BETWEEN 1 AND 2147483647
                    AND byte_start BETWEEN 1 AND 2147483647
                    AND segment_text <> '', FALSE))
      INTO v_window_start, v_window_end, v_byte_window_start, v_byte_window_end, v_valid
      FROM unnest(p_character_starts, p_byte_starts, p_texts)
           AS bounds(character_start, byte_start, segment_text);
    IF v_valid IS NOT TRUE OR v_window_start IS NULL
       OR v_window_end > p_source_character_count
       OR v_window_end - v_window_start + 1 > 8388608
       OR v_byte_window_end > p_source_byte_count
       OR v_byte_window_end - v_byte_window_start + 1 > 33554432 THEN
        RAISE EXCEPTION 'bounded source character and byte window required';
    END IF;
    -- All distinct document anchors were independently decoded once by
    -- finish; no caller can supply or modify this trusted-owned mapping.
    WITH prepared AS MATERIALIZED (
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
          JOIN pg_temp.mainrag_v2_lexical_anchors mapped USING (byte_start)
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
    IF p_source_byte_count>=262144
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
            SELECT cache.terms,cache.ordinals INTO v_first_terms,v_first_ordinals
              FROM public.storage_v2_lexical_first_term_cache(v_vectors[v_low:v_high]) cache;
            INSERT INTO public.storage_v2_derived_lexical_block(
                occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
                text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,
                fingerprints,text_byte_starts,text_byte_lengths,segment_masks,first_terms,first_term_ordinals
            ) VALUES(p_occurrence_id,v_source_id,p_artifact_version_id,p_segment_orders[v_low]/64,
                p_segment_orders[v_low:v_high],v_starts[v_low:v_high],v_lengths[v_low:v_high],
                v_hashes[v_low:v_high],p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],
                public.storage_v2_lexical_block_fingerprints(v_vectors[v_low:v_high]),
                p_byte_starts[v_low:v_high]::INTEGER[],v_byte_lengths[v_low:v_high],v_masks[v_low:v_high],v_first_terms,v_first_ordinals)
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
                   AND v_existing_derived.segment_masks IS DISTINCT FROM v_masks[v_low:v_high])
               OR (v_existing_derived.first_terms IS NOT NULL
                   AND (v_existing_derived.first_terms,v_existing_derived.first_term_ordinals)
                       IS DISTINCT FROM (v_first_terms,v_first_ordinals)) THEN
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
$function$;

CREATE FUNCTION storage_v2_finish_lexical_document_context(p_occurrence_id BIGINT,p_artifact_version_id BIGINT)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE context RECORD; batch RECORD; v_search_text TEXT; v_source_bytes BYTEA;
        v_character_count INTEGER; v_byte_count INTEGER; v_count BIGINT; total BIGINT:=0;
BEGIN
 PERFORM public.storage_v2_require_lexical_document_context(p_occurrence_id,p_artifact_version_id);
 SELECT * INTO STRICT context FROM pg_temp.mainrag_v2_lexical_context;
 IF context.staged_count<>context.expected_count
    OR (SELECT count(*) FROM pg_temp.mainrag_v2_lexical_batches)<>context.batch_count THEN
  RAISE EXCEPTION 'complete lexical document stage required';
 END IF;
 -- The canonical body is detoasted/converted once per document, never staged
 -- into another TOAST table and never fetched again for individual groups.
 SELECT document.search_text INTO STRICT v_search_text
  FROM public.storage_v2_search_document document WHERE document.id=context.document_id;
 v_source_bytes:=convert_to(v_search_text,'UTF8');
 v_character_count:=char_length(v_search_text);
 v_byte_count:=octet_length(v_source_bytes);
 IF v_byte_count<>context.body_bytes OR v_byte_count>134217728 THEN
  RAISE EXCEPTION 'document lexical optimization body identity differs';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_temp.mainrag_v2_lexical_anchors) THEN
  RAISE EXCEPTION 'empty lexical document anchor stage required';
 END IF;
 -- Decode each disjoint UTF8 gap once across every requested start. Starts
 -- inside a code point fail convert_from. Duplicates reuse one actual anchor.
 INSERT INTO pg_temp.mainrag_v2_lexical_anchors(byte_start,character_start)
 WITH boundaries AS MATERIALIZED (
  SELECT DISTINCT requested.byte_start
    FROM pg_temp.mainrag_v2_lexical_batches b
    CROSS JOIN LATERAL unnest(b.byte_starts) requested(byte_start)
 ), intervals AS MATERIALIZED (
  SELECT byte_start,lag(byte_start,1,1::BIGINT) OVER(ORDER BY byte_start) AS previous_byte_start
    FROM boundaries
 )
 SELECT byte_start,1+sum(char_length(convert_from(substring(v_source_bytes
    FROM previous_byte_start::INTEGER FOR (byte_start-previous_byte_start)::INTEGER),'UTF8')))
    OVER(ORDER BY byte_start) FROM intervals;
 FOR batch IN SELECT * FROM pg_temp.mainrag_v2_lexical_batches ORDER BY batch_order LOOP
  v_count:=public.storage_v2_put_lexical_segments_document_private(p_occurrence_id,p_artifact_version_id,
    batch.segment_orders,batch.texts,batch.context_prefixes,batch.chunk_types,
    batch.character_starts,batch.byte_starts,context.source_id,v_source_bytes,v_character_count,v_byte_count);
  IF v_count IS DISTINCT FROM cardinality(batch.segment_orders)::BIGINT THEN
   RAISE EXCEPTION 'lexical document group insertion differs';
  END IF;
  total:=total+v_count;
 END LOOP;
 IF total<>context.expected_count THEN RAISE EXCEPTION 'lexical document insertion count differs'; END IF;
 -- Finish releases scratch; a later document in the same transaction can
 -- reuse the verified relations. Any exception rolls back all finish inserts.
 DELETE FROM pg_temp.mainrag_v2_lexical_anchors;
 DELETE FROM pg_temp.mainrag_v2_lexical_batches;
 DELETE FROM pg_temp.mainrag_v2_lexical_context;
 RETURN total;
END $$;

DO $authority$
DECLARE signature TEXT; routine OID; grant_row RECORD;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_require_lexical_scratch()',
  'storage_v2_require_lexical_document_context(bigint,bigint)',
  'storage_v2_begin_lexical_document_context(bigint,bigint,bigint,integer)',
  'storage_v2_stage_lexical_document_context(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])',
  'storage_v2_put_lexical_segments_document_private(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer)',
  'storage_v2_finish_lexical_document_context(bigint,bigint)'] LOOP
  routine:=signature::REGPROCEDURE;
  EXECUTE format('ALTER FUNCTION %s OWNER TO mainrag_v2_frontier_owner',routine::REGPROCEDURE);
  FOR grant_row IN SELECT DISTINCT a.grantee FROM pg_proc p,
       LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid=routine AND a.grantee<>p.proowner LOOP
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s',routine::REGPROCEDURE,
        CASE WHEN grant_row.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END);
  END LOOP;
 END LOOP;
END $authority$;
GRANT EXECUTE ON FUNCTION storage_v2_begin_lexical_document_context(BIGINT,BIGINT,BIGINT,INTEGER),
 storage_v2_stage_lexical_document_context(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[],BIGINT[]),
 storage_v2_finish_lexical_document_context(BIGINT,BIGINT) TO mainrag;
COMMIT;
