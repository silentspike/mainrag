-- Losslessly replace retained flat postings with the established compact codec.
-- No source, generation, materialization or search-text identity is changed.
-- Operational resource/drain/evidence gates remain the main operator's job.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $predecessor$
DECLARE expected record; routine record; consumer record;
BEGIN
 FOR expected IN SELECT * FROM (VALUES
 ('storage_v2_posting_probe(text,bigint)','16ee003646a14fc2bbd61da0e11fbe1e57f4e332d3d5dfa3847bae2c6766baef'),
 ('storage_v2_document_posting(bigint,text)','f589f56784aa81e2776f2a2d6cbce7cf4fdced91e25603342d41cd55be4ad402'),
 ('storage_v2_cached_document_posting(bigint,text)','5b2c0ef7f2cf979480ec79e331ae2374fd9a45ab4e2556d0991f9440a7321da6'),
 ('storage_v2_scoped_term_posting(bigint[],text)','f12fdb97549deaf5dab6e2a04d904bf13c23c13e91c67b1f5595a50c2b77165c'),
 ('storage_v2_scoped_query_posting(bigint[],text[])','8b85f57a931bba1fdc00145097b64af96cea35c61e50d2a70607955481793c66'),
 ('storage_v2_complete_document_posting_blocks(bigint)','92abb7cd9e5fdc52ee136cc7f1b2b93f48ff136c4efc0408832862682896f002'),
 ('storage_v2_document_word_identifiers(bigint)','1fc03fef6ac2631850587a2df4cc12e100da37653bbdbf0b7a8d90c558edfff4'),
 ('storage_v2_reject_sealed_posting_insert()','cfa969f1366bda74fb5e262ea9577eadf52028c12003d6566626af0a0803b197'),
 ('storage_v2_reject_retrieval_mutation()','d11ce6d178bbe4989f674efbc0bcad839957c064152c673897a4f3758e84ac02'),
 ('storage_v2_seal_document_postings(bigint)','cafd683c4a6800ae57c18c15418d21805395cad3c67b5fe7c690e0f3f82d3aaa'),
 ('storage_v2_put_search_document(text,text,bigint,text,text[])','02d86dffca99c84b336f532c1c203d5ceec0bd129f41ee8f1a089604ea3fe9d9'),
 ('storage_v2_posting_fingerprint(text)','0c4a89b6e4442cfe8031c6a05d882f1bcfaa8c16af455d60ac31a7fc7693f521'),
 ('storage_v2_posting_fingerprints(text[])','45665c01d58b07b8b2c69f737c9dec8ca6e73f1570dbe42db5219ba95e31eb3b')
 ) pinned(signature,definition_sha256) LOOP
  SELECT * INTO STRICT routine FROM pg_proc WHERE oid=expected.signature::regprocedure;
  IF encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')
      IS DISTINCT FROM expected.definition_sha256 THEN
   RAISE EXCEPTION 'posting compaction predecessor differs: %',expected.signature;
  END IF;
  IF NOT (routine.proowner='mainrag'::regrole
           OR EXISTS(SELECT 1 FROM pg_roles WHERE oid=routine.proowner AND rolsuper))
     OR (expected.signature IN ('storage_v2_posting_probe(text,bigint)',
        'storage_v2_document_posting(bigint,text)','storage_v2_cached_document_posting(bigint,text)',
        'storage_v2_scoped_term_posting(bigint[],text)','storage_v2_scoped_query_posting(bigint[],text[])',
        'storage_v2_complete_document_posting_blocks(bigint)','storage_v2_document_word_identifiers(bigint)')
       AND EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
               WHERE a.grantee NOT IN (routine.proowner,'mainrag'::regrole)
                  OR a.privilege_type<>'EXECUTE')) THEN
   RAISE EXCEPTION 'posting compaction predecessor authority differs: %',expected.signature;
  END IF;
 END LOOP;
 -- No unreviewed physical consumer may observe both old and staged pairs.
 FOR consumer IN SELECT oid::regprocedure AS signature,proname,prosrc FROM pg_proc
   WHERE pronamespace='public'::regnamespace
    AND prosrc ~ '\mstorage_v2_(search_posting|compact_posting_block)\M'
    AND proname NOT IN ('storage_v2_posting_probe','storage_v2_document_posting',
      'storage_v2_cached_document_posting','storage_v2_scoped_term_posting',
      'storage_v2_scoped_query_posting','storage_v2_complete_document_posting_blocks',
      'storage_v2_document_word_identifiers','storage_v2_put_search_document',
      'storage_v2_validate_byte_posting_insert') LOOP
  RAISE EXCEPTION 'posting compaction has an unreviewed physical consumer: %',consumer.signature;
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_views WHERE schemaname='public'
   AND definition ~ '\mstorage_v2_(search_posting|compact_posting_block)\M'
   AND viewname<>'storage_v2_posting_block_all') THEN
  RAISE EXCEPTION 'posting compaction has an unreviewed physical view';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_class WHERE oid IN ('storage_v2_search_posting'::regclass,
         'storage_v2_compact_posting_block'::regclass)
       AND (relowner<>'mainrag'::regrole OR NOT relrowsecurity OR relforcerowsecurity))
    OR NOT EXISTS(SELECT 1 FROM pg_index WHERE indrelid='storage_v2_search_posting'::regclass
        AND indisprimary AND indisvalid AND indisready
        AND ARRAY(SELECT unnest(indkey))=ARRAY[1,4]::smallint[])
    OR NOT EXISTS(SELECT 1 FROM pg_index WHERE indrelid='storage_v2_compact_posting_block'::regclass
        AND indisprimary AND indisvalid AND indisready
        AND ARRAY(SELECT unnest(indkey))=ARRAY[1,2]::smallint[]) THEN
  RAISE EXCEPTION 'posting compaction physical authority or primary key differs';
 END IF;
