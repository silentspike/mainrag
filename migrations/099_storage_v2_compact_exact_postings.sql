-- Store new exact postings in immutable compressed blocks. Existing flat
-- documents retain their original identities and rows. Fingerprints only select
-- candidates; complete terms are always checked before returning a frequency.
BEGIN;

CREATE OR REPLACE FUNCTION storage_v2_posting_fingerprint(p_term TEXT)
RETURNS INTEGER LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
    SELECT ('x'||substr(encode(public.digest(p_term,'sha256'),'hex'),1,4))::BIT(16)::INTEGER
$$;

CREATE OR REPLACE FUNCTION storage_v2_posting_fingerprints(p_terms TEXT[])
RETURNS INTEGER[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
    SELECT ARRAY(SELECT DISTINCT public.storage_v2_posting_fingerprint(term)
      FROM unnest(p_terms) term ORDER BY 1)
$$;

CREATE TABLE IF NOT EXISTS storage_v2_compact_posting_block (
    document_id BIGINT NOT NULL REFERENCES storage_v2_search_document(id) ON DELETE RESTRICT,
    block_order BIGINT NOT NULL CHECK(block_order>=0),
    terms TEXT[] NOT NULL,
    term_frequencies BIGINT[] NOT NULL,
    fingerprints INTEGER[] GENERATED ALWAYS AS
        (storage_v2_posting_fingerprints(terms)) STORED NOT NULL,
    PRIMARY KEY(document_id,block_order),
    CHECK(array_ndims(terms)=1 AND array_lower(terms,1)=1
       AND cardinality(terms) BETWEEN 1 AND 256
       AND array_position(terms,NULL) IS NULL AND NOT ''=ANY(terms)),
    CHECK(array_ndims(term_frequencies)=1 AND array_lower(term_frequencies,1)=1
       AND cardinality(term_frequencies)=cardinality(terms)
       AND array_position(term_frequencies,NULL) IS NULL
       AND 0<ALL(term_frequencies))
);
CREATE INDEX IF NOT EXISTS idx_storage_v2_compact_posting_fingerprint
    ON storage_v2_compact_posting_block USING GIN(fingerprints);
ALTER TABLE storage_v2_compact_posting_block ENABLE ROW LEVEL SECURITY;
DO $authority$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid='storage_v2_compact_posting_block'::REGCLASS
                      AND polname='storage_v2_compact_posting_admin') THEN
        CREATE POLICY storage_v2_compact_posting_admin ON storage_v2_compact_posting_block
            USING(storage_v2_is_admin()) WITH CHECK(storage_v2_is_admin());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid='storage_v2_compact_posting_block'::REGCLASS
                      AND tgname='storage_v2_compact_posting_immutable') THEN
        CREATE TRIGGER storage_v2_compact_posting_immutable
            BEFORE UPDATE OR DELETE ON storage_v2_compact_posting_block
            FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
    END IF;
END
$authority$;

-- Keep the full term lookup separate from its digest recheck. PostgreSQL
-- otherwise combines the term index with a skip scan over the document-first
-- primary key, visiting the physical corpus before honoring the probe limit.
-- These qualified invoker SQL readers can inline; NULL inputs still yield no
-- rows through their complete equality predicates.
CREATE OR REPLACE FUNCTION storage_v2_posting_probe(p_term TEXT,p_limit BIGINT)
RETURNS TABLE(document_id BIGINT,term TEXT,term_frequency BIGINT)
LANGUAGE sql STABLE
AS $$
    SELECT document_id,term,term_frequency FROM (
      SELECT posting.document_id,posting.term,posting.term_frequency
        FROM (
            SELECT probe.document_id,probe.term,probe.term_frequency,probe.term_sha256
              FROM public.storage_v2_search_posting probe
             WHERE probe.term=p_term OFFSET 0
        ) posting
       WHERE posting.term_sha256=public.digest(p_term,'sha256')
      UNION ALL
      SELECT block.document_id,item.term,item.frequency
        FROM public.storage_v2_compact_posting_block block
        CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
       WHERE block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
         AND item.term=p_term
    ) complete WHERE p_limit BETWEEN 1 AND 4097 LIMIT p_limit
