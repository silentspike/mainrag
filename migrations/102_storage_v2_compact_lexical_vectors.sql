-- Pack new canonical lexical segments without changing their logical rows.
-- Existing projections remain untouched. Fingerprints select possible blocks;
-- full original weighted vectors decide matches and ranks.
BEGIN;

CREATE OR REPLACE FUNCTION storage_v2_lexical_block_fingerprints(p_vectors TSVECTOR[])
RETURNS INTEGER[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp
AS $$
    SELECT ARRAY(SELECT DISTINCT public.storage_v2_posting_fingerprint(term)
      FROM unnest(p_vectors) vector
      CROSS JOIN LATERAL unnest(tsvector_to_array(vector)) term ORDER BY 1)
$$;

CREATE OR REPLACE FUNCTION storage_v2_lexical_block_orders_valid(
    p_block_order BIGINT,p_orders BIGINT[]
) RETURNS BOOLEAN LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp
AS $$
    SELECT p_block_order BETWEEN 0 AND 144115188075855871
       AND array_ndims(p_orders)=1 AND array_lower(p_orders,1)=1
       AND cardinality(p_orders) BETWEEN 1 AND 64
       AND array_position(p_orders,NULL) IS NULL
       AND p_orders=ARRAY(SELECT generate_series(p_block_order*64,
                                  p_block_order*64+cardinality(p_orders)-1))
$$;

CREATE TABLE IF NOT EXISTS storage_v2_compact_lexical_block (
    occurrence_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    block_order BIGINT NOT NULL,
    segment_orders BIGINT[] NOT NULL,
    text_starts INTEGER[] NOT NULL,
    text_lengths INTEGER[] NOT NULL,
    text_hashes BYTEA[] NOT NULL,
    context_prefixes TEXT[] NOT NULL,
    chunk_types TEXT[] NOT NULL,
    fts_vectors TSVECTOR[] NOT NULL,
    fingerprints INTEGER[] GENERATED ALWAYS AS
        (storage_v2_lexical_block_fingerprints(fts_vectors)) STORED NOT NULL,
    PRIMARY KEY(occurrence_id,block_order),
    FOREIGN KEY(occurrence_id,source_id,artifact_version_id)
        REFERENCES occurrence(id,source_id,artifact_version_id) ON DELETE RESTRICT,
    CHECK(storage_v2_lexical_block_orders_valid(block_order,segment_orders)),
    CHECK(array_ndims(text_starts)=1 AND array_lower(text_starts,1)=1
       AND cardinality(text_starts)=cardinality(segment_orders)
       AND array_position(text_starts,NULL) IS NULL AND 0<ALL(text_starts)),
    CHECK(array_ndims(text_lengths)=1 AND array_lower(text_lengths,1)=1
       AND cardinality(text_lengths)=cardinality(segment_orders)
       AND array_position(text_lengths,NULL) IS NULL AND 0<ALL(text_lengths)),
    CHECK(array_ndims(text_hashes)=1 AND array_lower(text_hashes,1)=1
       AND cardinality(text_hashes)=cardinality(segment_orders)
       AND array_position(text_hashes,NULL) IS NULL),
    CHECK(array_ndims(context_prefixes)=1 AND array_lower(context_prefixes,1)=1
       AND cardinality(context_prefixes)=cardinality(segment_orders)
       AND array_position(context_prefixes,NULL) IS NULL),
    CHECK(array_ndims(chunk_types)=1 AND array_lower(chunk_types,1)=1
       AND cardinality(chunk_types)=cardinality(segment_orders)
       AND array_position(chunk_types,NULL) IS NULL AND NOT ''=ANY(chunk_types)),
    CHECK(array_ndims(fts_vectors)=1 AND array_lower(fts_vectors,1)=1
       AND cardinality(fts_vectors)=cardinality(segment_orders)
       AND array_position(fts_vectors,NULL) IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_storage_v2_compact_lexical_fingerprint
    ON storage_v2_compact_lexical_block USING GIN(fingerprints);
CREATE INDEX IF NOT EXISTS idx_storage_v2_compact_lexical_source
    ON storage_v2_compact_lexical_block(source_id,occurrence_id);
ALTER TABLE storage_v2_compact_lexical_block OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_compact_lexical_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_compact_lexical_block FORCE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_compact_lexical_block ALTER COLUMN fts_vectors SET COMPRESSION lz4;
ALTER TABLE storage_v2_compact_lexical_block ALTER COLUMN text_hashes SET COMPRESSION lz4;

CREATE TEMP TABLE storage_v2_expected_compact_lexical_checks (
    block_order BIGINT,segment_orders BIGINT[],text_starts INTEGER[],text_lengths INTEGER[],
    text_hashes BYTEA[],context_prefixes TEXT[],chunk_types TEXT[],fts_vectors TSVECTOR[],
    CHECK(storage_v2_lexical_block_orders_valid(block_order,segment_orders)),
    CHECK(array_ndims(text_starts)=1 AND array_lower(text_starts,1)=1
       AND cardinality(text_starts)=cardinality(segment_orders)
       AND array_position(text_starts,NULL) IS NULL AND 0<ALL(text_starts)),
    CHECK(array_ndims(text_lengths)=1 AND array_lower(text_lengths,1)=1
       AND cardinality(text_lengths)=cardinality(segment_orders)
       AND array_position(text_lengths,NULL) IS NULL AND 0<ALL(text_lengths)),
    CHECK(array_ndims(text_hashes)=1 AND array_lower(text_hashes,1)=1
       AND cardinality(text_hashes)=cardinality(segment_orders)
       AND array_position(text_hashes,NULL) IS NULL),
    CHECK(array_ndims(context_prefixes)=1 AND array_lower(context_prefixes,1)=1
       AND cardinality(context_prefixes)=cardinality(segment_orders)
       AND array_position(context_prefixes,NULL) IS NULL),
    CHECK(array_ndims(chunk_types)=1 AND array_lower(chunk_types,1)=1
       AND cardinality(chunk_types)=cardinality(segment_orders)
       AND array_position(chunk_types,NULL) IS NULL AND NOT ''=ANY(chunk_types)),
    CHECK(array_ndims(fts_vectors)=1 AND array_lower(fts_vectors,1)=1
       AND cardinality(fts_vectors)=cardinality(segment_orders)
       AND array_position(fts_vectors,NULL) IS NULL)
);

-- A replay must not accept a same-named table or index with weaker identity.
DO $layout$
DECLARE
    columns JSONB;
    actual pg_index;
    signature TEXT;
BEGIN
    SELECT jsonb_agg(jsonb_build_array(attname,format_type(atttypid,atttypmod),
                                     attnotnull,attgenerated) ORDER BY attnum)
      INTO columns FROM pg_attribute
     WHERE attrelid='storage_v2_compact_lexical_block'::REGCLASS
       AND attnum>0 AND NOT attisdropped;
    IF columns IS DISTINCT FROM '[
        ["occurrence_id","bigint",true,""],["source_id","bigint",true,""],
        ["artifact_version_id","bigint",true,""],["block_order","bigint",true,""],
        ["segment_orders","bigint[]",true,""],["text_starts","integer[]",true,""],
        ["text_lengths","integer[]",true,""],["text_hashes","bytea[]",true,""],
        ["context_prefixes","text[]",true,""],["chunk_types","text[]",true,""],
        ["fts_vectors","tsvector[]",true,""],["fingerprints","integer[]",true,"s"]
    ]'::JSONB OR (SELECT relkind<>'r' OR relpersistence<>'p' FROM pg_class
                  WHERE oid='storage_v2_compact_lexical_block'::REGCLASS) THEN
        RAISE EXCEPTION 'compact lexical table layout differs';
    END IF;
    IF (SELECT pg_get_expr(adbin,adrelid) FROM pg_attrdef
         WHERE adrelid='storage_v2_compact_lexical_block'::REGCLASS
           AND adnum=12) IS DISTINCT FROM
       'storage_v2_lexical_block_fingerprints(fts_vectors)' THEN
        RAISE EXCEPTION 'compact lexical fingerprint expression differs';
    END IF;
    IF (SELECT jsonb_agg(pg_get_constraintdef(oid) ORDER BY pg_get_constraintdef(oid))
          FROM pg_constraint
         WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS AND contype='c')
       IS DISTINCT FROM
       (SELECT jsonb_agg(pg_get_constraintdef(oid) ORDER BY pg_get_constraintdef(oid))
          FROM pg_constraint
         WHERE conrelid='pg_temp.storage_v2_expected_compact_lexical_checks'::REGCLASS
           AND contype='c')
       OR EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS
                     AND contype='c' AND (NOT convalidated OR connoinherit)) THEN
        RAISE EXCEPTION 'compact lexical payload constraints differ';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS
          AND contype='p' AND conkey=ARRAY[1,4]::SMALLINT[] AND convalidated)
       OR NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS
          AND contype='f' AND confrelid='occurrence'::REGCLASS
          AND conkey=ARRAY[1,2,3]::SMALLINT[] AND convalidated
          AND confdeltype='r' AND confupdtype='a' AND NOT condeferrable
          AND pg_get_constraintdef(oid)=
              'FOREIGN KEY (occurrence_id, source_id, artifact_version_id) REFERENCES occurrence(id, source_id, artifact_version_id) ON DELETE RESTRICT') THEN
        RAISE EXCEPTION 'compact lexical identity constraints differ';
    END IF;
    FOREACH signature IN ARRAY ARRAY[
        'idx_storage_v2_compact_lexical_fingerprint',
        'idx_storage_v2_compact_lexical_source'
    ] LOOP
        SELECT * INTO STRICT actual FROM pg_index WHERE indexrelid=signature::REGCLASS;
        IF actual.indrelid<>'storage_v2_compact_lexical_block'::REGCLASS
           OR NOT actual.indisvalid OR NOT actual.indisready OR actual.indisunique
           OR actual.indpred IS NOT NULL OR actual.indexprs IS NOT NULL
           OR actual.indnatts<>actual.indnkeyatts
           OR (signature LIKE '%_fingerprint' AND
               (actual.indnkeyatts<>1 OR pg_get_indexdef(actual.indexrelid,1,TRUE)<>'fingerprints'
                OR (SELECT amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam
                     WHERE c.oid=actual.indexrelid)<>'gin'))
           OR (signature LIKE '%_source' AND
               (actual.indnkeyatts<>2 OR pg_get_indexdef(actual.indexrelid,1,TRUE)<>'source_id'
                OR pg_get_indexdef(actual.indexrelid,2,TRUE)<>'occurrence_id'
                OR (SELECT amname FROM pg_class c JOIN pg_am a ON a.oid=c.relam
                     WHERE c.oid=actual.indexrelid)<>'btree')) THEN
            RAISE EXCEPTION 'compact lexical index identity differs';
        END IF;
    END LOOP;