END $predecessor$;


CREATE TABLE storage_v2_posting_conversion (
 document_id bigint PRIMARY KEY REFERENCES storage_v2_search_document(id) ON DELETE RESTRICT,
 manifest_sha256 bytea NOT NULL CHECK(octet_length(manifest_sha256)=32),
 materialization_sha256 bytea NOT NULL CHECK(octet_length(materialization_sha256)=32),
 phase text NOT NULL DEFAULT 'FLAT' CHECK(phase IN ('FLAT','COMPACT')),
 last_term_sha256 bytea CHECK(octet_length(last_term_sha256)=32),
 next_block bigint NOT NULL DEFAULT 0 CHECK(next_block>=0),
 posting_count bigint NOT NULL DEFAULT 0 CHECK(posting_count>=0),
 pairs_sha256 bytea NOT NULL DEFAULT sha256(convert_to('storage-v2-posting-pairs-v1','UTF8'))
   CHECK(octet_length(pairs_sha256)=32),
 complete boolean NOT NULL DEFAULT false,
 retired boolean NOT NULL DEFAULT false,
 CHECK(NOT retired OR (complete AND phase='COMPACT'))
);
CREATE TABLE storage_v2_posting_conversion_receipt (
 document_id bigint NOT NULL REFERENCES storage_v2_posting_conversion(document_id) ON DELETE RESTRICT,
 block_order bigint NOT NULL CHECK(block_order>=0),
 start_sha256 bytea,
 last_sha256 bytea NOT NULL CHECK(octet_length(last_sha256)=32),
 term_count integer NOT NULL CHECK(term_count BETWEEN 1 AND 256),
 term_bytes bigint NOT NULL CHECK(term_bytes>0),
 pairs_sha256 bytea NOT NULL CHECK(octet_length(pairs_sha256)=32),
 PRIMARY KEY(document_id,block_order)
);
-- Intent exists only in the copy transaction and cannot be fabricated through
-- a user-settable setting. It authorizes precisely one verified transition row.
CREATE TABLE storage_v2_posting_conversion_intent (
 document_id bigint PRIMARY KEY REFERENCES storage_v2_posting_conversion(document_id) ON DELETE RESTRICT,
 transaction_id xid8 NOT NULL,
 block_order bigint NOT NULL,
 terms text[] NOT NULL,
 term_frequencies bigint[] NOT NULL
);
CREATE TABLE storage_v2_posting_conversion_contract (
 signature text PRIMARY KEY,
 definition_sha256 bytea NOT NULL CHECK(octet_length(definition_sha256)=32),
 owner_oid oid NOT NULL,
 acl_sha256 bytea NOT NULL CHECK(octet_length(acl_sha256)=32)
);
CREATE TABLE storage_v2_posting_conversion_retirement (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 manifest_sha256 bytea NOT NULL CHECK(octet_length(manifest_sha256)=32),
 acceptance_sha256 bytea NOT NULL CHECK(octet_length(acceptance_sha256)=32),
 document_count bigint NOT NULL,
 posting_count bigint NOT NULL,
 representations_sha256 bytea NOT NULL CHECK(octet_length(representations_sha256)=32),
 relation_bytes_before bigint NOT NULL,
 relation_bytes_after bigint NOT NULL
);
REVOKE ALL ON storage_v2_posting_conversion,storage_v2_posting_conversion_receipt,
 storage_v2_posting_conversion_intent,storage_v2_posting_conversion_contract,
 storage_v2_posting_conversion_retirement FROM PUBLIC,mainrag;