$$;

CREATE OR REPLACE FUNCTION storage_v2_document_posting(p_document_id BIGINT,p_term TEXT)
RETURNS TABLE(term TEXT,term_frequency BIGINT)
LANGUAGE sql STABLE
AS $$
    SELECT posting.term,posting.term_frequency FROM public.storage_v2_search_posting posting
     WHERE posting.document_id=p_document_id
       AND posting.term_sha256=public.digest(p_term,'sha256') AND posting.term=p_term
    UNION ALL
    SELECT item.term,item.frequency FROM public.storage_v2_compact_posting_block block
      CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
     WHERE block.document_id=p_document_id
       AND block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
       AND item.term=p_term
$$;

DO $writer$
DECLARE
    definition TEXT;
    old TEXT := $old$    INSERT INTO storage_v2_search_posting(document_id, term, term_frequency)
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
     GROUP BY token;$old$;
    replacement TEXT := $new$    WITH searchable_tokens AS (
          SELECT token
            FROM regexp_split_to_table(lower(p_search_text), '[^[:alnum:]_]+') AS token
           WHERE token <> ''
          UNION ALL
          SELECT token
            FROM regexp_split_to_table(lower(p_search_text), '[[:space:]]+') AS token
           WHERE token <> '' AND token !~ '^[[:alnum:]_]+$'
             AND token ~ '[[:alnum:]_]'
    ), frequency AS (
        SELECT token,COUNT(*) frequency FROM searchable_tokens GROUP BY token
    ), numbered AS (
        SELECT token,frequency,
               (row_number() OVER (ORDER BY token COLLATE "C")-1)/256 block_order
          FROM frequency
    )
    INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies)
    SELECT v_document.id,block_order,array_agg(token ORDER BY token COLLATE "C"),
           array_agg(frequency ORDER BY token COLLATE "C")
      FROM numbered GROUP BY block_order;$new$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_put_search_document(text,text,bigint,text,text[])'::REGPROCEDURE);
    IF strpos(definition,replacement)>0 AND strpos(definition,old)=0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
       OR strpos(definition,replacement)>0 THEN
        RAISE EXCEPTION 'compact constructor differs from the accepted writer';
    END IF;
    EXECUTE replace(definition,old,replacement);
END
$writer$;

CREATE OR REPLACE FUNCTION storage_v2_scoped_term_posting(
    p_document_ids BIGINT[],p_term TEXT
) RETURNS TABLE(document_id BIGINT,term TEXT,term_frequency BIGINT)
LANGUAGE plpgsql STABLE STRICT
SET search_path=pg_catalog,public,pg_temp
SET plan_cache_mode=force_custom_plan
AS $$
DECLARE
    v_document_estimate DOUBLE PRECISION;
BEGIN
    SELECT GREATEST(reltuples,1) INTO v_document_estimate FROM pg_class
     WHERE oid='public.storage_v2_search_document'::REGCLASS;
    IF cardinality(p_document_ids)>4096
       AND cardinality(p_document_ids)>=v_document_estimate*0.1 THEN
        RETURN QUERY
        SELECT posting.document_id,posting.term,posting.term_frequency::BIGINT
          FROM public.storage_v2_search_posting posting
         WHERE posting.term_sha256=public.digest(p_term,'sha256')
           AND posting.term=p_term AND posting.document_id=ANY(p_document_ids)
        UNION ALL
        SELECT block.document_id,item.term,item.frequency
          FROM public.storage_v2_compact_posting_block block
          CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
         WHERE block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
           AND block.document_id=ANY(p_document_ids) AND item.term=p_term;
    ELSE
        RETURN QUERY
        SELECT requested.id,posting.term,posting.term_frequency
          FROM (SELECT DISTINCT id FROM unnest(p_document_ids) input(id)) requested
          CROSS JOIN LATERAL (
              SELECT probe.term,probe.term_frequency
                FROM public.storage_v2_document_posting(requested.id,p_term) probe OFFSET 0
          ) posting;
    END IF;
END
$$;