END
$layout$;
DROP TABLE storage_v2_expected_compact_lexical_checks;

DO $authority$
DECLARE
    policy pg_policy;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid='storage_v2_compact_lexical_block'::REGCLASS
                      AND polname='storage_v2_compact_lexical_source') THEN
        CREATE POLICY storage_v2_compact_lexical_source ON storage_v2_compact_lexical_block
            USING(storage_v2_can_access_source(source_id,'read'))
            WITH CHECK(storage_v2_can_access_source(source_id,'write'));
    END IF;
    SELECT * INTO STRICT policy FROM pg_policy
     WHERE polrelid='storage_v2_compact_lexical_block'::REGCLASS
       AND polname='storage_v2_compact_lexical_source';
    IF policy.polcmd<>'*' OR NOT policy.polpermissive OR policy.polroles<>ARRAY[0::OID]
       OR pg_get_expr(policy.polqual,policy.polrelid)<>
          'storage_v2_can_access_source(source_id, ''read''::text)'
       OR pg_get_expr(policy.polwithcheck,policy.polrelid)<>
          'storage_v2_can_access_source(source_id, ''write''::text)' THEN
        RAISE EXCEPTION 'compact lexical source policy differs';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid='storage_v2_compact_lexical_block'::REGCLASS
                      AND polname='storage_v2_compact_lexical_rank_reader') THEN
        CREATE POLICY storage_v2_compact_lexical_rank_reader ON storage_v2_compact_lexical_block
            FOR SELECT TO mainrag_v2_lexical_rank_owner USING(TRUE);
    END IF;
    SELECT * INTO STRICT policy FROM pg_policy
     WHERE polrelid='storage_v2_compact_lexical_block'::REGCLASS
       AND polname='storage_v2_compact_lexical_rank_reader';
    IF policy.polcmd<>'r' OR NOT policy.polpermissive
       OR policy.polroles<>ARRAY['mainrag_v2_lexical_rank_owner'::REGROLE::OID]
       OR pg_get_expr(policy.polqual,policy.polrelid)<>'true'
       OR policy.polwithcheck IS NOT NULL THEN
        RAISE EXCEPTION 'compact lexical rank policy differs';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid='storage_v2_compact_lexical_block'::REGCLASS
                      AND tgname='storage_v2_compact_lexical_immutable') THEN
        CREATE TRIGGER storage_v2_compact_lexical_immutable
            BEFORE UPDATE OR DELETE ON storage_v2_compact_lexical_block
            FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='storage_v2_compact_lexical_block'::REGCLASS
          AND tgname='storage_v2_compact_lexical_immutable' AND NOT tgisinternal
          AND tgenabled='O' AND tgtype=27 AND tgnargs=0 AND tgqual IS NULL
          AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'compact lexical immutable trigger differs';
    END IF;