CREATE TABLE storage_v2_posting_conversion_catalog_contract (
 relation_oid oid PRIMARY KEY,
 identity_sha256 bytea NOT NULL CHECK(octet_length(identity_sha256)=32)
);
REVOKE ALL ON storage_v2_posting_conversion_catalog_contract FROM PUBLIC,mainrag;
CREATE FUNCTION storage_v2_posting_conversion_relation_sha256(p_relation regclass)
RETURNS bytea LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
 SELECT sha256(convert_to(jsonb_build_object(
  'owner',r.relowner,'acl',r.relacl,'kind',r.relkind,
  'options',r.reloptions,'rls',r.relrowsecurity,'force_rls',r.relforcerowsecurity,
  'view',CASE WHEN r.relkind='v' THEN pg_get_viewdef(r.oid,true) ELSE NULL END,
  'columns',(SELECT jsonb_agg(jsonb_build_array(a.attnum,a.attname,a.atttypid,a.atttypmod,
      a.attnotnull,a.attgenerated,a.attcompression,a.attcollation,
      (SELECT pg_get_expr(d.adbin,d.adrelid) FROM pg_attrdef d WHERE d.adrelid=a.attrelid AND d.adnum=a.attnum))
      ORDER BY a.attnum) FROM pg_attribute a WHERE a.attrelid=r.oid AND a.attnum>0 AND NOT a.attisdropped),
  'constraints',(SELECT jsonb_agg(jsonb_build_array(c.conname,c.convalidated,pg_get_constraintdef(c.oid))
      ORDER BY c.conname) FROM pg_constraint c WHERE c.conrelid=r.oid),
  'policies',(SELECT jsonb_agg(jsonb_build_array(p.polname,p.polcmd,p.polroles,p.polpermissive,
      pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)) ORDER BY p.polname)
      FROM pg_policy p WHERE p.polrelid=r.oid),
  'triggers',(SELECT jsonb_agg(jsonb_build_array(g.tgname,g.tgenabled,g.tgfoid,pg_get_triggerdef(g.oid))
      ORDER BY g.tgname) FROM pg_trigger g WHERE g.tgrelid=r.oid),
  'indexes',(SELECT jsonb_agg(jsonb_build_array(i.indisvalid,i.indisready,pg_get_indexdef(i.indexrelid))
      ORDER BY i.indexrelid) FROM pg_index i WHERE i.indrelid=r.oid))::text,'UTF8'))
 FROM pg_class r WHERE r.oid=p_relation
$$;