DO $readers$
DECLARE
    signature TEXT;
    definition TEXT;
    old_probe TEXT := $old$              SELECT document_id, term, term_frequency
                FROM storage_v2_search_posting
               WHERE term = requested_term.value
                 AND term_sha256 = ANY(query.term_hashes)
               LIMIT 4097
               OFFSET 0$old$;
    new_probe TEXT := $new$              SELECT document_id,term,term_frequency
                FROM public.storage_v2_posting_probe(requested_term.value,4097)
               OFFSET 0$new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,new_probe)>0 AND strpos(definition,old_probe)=0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,old_probe,'')))/length(old_probe)<>1
           OR strpos(definition,new_probe)>0 THEN
            RAISE EXCEPTION 'mixed exact posting reader differs';
        END IF;
        EXECUTE replace(definition,old_probe,new_probe);
    END LOOP;
END
$readers$;

-- Qualification uses the same complete logical posting interface as search.
DO $evidence$
DECLARE
    definition TEXT;
    old TEXT := $old$COALESCE((SELECT term_frequency FROM storage_v2_search_posting posting
                          WHERE posting.document_id = candidate.document_id
                            AND posting.term_sha256 = digest(lower(p_query), 'sha256')
                            AND posting.term = lower(p_query)), 0)$old$;
    replacement TEXT := $new$COALESCE((SELECT posting.term_frequency
                            FROM public.storage_v2_document_posting(candidate.document_id,lower(p_query)) posting),0)$new$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'::REGPROCEDURE);
    IF strpos(definition,replacement)>0 AND strpos(definition,old)=0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
       OR strpos(definition,replacement)>0 THEN
        RAISE EXCEPTION 'mixed qualification posting definition differs';
    END IF;
    EXECUTE replace(definition,old,replacement);
END
$evidence$;