END
$authority$;
REVOKE ALL ON storage_v2_compact_lexical_block FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_compact_lexical_block TO mainrag,mainrag_v2_lexical_rank_owner;
GRANT EXECUTE ON FUNCTION storage_v2_posting_fingerprints(TEXT[]),
    storage_v2_posting_fingerprint(TEXT) TO mainrag_v2_lexical_rank_owner;

CREATE OR REPLACE VIEW storage_v2_lexical_segment_all WITH(security_invoker=true) AS
    SELECT * FROM storage_v2_lexical_segment
    UNION ALL
    SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
           item.segment_order,item.text_start::BIGINT,item.text_length::BIGINT,
           item.text_sha256,item.context_prefix,item.chunk_type,item.fts_vector
      FROM storage_v2_compact_lexical_block block
      CROSS JOIN LATERAL unnest(block.segment_orders,block.text_starts,block.text_lengths,
          block.text_hashes,block.context_prefixes,block.chunk_types,block.fts_vectors)
          item(segment_order,text_start,text_length,text_sha256,context_prefix,chunk_type,fts_vector);
ALTER VIEW storage_v2_lexical_segment_all OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON storage_v2_lexical_segment_all FROM PUBLIC;
GRANT SELECT ON storage_v2_lexical_segment_all TO mainrag;