CREATE FUNCTION storage_v2_posting_conversion_require_operator()
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE pinned record;
BEGIN
 -- This first package deliberately exposes conversion only to the local DB
 -- operator. Do not grant application administrators the superuser surface.
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user) OR storage_v2_is_admin() IS DISTINCT FROM true THEN
  RAISE EXCEPTION 'posting conversion requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('storage-v2-posting-conversion-v1',0));
 IF (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>23 THEN
  RAISE EXCEPTION 'posting conversion reader/guard contract is incomplete';
 END IF;
 FOR pinned IN SELECT * FROM storage_v2_posting_conversion_contract LOOP
  IF (SELECT proowner FROM pg_proc WHERE oid=pinned.signature::regprocedure)
        IS DISTINCT FROM pinned.owner_oid
     OR (SELECT sha256(convert_to(coalesce(proacl::text,''),'UTF8')) FROM pg_proc
         WHERE oid=pinned.signature::regprocedure) IS DISTINCT FROM pinned.acl_sha256
     OR sha256(convert_to(pg_get_functiondef(pinned.signature::regprocedure),'UTF8'))
        IS DISTINCT FROM pinned.definition_sha256 THEN
   RAISE EXCEPTION 'posting conversion reader/guard identity changed: %',pinned.signature;
  END IF;
 END LOOP;
 IF (SELECT count(*) FROM storage_v2_posting_conversion_catalog_contract)<>7 THEN
  RAISE EXCEPTION 'posting conversion catalog contract is incomplete';
 END IF;
 FOR pinned IN SELECT * FROM storage_v2_posting_conversion_catalog_contract LOOP
  IF storage_v2_posting_conversion_relation_sha256(pinned.relation_oid::regclass)
        IS DISTINCT FROM pinned.identity_sha256 THEN
   RAISE EXCEPTION 'posting conversion relation/view/trigger identity changed';
  END IF;
 END LOOP;
 IF (SELECT count(*) FROM pg_trigger WHERE tgenabled='O' AND tgtype=27
       AND tgfoid='storage_v2_reject_retrieval_mutation()'::regprocedure
       AND ((tgrelid='storage_v2_search_posting'::regclass AND tgname='storage_v2_search_posting_immutable')
         OR (tgrelid='storage_v2_compact_posting_block'::regclass AND tgname='storage_v2_compact_posting_immutable')))<>2
    OR (SELECT count(*) FROM pg_trigger WHERE tgenabled='O' AND tgtype=4
       AND tgfoid='storage_v2_reject_sealed_posting_insert()'::regprocedure
       AND ((tgrelid='storage_v2_search_posting'::regclass AND tgname='storage_v2_search_posting_sealed')
         OR (tgrelid='storage_v2_compact_posting_block'::regclass AND tgname='storage_v2_compact_posting_sealed')))<>2 THEN
  RAISE EXCEPTION 'posting conversion immutable/insert guards changed';
 END IF;
END $$;

CREATE FUNCTION storage_v2_posting_representation_visible(p_document bigint,p_representation text)
RETURNS boolean LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
 SELECT p_representation IN ('FLAT','COMPACT') AND COALESCE(
  (SELECT phase=p_representation FROM storage_v2_posting_conversion WHERE document_id=p_document),true)
$$;
REVOKE ALL ON FUNCTION storage_v2_posting_representation_visible(bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_posting_representation_visible(bigint,text) TO mainrag;

-- The invoker still needs the original table privileges and original RLS.
-- No payload is exposed while a document's compact representation is partial.
CREATE VIEW storage_v2_visible_flat_posting WITH(security_invoker=true) AS
 SELECT posting.* FROM storage_v2_search_posting posting
 WHERE storage_v2_posting_representation_visible(posting.document_id,'FLAT');
CREATE VIEW storage_v2_visible_compact_posting WITH(security_invoker=true) AS
 SELECT block.* FROM storage_v2_compact_posting_block block
 WHERE storage_v2_posting_representation_visible(block.document_id,'COMPACT');
ALTER VIEW storage_v2_visible_flat_posting OWNER TO mainrag;
ALTER VIEW storage_v2_visible_compact_posting OWNER TO mainrag;
REVOKE ALL ON storage_v2_visible_flat_posting,storage_v2_visible_compact_posting FROM PUBLIC;
GRANT SELECT ON storage_v2_visible_flat_posting,storage_v2_visible_compact_posting TO mainrag;

-- Only known read-only routines are rewritten. Constructors and insert
-- validators continue to see physical representations, including staged rows.
DO $readers$
DECLARE signature text; definition text; rewritten text;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_posting_probe(text,bigint)',
  'storage_v2_document_posting(bigint,text)',
  'storage_v2_cached_document_posting(bigint,text)',
  'storage_v2_scoped_term_posting(bigint[],text)',
  'storage_v2_scoped_query_posting(bigint[],text[])',
  'storage_v2_complete_document_posting_blocks(bigint)',
  'storage_v2_document_word_identifiers(bigint)'
 ] LOOP
  definition:=pg_get_functiondef(signature::regprocedure);
  IF definition ~* '\m(INSERT[[:space:]]+INTO|UPDATE[[:space:]]+public\.|DELETE[[:space:]]+FROM|TRUNCATE)\M' THEN
   RAISE EXCEPTION 'conversion reader unexpectedly writes: %',signature;
  END IF;
  rewritten:=regexp_replace(definition,'\m(public\.)?storage_v2_search_posting\M',
                             'public.storage_v2_visible_flat_posting','g');
  rewritten:=regexp_replace(rewritten,'\m(public\.)?storage_v2_compact_posting_block\M',
                             'public.storage_v2_visible_compact_posting','g');
  -- A delegating reader can legitimately contain no direct physical scan.
  EXECUTE rewritten;
 END LOOP;
END $readers$;
CREATE OR REPLACE VIEW storage_v2_posting_block_all WITH(security_invoker=true) AS
 SELECT document_id,block_order,terms,term_frequencies,fingerprints
  FROM storage_v2_visible_compact_posting
 UNION ALL
 SELECT block.document_id,block.block_order,
  storage_v2_decode_term_locations(document.search_text,block.text_byte_starts,block.text_byte_lengths),
  block.term_frequencies,block.fingerprints
 FROM storage_v2_byte_posting_block block JOIN storage_v2_search_document document ON document.id=block.document_id;

CREATE OR REPLACE FUNCTION storage_v2_reject_sealed_posting_insert()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE document bigint;
BEGIN
 FOR document IN SELECT DISTINCT added.document_id FROM storage_v2_new_postings added LOOP
  PERFORM 1 FROM storage_v2_search_document WHERE id=document FOR KEY SHARE;
  IF EXISTS(SELECT 1 FROM storage_v2_document_postings_seal WHERE document_id=document) THEN
   IF TG_TABLE_NAME='storage_v2_compact_posting_block' AND (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
      AND storage_v2_is_admin() IS true THEN
    IF NOT EXISTS(
     SELECT 1 FROM storage_v2_new_postings added
     WHERE added.document_id=document AND NOT EXISTS(
      SELECT 1 FROM storage_v2_posting_conversion_intent intent
       JOIN storage_v2_posting_conversion conversion USING(document_id)
      WHERE intent.document_id=added.document_id AND intent.transaction_id=pg_current_xact_id()
       AND conversion.phase='FLAT' AND NOT conversion.complete AND NOT conversion.retired
       AND intent.block_order=conversion.next_block AND intent.block_order=added.block_order
       AND intent.terms IS NOT DISTINCT FROM added.terms
       AND intent.term_frequencies IS NOT DISTINCT FROM added.term_frequencies)) THEN
     CONTINUE;
    END IF;
   END IF;
   RAISE EXCEPTION 'sealed search-document postings are immutable';
  END IF;
 END LOOP;
 RETURN NULL;
END $$;

CREATE FUNCTION storage_v2_posting_conversion_contract_sha256()
RETURNS bytea LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE result bytea;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 SELECT sha256(convert_to(jsonb_build_object(
   'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_posting_conversion_contract c),
   'catalog',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c))::text,'UTF8'))
 INTO result;
 RETURN result;
END $$;

CREATE FUNCTION storage_v2_prepare_posting_conversion(
 p_document bigint,p_identity bytea,p_manifest bytea
) RETURNS storage_v2_posting_conversion LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE identity bytea; result storage_v2_posting_conversion;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 IF octet_length(p_identity) IS DISTINCT FROM 32 OR octet_length(p_manifest) IS DISTINCT FROM 32 THEN
  RAISE EXCEPTION 'exact materialization and manifest identities required';
 END IF;
 SELECT materialization_sha256 INTO STRICT identity FROM storage_v2_search_document
  WHERE id=p_document FOR UPDATE;
 IF identity IS DISTINCT FROM p_identity THEN RAISE EXCEPTION 'posting conversion materialization differs'; END IF;
 SELECT * INTO result FROM storage_v2_posting_conversion WHERE document_id=p_document FOR UPDATE;
 IF FOUND THEN
  IF result.manifest_sha256 IS DISTINCT FROM p_manifest OR result.materialization_sha256 IS DISTINCT FROM p_identity THEN
   RAISE EXCEPTION 'posting conversion belongs to a different manifest';
  END IF;
  RETURN result;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_search_posting WHERE document_id=p_document)
    OR EXISTS(SELECT 1 FROM storage_v2_compact_posting_block WHERE document_id=p_document)
    OR EXISTS(SELECT 1 FROM storage_v2_byte_posting_block WHERE document_id=p_document) THEN
  RAISE EXCEPTION 'initial conversion requires a flat-only, nonempty document';
 END IF;
 PERFORM storage_v2_seal_document_postings(p_document);
 INSERT INTO storage_v2_posting_conversion(document_id,manifest_sha256,materialization_sha256)
  VALUES(p_document,p_manifest,p_identity) RETURNING * INTO result;
 RETURN result;