ALTER TABLE storage_v2_compact_posting_block OWNER TO mainrag;
ALTER FUNCTION storage_v2_posting_fingerprint(TEXT) OWNER TO mainrag;
ALTER FUNCTION storage_v2_posting_fingerprints(TEXT[]) OWNER TO mainrag;
ALTER FUNCTION storage_v2_posting_probe(TEXT,BIGINT) OWNER TO mainrag;
ALTER FUNCTION storage_v2_document_posting(BIGINT,TEXT) OWNER TO mainrag;
REVOKE ALL ON storage_v2_compact_posting_block FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_posting_fingerprint(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_posting_fingerprints(TEXT[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_posting_probe(TEXT,BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_document_posting(BIGINT,TEXT) FROM PUBLIC;

-- LZ4 applies to new values. Existing immutable rows are not rewritten.
ALTER TABLE storage_v2_search_document ALTER COLUMN search_text SET COMPRESSION lz4;
ALTER TABLE storage_v2_search_document ALTER COLUMN exact_identifiers SET COMPRESSION lz4;
ALTER TABLE storage_v2_search_document ALTER COLUMN fts_simple SET COMPRESSION lz4;
ALTER TABLE storage_v2_lexical_segment ALTER COLUMN fts_vector SET COMPRESSION lz4;
ALTER TABLE storage_v2_compact_posting_block ALTER COLUMN terms SET COMPRESSION lz4;
ALTER TABLE storage_v2_compact_posting_block ALTER COLUMN term_frequencies SET COMPRESSION lz4;
ALTER FUNCTION storage_v2_scoped_term_posting(BIGINT[],TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_scoped_term_posting(BIGINT[],TEXT) FROM PUBLIC;

-- Validate replay against an independently parsed expected shape. Do not
-- silently accept a same-named relation, weakened check, policy or index.
CREATE TEMP TABLE storage_v2_expected_compact_block (
    document_id BIGINT NOT NULL,
    block_order BIGINT NOT NULL CHECK(block_order>=0),
    terms TEXT[] NOT NULL,
    term_frequencies BIGINT[] NOT NULL,
    fingerprints INTEGER[] GENERATED ALWAYS AS
        (storage_v2_posting_fingerprints(terms)) STORED NOT NULL,
    PRIMARY KEY(document_id,block_order),
    CHECK(array_ndims(terms)=1 AND array_lower(terms,1)=1
       AND cardinality(terms) BETWEEN 1 AND 256
       AND array_position(terms,NULL) IS NULL AND NOT ''=ANY(terms)),
    CHECK(array_ndims(term_frequencies)=1 AND array_lower(term_frequencies,1)=1
       AND cardinality(term_frequencies)=cardinality(terms)
       AND array_position(term_frequencies,NULL) IS NULL
       AND 0<ALL(term_frequencies))
) ON COMMIT DROP;
DO $shape$
DECLARE
    expected JSONB;
    actual JSONB;
    relation REGCLASS := 'public.storage_v2_compact_posting_block'::REGCLASS;
BEGIN
    SELECT jsonb_agg(jsonb_build_array(a.attname,a.atttypid,a.attnotnull,a.attgenerated,
                                      pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum)
      INTO expected FROM pg_attribute a
      LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
     WHERE a.attrelid='pg_temp.storage_v2_expected_compact_block'::REGCLASS
       AND a.attnum>0 AND NOT a.attisdropped;
    SELECT jsonb_agg(jsonb_build_array(a.attname,a.atttypid,a.attnotnull,a.attgenerated,
                                      pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum)
      INTO actual FROM pg_attribute a
      LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
     WHERE a.attrelid=relation AND a.attnum>0 AND NOT a.attisdropped;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'compact posting column identity differs';
    END IF;
    SELECT jsonb_agg(pg_get_constraintdef(oid) ORDER BY pg_get_constraintdef(oid))
      INTO expected FROM pg_constraint
     WHERE conrelid='pg_temp.storage_v2_expected_compact_block'::REGCLASS;
    SELECT jsonb_agg(pg_get_constraintdef(oid) ORDER BY pg_get_constraintdef(oid))
      INTO actual FROM pg_constraint WHERE conrelid=relation AND contype<>'f';
    IF actual IS DISTINCT FROM expected
       OR (SELECT count(*) FROM pg_constraint WHERE conrelid=relation AND contype='f')<>1
       OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=relation AND contype='f'
           AND confrelid='storage_v2_search_document'::REGCLASS
           AND conkey=ARRAY[1]::SMALLINT[] AND confkey=ARRAY[1]::SMALLINT[]
           AND confdeltype='r' AND confupdtype='a' AND confmatchtype='s'
           AND convalidated AND NOT condeferrable AND NOT condeferred) THEN
        RAISE EXCEPTION 'compact posting constraint identity differs';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_am am ON am.oid=c.relam
        WHERE c.oid='idx_storage_v2_compact_posting_fingerprint'::REGCLASS
          AND i.indrelid=relation AND am.amname='gin'
          AND i.indisvalid AND i.indisready AND NOT i.indisunique
          AND i.indpred IS NULL AND i.indexprs IS NULL
          AND i.indnatts=1 AND i.indnkeyatts=1
          AND ARRAY(SELECT unnest(i.indkey))=ARRAY[5]::SMALLINT[]
          AND ARRAY(SELECT unnest(i.indclass))=ARRAY[
              (SELECT oid FROM pg_opclass WHERE opcname='array_ops'
                  AND opcnamespace='pg_catalog'::REGNAMESPACE AND opcmethod=am.oid)
          ]::OID[]) THEN
        RAISE EXCEPTION 'compact posting index identity differs';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=relation
        AND polname='storage_v2_compact_posting_admin' AND polcmd='*' AND polpermissive
        AND polroles=ARRAY[0]::OID[] AND pg_get_expr(polqual,polrelid)='storage_v2_is_admin()'
        AND pg_get_expr(polwithcheck,polrelid)='storage_v2_is_admin()')
       OR (SELECT count(*) FROM pg_policy WHERE polrelid=relation)<>1
       OR NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=relation
           AND tgname='storage_v2_compact_posting_immutable' AND tgtype=27
           AND tgenabled='O' AND tgqual IS NULL AND tgnargs=0
           AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE)
       OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=relation AND relrowsecurity
           AND NOT relforcerowsecurity AND relowner='mainrag'::REGROLE) THEN
        RAISE EXCEPTION 'compact posting authority identity differs';
    END IF;
END
$shape$;
DROP TABLE pg_temp.storage_v2_expected_compact_block;

COMMIT;