-- Serialize mixed constructors by logical occurrence. Exact flat replays of a
-- compact row are skipped; a different value cannot create a duplicate identity.
CREATE OR REPLACE FUNCTION storage_v2_guard_flat_lexical_insert()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp
SET row_security=on
AS $$
DECLARE
    existing RECORD;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'mainrag.lexical-segment:'||NEW.occurrence_id::TEXT,0));
    SELECT block.source_id,block.artifact_version_id,item.* INTO existing
      FROM storage_v2_compact_lexical_block block
      CROSS JOIN LATERAL unnest(block.segment_orders,block.text_starts,block.text_lengths,
          block.text_hashes,block.context_prefixes,block.chunk_types,block.fts_vectors)
          item(segment_order,text_start,text_length,text_sha256,context_prefix,chunk_type,fts_vector)
     WHERE block.occurrence_id=NEW.occurrence_id
       AND block.block_order=NEW.segment_order/64 AND item.segment_order=NEW.segment_order;
    IF FOUND THEN
        IF (existing.source_id,existing.artifact_version_id,existing.text_start,
            existing.text_length,existing.text_sha256,existing.context_prefix,
            existing.chunk_type,existing.fts_vector) IS DISTINCT FROM
           (NEW.source_id,NEW.artifact_version_id,NEW.text_start,NEW.text_length,
            NEW.text_sha256,NEW.context_prefix,NEW.chunk_type,NEW.fts_vector) THEN
            RAISE EXCEPTION 'lexical segment identity collision';
        END IF;
        RETURN NULL;
    END IF;
    RETURN NEW;
END
$$;
ALTER FUNCTION storage_v2_guard_flat_lexical_insert() OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_guard_flat_lexical_insert() FROM PUBLIC;
DROP TRIGGER IF EXISTS storage_v2_flat_lexical_identity ON storage_v2_lexical_segment;
CREATE TRIGGER storage_v2_flat_lexical_identity
    BEFORE INSERT ON storage_v2_lexical_segment
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_flat_lexical_insert();

DO $constructor$
DECLARE
    definition TEXT;
    declaration TEXT := E'    v_vectors TSVECTOR[];\n';
    marker TEXT := '    INSERT INTO storage_v2_lexical_segment (';
    compact TEXT := $compact$    -- All payload values were independently validated above.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'mainrag.lexical-segment:'||p_occurrence_id::TEXT,0));
    IF array_lower(p_segment_orders,1)=1 AND p_segment_orders[1]%64=0
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
$compact$;
BEGIN
    definition:=pg_get_functiondef(
        'storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])'::REGPROCEDURE);
    IF strpos(definition,compact)>0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,declaration,'')))/length(declaration)<>1
       OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
        RAISE EXCEPTION 'compact lexical constructor definition differs';
    END IF;
    definition:=replace(definition,declaration,declaration
        ||E'    v_low INTEGER;\n    v_high INTEGER;\n'
        ||E'    v_existing_block public.storage_v2_compact_lexical_block;\n');
    EXECUTE replace(definition,marker,compact||marker);
END
$constructor$;

-- Verification, presence, evidence and compatibility collision reads use the
-- same complete logical relation. Leave the index-driven lexical helper's flat
-- branch alone; its compact branch below screens blocks before unpacking.
DO $logical_readers$
DECLARE
    routine REGPROCEDURE;
    definition TEXT;
    replacement TEXT;