END $$;

CREATE FUNCTION storage_v2_copy_posting_conversion_batch(
 p_document bigint,p_manifest bytea,p_expected_block bigint,p_expected_cursor bytea,p_max_bytes bigint
) RETURNS storage_v2_posting_conversion_receipt LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE conversion storage_v2_posting_conversion; receipt storage_v2_posting_conversion_receipt;
 row_meta record; original record; item record; source_terms text[]:=ARRAY[]::text[];
 source_frequencies bigint[]:=ARRAY[]::bigint[]; size bigint:=0; last_sha bytea;
 source_digest bytea; target_digest bytea; target_count integer:=0;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 IF p_expected_block IS NULL OR p_expected_block<0 OR p_max_bytes IS NULL
    OR p_max_bytes NOT BETWEEN 1 AND 67108864 THEN RAISE EXCEPTION 'bounded conversion request required'; END IF;
 SELECT * INTO STRICT conversion FROM storage_v2_posting_conversion WHERE document_id=p_document FOR UPDATE;
 IF conversion.manifest_sha256 IS DISTINCT FROM p_manifest THEN RAISE EXCEPTION 'posting manifest differs'; END IF;
 SELECT * INTO receipt FROM storage_v2_posting_conversion_receipt
  WHERE document_id=p_document AND block_order=p_expected_block;
 IF FOUND THEN
  IF receipt.start_sha256 IS DISTINCT FROM p_expected_cursor THEN RAISE EXCEPTION 'retained batch cursor differs'; END IF;
  RETURN receipt; -- Same operation after an unknown acknowledgement.
 END IF;
 IF conversion.retired OR conversion.complete OR conversion.phase<>'FLAT'
    OR conversion.next_block IS DISTINCT FROM p_expected_block
    OR conversion.last_term_sha256 IS DISTINCT FROM p_expected_cursor THEN
  RAISE EXCEPTION 'conversion cursor or publication state differs';
 END IF;
 PERFORM 1 FROM storage_v2_search_document WHERE id=p_document
  AND materialization_sha256=conversion.materialization_sha256 FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'posting materialization changed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal
  WHERE document_id=p_document AND materialization_sha256=conversion.materialization_sha256) THEN
  RAISE EXCEPTION 'original postings are not sealed';
 END IF;
 source_digest:=conversion.pairs_sha256; last_sha:=p_expected_cursor;
 -- Inspect term lengths before fetching term payloads. No document-wide sort
 -- or array_agg over the corpus; the existing document/SHA primary key bounds it.
 FOR row_meta IN SELECT term_sha256,octet_length(term) term_bytes FROM storage_v2_search_posting
  WHERE document_id=p_document AND term_sha256>coalesce(p_expected_cursor,''::bytea)
  ORDER BY term_sha256 LIMIT 256 LOOP
  IF row_meta.term_bytes>p_max_bytes-size THEN
   IF cardinality(source_terms)=0 THEN RAISE EXCEPTION 'single term exceeds admitted batch bytes'; END IF;
   EXIT;
  END IF;
  SELECT term,term_frequency INTO STRICT original FROM storage_v2_search_posting
   WHERE document_id=p_document AND term_sha256=row_meta.term_sha256;
  IF public.digest(original.term,'sha256') IS DISTINCT FROM row_meta.term_sha256
     OR original.term_frequency<=0 THEN RAISE EXCEPTION 'original posting pair is invalid'; END IF;
  source_terms:=array_append(source_terms,original.term);
  source_frequencies:=array_append(source_frequencies,original.term_frequency);
  source_digest:=sha256(source_digest||int4send(octet_length(original.term))
                        ||convert_to(original.term,'UTF8')||int8send(original.term_frequency));
  size:=size+row_meta.term_bytes; last_sha:=row_meta.term_sha256;
 END LOOP;
 IF cardinality(source_terms)=0 THEN RAISE EXCEPTION 'empty batch cannot be committed'; END IF;
 INSERT INTO storage_v2_posting_conversion_intent VALUES(
  p_document,pg_current_xact_id(),p_expected_block,source_terms,source_frequencies);
 INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies)
  VALUES(p_document,p_expected_block,source_terms,source_frequencies);
 -- Flatten the stored target independently and prove each pair against its
 -- immutable source row. A source SHA or materialization SHA alone is not proof.
 target_digest:=conversion.pairs_sha256;
 FOR item IN SELECT t.term,t.frequency,t.n FROM storage_v2_compact_posting_block b
  CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) WITH ORDINALITY t(term,frequency,n)
  WHERE b.document_id=p_document AND b.block_order=p_expected_block ORDER BY t.n LOOP
  target_count:=target_count+1;
  IF item.term IS DISTINCT FROM source_terms[target_count]
     OR item.frequency IS DISTINCT FROM source_frequencies[target_count] THEN
   RAISE EXCEPTION 'stored compact pair differs from exact flat pair';
  END IF;
  target_digest:=sha256(target_digest||int4send(octet_length(item.term))
                       ||convert_to(item.term,'UTF8')||int8send(item.frequency));
 END LOOP;
 IF target_count<>cardinality(source_terms) OR target_digest IS DISTINCT FROM source_digest THEN
  RAISE EXCEPTION 'stored compact posting count or digest differs';
 END IF;
 DELETE FROM storage_v2_posting_conversion_intent WHERE document_id=p_document;
 INSERT INTO storage_v2_posting_conversion_receipt VALUES(
  p_document,p_expected_block,p_expected_cursor,last_sha,target_count,size,target_digest) RETURNING * INTO receipt;
 UPDATE storage_v2_posting_conversion SET last_term_sha256=last_sha,next_block=next_block+1,
  posting_count=posting_count+target_count,pairs_sha256=target_digest,
  complete=NOT EXISTS(SELECT 1 FROM storage_v2_search_posting
                      WHERE document_id=p_document AND term_sha256>last_sha)
 WHERE document_id=p_document;
 RETURN receipt;
END $$;

-- Amortize operator/process transactions over bounded groups. Each physical
-- block retains its own exact committed receipt; the whole group is atomic.
CREATE FUNCTION storage_v2_copy_posting_conversion_group(
 p_document bigint,p_manifest bytea,p_expected_block bigint,p_expected_cursor bytea,
 p_max_bytes bigint,p_max_blocks integer DEFAULT 32
) RETURNS TABLE(document_id bigint,block_order bigint,term_count bigint,term_bytes bigint,pairs_sha256 text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE current_state storage_v2_posting_conversion; receipt storage_v2_posting_conversion_receipt;
 remaining bigint; next_bytes bigint; n integer;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 IF p_max_blocks IS NULL OR p_max_blocks NOT BETWEEN 1 AND 256
    OR p_max_bytes IS NULL OR p_max_bytes NOT BETWEEN 1 AND 67108864 THEN
  RAISE EXCEPTION 'bounded posting conversion group required';
 END IF;
 SELECT * INTO STRICT current_state FROM storage_v2_posting_conversion
  WHERE storage_v2_posting_conversion.document_id=p_document FOR UPDATE;
 IF current_state.manifest_sha256 IS DISTINCT FROM p_manifest OR current_state.retired
    OR current_state.complete OR current_state.phase<>'FLAT'
    OR current_state.next_block IS DISTINCT FROM p_expected_block
    OR current_state.last_term_sha256 IS DISTINCT FROM p_expected_cursor THEN
  RAISE EXCEPTION 'group cursor changed; reconcile the existing committed receipts';
 END IF;
 document_id:=p_document; term_count:=0; term_bytes:=0; remaining:=p_max_bytes;
 FOR n IN 1..p_max_blocks LOOP
  SELECT octet_length(term) INTO next_bytes FROM storage_v2_search_posting
   WHERE storage_v2_search_posting.document_id=p_document
    AND term_sha256>coalesce(current_state.last_term_sha256,''::bytea)
   ORDER BY term_sha256 LIMIT 1;
  IF term_count>0 AND (next_bytes IS NULL OR next_bytes>remaining) THEN EXIT; END IF;
  receipt:=storage_v2_copy_posting_conversion_batch(p_document,p_manifest,current_state.next_block,
                                                   current_state.last_term_sha256,remaining);
  term_count:=term_count+receipt.term_count; term_bytes:=term_bytes+receipt.term_bytes;
  remaining:=remaining-receipt.term_bytes;
  SELECT * INTO STRICT current_state FROM storage_v2_posting_conversion
    WHERE storage_v2_posting_conversion.document_id=p_document;
  IF current_state.complete OR remaining=0 THEN EXIT; END IF;
 END LOOP;
 block_order:=current_state.next_block-1; pairs_sha256:=encode(current_state.pairs_sha256,'hex');
 RETURN NEXT;
END $$;

CREATE FUNCTION storage_v2_publish_posting_conversion(
 p_document bigint,p_manifest bytea,p_count bigint,p_digest bytea
) RETURNS storage_v2_posting_conversion LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE conversion storage_v2_posting_conversion;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 SELECT * INTO STRICT conversion FROM storage_v2_posting_conversion WHERE document_id=p_document FOR UPDATE;
 PERFORM 1 FROM storage_v2_search_document WHERE id=p_document
  AND materialization_sha256=conversion.materialization_sha256 FOR UPDATE;
 IF NOT FOUND OR conversion.manifest_sha256 IS DISTINCT FROM p_manifest
    OR NOT conversion.complete OR conversion.posting_count IS DISTINCT FROM p_count
    OR conversion.pairs_sha256 IS DISTINCT FROM p_digest
    OR EXISTS(SELECT 1 FROM storage_v2_search_posting WHERE document_id=p_document
               AND term_sha256>conversion.last_term_sha256)
    OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_intent WHERE document_id=p_document)
    OR (SELECT count(*) FROM storage_v2_posting_conversion_receipt WHERE document_id=p_document)<>conversion.next_block
    OR (SELECT count(*) FROM storage_v2_compact_posting_block WHERE document_id=p_document)<>conversion.next_block
    OR EXISTS(SELECT 1 FROM storage_v2_byte_posting_block WHERE document_id=p_document) THEN
  RAISE EXCEPTION 'posting conversion is incomplete or its publication proof differs';
 END IF;
 UPDATE storage_v2_posting_conversion SET phase='COMPACT' WHERE document_id=p_document RETURNING * INTO conversion;
 RETURN conversion;