BEGIN
    FOR routine IN SELECT oid::REGPROCEDURE FROM pg_proc
        WHERE pronamespace='public'::REGNAMESPACE AND proname LIKE 'storage_v2_%'
          AND proname<>'storage_v2_authorized_lexical_matches'
          AND prokind='f' LOOP
        definition:=pg_get_functiondef(routine);
        replacement:=regexp_replace(definition,
            '\m(FROM|JOIN)([[:space:]]+)(public\.)?storage_v2_lexical_segment\M',
            '\1\2public.storage_v2_lexical_segment_all','g');
        -- The compact constructor's old-row admission probe must inspect only
        -- the flat physical layout, not reject its own immutable compact replay.
        replacement:=replace(replacement,
            'FROM public.storage_v2_lexical_segment_all old',
            'FROM public.storage_v2_lexical_segment old');
        IF replacement<>definition THEN EXECUTE replacement; END IF;
    END LOOP;
END
$logical_readers$;

-- The mixed physical view must be scanned once for the visible generation.
-- A parameterized union can otherwise unpack every compact block once per
-- occurrence. Preserve every original digest/vector and missing-row check.
DO $verification_scope$
DECLARE
    definition TEXT;
    old TEXT := $old$    ), segment_base AS MATERIALIZED ($old$;
    replacement TEXT := $new$    ), lexical AS MATERIALIZED (
        SELECT segment.* FROM public.storage_v2_lexical_segment_all segment
         WHERE segment.source_id=v_source_id
           AND EXISTS (SELECT 1 FROM visible WHERE visible.id=segment.occurrence_id)
    ), segment_base AS MATERIALIZED ($new$;
    old_join TEXT := 'JOIN public.storage_v2_lexical_segment_all lexical';
BEGIN
    definition:=pg_get_functiondef('storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE);
    IF strpos(definition,replacement)>0 AND strpos(definition,old_join)=0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
       OR (length(definition)-length(replace(definition,old_join,'')))/length(old_join)<>1 THEN
        RAISE EXCEPTION 'compact lexical verification scope definition differs';
    END IF;
    EXECUTE replace(replace(definition,old,replacement),old_join,'JOIN lexical');
END
$verification_scope$;
-- Generation visibility is commonly estimated as one row before qualification.
-- The complete verifier has no required correlated join. Prevent repeated scans
-- of its materialized segment/chunk sets when their real cardinality is large.
ALTER FUNCTION storage_v2_verify_lexical_segments(BIGINT) SET enable_nestloop=off;

DO $rank_reader$
DECLARE
    definition TEXT;
    marker TEXT := E'END\n$function$';
    compact TEXT := $compact$    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED (
        SELECT source.id FROM public.sources source
         WHERE source.id=ANY(p_source_ids)
           AND public.storage_v2_can_access_source(source.id,'read')
    ), requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) input(id)
    ), eligible_blocks AS MATERIALIZED (
        SELECT block.* FROM public.storage_v2_compact_lexical_block block
         WHERE block.source_id=ANY(p_source_ids)
           AND block.source_id IN (SELECT id FROM authorized_sources)
           AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
           AND CASE WHEN p_query ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
                         AND lower(p_query) !~ '(^|[[:space:]])or([[:space:]]|$)'
                    THEN block.fingerprints @> public.storage_v2_posting_fingerprints(
                             tsvector_to_array(to_tsvector('simple',p_query)))
                    ELSE TRUE END
    )
    SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
           item.segment_order,ts_rank_cd(item.fts_vector,v_query,0)
      FROM eligible_blocks block
      CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
          item(segment_order,fts_vector)
     WHERE item.fts_vector@@v_query;
$compact$;
BEGIN
    definition:=pg_get_functiondef(
        'storage_v2_authorized_lexical_matches(bigint[],bigint[],text)'::REGPROCEDURE);
    IF strpos(definition,compact)>0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
        RAISE EXCEPTION 'compact lexical rank reader definition differs';
    END IF;
    EXECUTE replace(definition,marker,compact||marker);
END
$rank_reader$;

ALTER FUNCTION storage_v2_lexical_block_fingerprints(TSVECTOR[]) OWNER TO mainrag;
ALTER FUNCTION storage_v2_lexical_block_orders_valid(BIGINT,BIGINT[]) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_lexical_block_fingerprints(TSVECTOR[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_lexical_block_orders_valid(BIGINT,BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_lexical_block_fingerprints(TSVECTOR[]),
    storage_v2_lexical_block_orders_valid(BIGINT,BIGINT[]) TO mainrag_v2_frontier_owner;
COMMIT;