END $$;

CREATE FUNCTION storage_v2_restore_flat_posting_visibility(p_document bigint,p_manifest bytea)
RETURNS storage_v2_posting_conversion LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE conversion storage_v2_posting_conversion;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 SELECT * INTO STRICT conversion FROM storage_v2_posting_conversion WHERE document_id=p_document FOR UPDATE;
 IF conversion.manifest_sha256 IS DISTINCT FROM p_manifest OR conversion.retired
    OR NOT EXISTS(SELECT 1 FROM storage_v2_search_posting WHERE document_id=p_document) THEN
  RAISE EXCEPTION 'flat visibility can no longer be restored under this manifest';
 END IF;
 UPDATE storage_v2_posting_conversion SET phase='FLAT' WHERE document_id=p_document RETURNING * INTO conversion;
 RETURN conversion;
END $$;

-- The owner runner must verify drain, current resource admission, exact package,
-- retained-generation integrity and the bound acceptance receipt BEFORE calling
-- this final surface. There is no automatic retirement in the batch adapter.
-- This single final completeness scan is an integrity gate, not a benchmark.
CREATE FUNCTION storage_v2_retire_converted_flat_postings(
 p_manifest bytea,p_acceptance_sha256 bytea,p_expected_documents bigint,
 p_expected_postings bigint,p_expected_representations_sha256 bytea
) RETURNS storage_v2_posting_conversion_retirement LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE result storage_v2_posting_conversion_retirement;
 documents bigint; postings bigint; representations bytea; old_bytes bigint;
BEGIN
 PERFORM storage_v2_posting_conversion_require_operator();
 IF octet_length(p_manifest) IS DISTINCT FROM 32 OR octet_length(p_acceptance_sha256) IS DISTINCT FROM 32
    OR octet_length(p_expected_representations_sha256) IS DISTINCT FROM 32
    OR p_expected_documents IS NULL OR p_expected_documents<=0
    OR p_expected_postings IS NULL OR p_expected_postings<=0 THEN
  RAISE EXCEPTION 'exact complete-set retirement identities required';
 END IF;
 SELECT * INTO result FROM storage_v2_posting_conversion_retirement WHERE singleton;
 IF FOUND THEN
  IF result.manifest_sha256 IS DISTINCT FROM p_manifest
     OR result.acceptance_sha256 IS DISTINCT FROM p_acceptance_sha256
     OR result.document_count IS DISTINCT FROM p_expected_documents
     OR result.posting_count IS DISTINCT FROM p_expected_postings
     OR result.representations_sha256 IS DISTINCT FROM p_expected_representations_sha256
     OR EXISTS(SELECT 1 FROM storage_v2_search_posting) THEN
   RAISE EXCEPTION 'retained retirement receipt belongs to a different operation';
  END IF;
  RETURN result;
 END IF;
 LOCK TABLE storage_v2_search_posting IN ACCESS EXCLUSIVE MODE;
 LOCK TABLE storage_v2_posting_conversion IN SHARE ROW EXCLUSIVE MODE;
 -- TRUNCATE is not MVCC-safe. Draining just currently active SELECTs misses
 -- idle repeatable-read transactions retaining a pre-publication snapshot.
 IF EXISTS(SELECT 1 FROM pg_stat_activity
           WHERE datname=current_database() AND pid<>pg_backend_pid()
             AND backend_type='client backend' AND backend_xmin IS NOT NULL) THEN
  RAISE EXCEPTION 'flat retirement still has another retained reader snapshot';
 END IF;
 IF EXISTS(SELECT 1 FROM storage_v2_posting_conversion
            WHERE manifest_sha256<>p_manifest OR phase<>'COMPACT' OR NOT complete OR retired)
    OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_intent)
    OR EXISTS(SELECT 1 FROM pg_constraint
               WHERE contype='f' AND confrelid='storage_v2_search_posting'::regclass)
    OR EXISTS(SELECT 1 FROM storage_v2_search_posting posting WHERE NOT EXISTS(
               SELECT 1 FROM storage_v2_posting_conversion converted
                WHERE converted.document_id=posting.document_id AND converted.phase='COMPACT'
                  AND converted.complete AND converted.manifest_sha256=p_manifest)) THEN
  RAISE EXCEPTION 'flat retirement has unconverted owners, pending writes or an incoming reference';
 END IF;
 -- Fixed-width identities only; never re-export bodies or term payloads.
 SELECT count(*),sum(posting_count),sha256(convert_to(string_agg(
          document_id::text||':'||posting_count::text||':'||encode(pairs_sha256,'hex'),
          E'\n' ORDER BY document_id),'UTF8')) INTO documents,postings,representations
 FROM storage_v2_posting_conversion;
 IF documents IS DISTINCT FROM p_expected_documents OR postings IS DISTINCT FROM p_expected_postings
    OR representations IS DISTINCT FROM p_expected_representations_sha256
    OR (SELECT count(*) FROM storage_v2_search_posting) IS DISTINCT FROM postings THEN
  RAISE EXCEPTION 'retirement complete-set count or receipt identity differs';
 END IF;
 old_bytes:=pg_total_relation_size('storage_v2_search_posting'::regclass);
 TRUNCATE TABLE storage_v2_search_posting; -- No CASCADE or identity reset.
 UPDATE storage_v2_posting_conversion SET retired=true;
 INSERT INTO storage_v2_posting_conversion_retirement VALUES(
  true,p_manifest,p_acceptance_sha256,documents,postings,representations,old_bytes,
  pg_total_relation_size('storage_v2_search_posting'::regclass)) RETURNING * INTO result;
 RETURN result; -- Relation allocation is not filesystem/thin-pool reclaim.
END $$;

-- Pin the accepted installed reader and guard contract for every later batch.
INSERT INTO storage_v2_posting_conversion_catalog_contract
 SELECT oid,storage_v2_posting_conversion_relation_sha256(oid::regclass)
 FROM pg_class WHERE oid IN (
  'storage_v2_search_posting'::regclass,'storage_v2_compact_posting_block'::regclass,
  'storage_v2_search_document'::regclass,'storage_v2_document_postings_seal'::regclass,
  'storage_v2_visible_flat_posting'::regclass,'storage_v2_visible_compact_posting'::regclass,
  'storage_v2_posting_block_all'::regclass);

DO $private_authority$
DECLARE signature text;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
 'storage_v2_posting_conversion_require_operator()',
 'storage_v2_prepare_posting_conversion(bigint,bytea,bytea)',
 'storage_v2_copy_posting_conversion_batch(bigint,bytea,bigint,bytea,bigint)',
 'storage_v2_copy_posting_conversion_group(bigint,bytea,bigint,bytea,bigint,integer)',
 'storage_v2_publish_posting_conversion(bigint,bytea,bigint,bytea)',
 'storage_v2_restore_flat_posting_visibility(bigint,bytea)',
 'storage_v2_retire_converted_flat_postings(bytea,bytea,bigint,bigint,bytea)',
 'storage_v2_posting_conversion_relation_sha256(regclass)',
 'storage_v2_posting_conversion_contract_sha256()'
 ] LOOP
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,mainrag',signature);
 END LOOP;
END $private_authority$;

INSERT INTO storage_v2_posting_conversion_contract
SELECT signature,sha256(convert_to(pg_get_functiondef(signature::regprocedure),'UTF8')),proowner,sha256(convert_to(coalesce(proacl::text,''),'UTF8'))
 FROM unnest(ARRAY[
 'storage_v2_posting_probe(text,bigint)',
 'storage_v2_document_posting(bigint,text)',
 'storage_v2_cached_document_posting(bigint,text)',
 'storage_v2_scoped_term_posting(bigint[],text)',
 'storage_v2_scoped_query_posting(bigint[],text[])',
 'storage_v2_complete_document_posting_blocks(bigint)',
 'storage_v2_document_word_identifiers(bigint)',
 'storage_v2_reject_sealed_posting_insert()',
 'storage_v2_reject_retrieval_mutation()',
 'storage_v2_posting_representation_visible(bigint,text)',
 'storage_v2_seal_document_postings(bigint)',
 'storage_v2_put_search_document(text,text,bigint,text,text[])',
 'storage_v2_posting_fingerprint(text)',
 'storage_v2_posting_fingerprints(text[])',
 'storage_v2_posting_conversion_require_operator()',
 'storage_v2_prepare_posting_conversion(bigint,bytea,bytea)',
 'storage_v2_copy_posting_conversion_batch(bigint,bytea,bigint,bytea,bigint)',
 'storage_v2_copy_posting_conversion_group(bigint,bytea,bigint,bytea,bigint,integer)',
 'storage_v2_publish_posting_conversion(bigint,bytea,bigint,bytea)',
 'storage_v2_restore_flat_posting_visibility(bigint,bytea)',
 'storage_v2_retire_converted_flat_postings(bytea,bytea,bigint,bigint,bytea)',
 'storage_v2_posting_conversion_relation_sha256(regclass)',
 'storage_v2_posting_conversion_contract_sha256()'
 ]) signatures(signature) JOIN pg_proc ON pg_proc.oid=signature::regprocedure;

COMMIT;
