-- Derived vector caches do not change the identity or lengths cached by readers.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $metadata$
DECLARE definition TEXT;
        marker TEXT := $old$        ELSE
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o
             JOIN public.storage_v2_search_view_document binding ON binding.view_id=o.view_id JOIN (
                SELECT id FROM storage_v2_reader_new UNION SELECT id FROM storage_v2_reader_old
             ) changed ON changed.id=binding.document_id;$old$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_invalidate_reader_metadata()'::REGPROCEDURE);
    IF encode(sha256(convert_to(definition,'UTF8')),'hex')
       IS DISTINCT FROM '5fb887c3860609ef143b218f656ff91f0615423dbc20e70fb604ae4668e6845b'
       OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
        RAISE EXCEPTION 'cache metadata predecessor differs';
    END IF;
    EXECUTE replace(definition,marker,$new$        ELSE
            -- Document metadata uses only id and token_count. The independent
            -- immutable-document trigger still validates all content/cache writes.
            -- EXCEPT ALL also rejects changed identities and row multiplicities.
            IF NOT EXISTS (
                (SELECT id,token_count FROM storage_v2_reader_new
                 EXCEPT ALL SELECT id,token_count FROM storage_v2_reader_old)
                UNION ALL
                (SELECT id,token_count FROM storage_v2_reader_old
                 EXCEPT ALL SELECT id,token_count FROM storage_v2_reader_new)
            ) THEN RETURN NULL; END IF;
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o
             JOIN public.storage_v2_search_view_document binding ON binding.view_id=o.view_id JOIN (
                SELECT id FROM storage_v2_reader_new UNION SELECT id FROM storage_v2_reader_old
             ) changed ON changed.id=binding.document_id;$new$);
END $metadata$;

-- Seek the next block identity before fetching its toasted exact vectors.
DO $lazy$
DECLARE definition TEXT;
        marker TEXT := $old$            RETURN QUERY
            WITH requested AS MATERIALIZED (
                SELECT DISTINCT id FROM unnest(p_occurrence_ids) input(id)
            ), candidates AS MATERIALIZED (
                -- Read block identities once; defer toasted vectors until the
                -- occurrence's earliest exact match is sought.
                SELECT DISTINCT block.occurrence_id,block.source_id,block.artifact_version_id
                  FROM public.storage_v2_compact_lexical_block block
                 WHERE block.source_id=ANY(v_source_ids)
                   AND block.fingerprints @> public.storage_v2_posting_fingerprints(
                       tsvector_to_array(to_tsvector('simple',p_query)))
                   AND EXISTS(SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
            )
            SELECT candidate.occurrence_id,candidate.source_id,candidate.artifact_version_id,
                   matched.segment_order,0.0::REAL
              FROM candidates candidate
              CROSS JOIN LATERAL (
                SELECT item.segment_order
                  FROM public.storage_v2_compact_lexical_block block
                  CROSS JOIN LATERAL (
                    SELECT value.segment_order FROM unnest(block.segment_orders,block.fts_vectors)
                        value(segment_order,fts_vector)
                     WHERE value.fts_vector@@v_query
                     ORDER BY value.segment_order LIMIT 1
                  ) item
                 WHERE block.occurrence_id=candidate.occurrence_id
                   AND block.source_id=candidate.source_id
                   AND block.artifact_version_id=candidate.artifact_version_id
                   AND block.fingerprints @> public.storage_v2_posting_fingerprints(
                       tsvector_to_array(to_tsvector('simple',p_query)))
                 ORDER BY block.block_order LIMIT 1
              ) matched;
$old$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
    IF encode(sha256(convert_to(definition,'UTF8')),'hex')
       IS DISTINCT FROM 'e1069c06c78e30a663535c6a233ca63bccbf440490f6fa8baf2f65d67548a2e1'
       OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
        RAISE EXCEPTION 'lazy compact predecessor differs';
    END IF;
    EXECUTE replace(definition,marker,$new$DECLARE
    lazy_candidate RECORD;
    lazy_payload RECORD;
    lazy_fingerprints INTEGER[] := public.storage_v2_posting_fingerprints(
        tsvector_to_array(to_tsvector('simple', p_query)));
    lazy_after_block BIGINT;
    lazy_next_block BIGINT;
    lazy_first_order BIGINT;
    lazy_point_plan BOOLEAN := FALSE;
BEGIN
    FOR lazy_candidate IN
        WITH requested AS MATERIALIZED (
            SELECT DISTINCT id
              FROM unnest(p_occurrence_ids) input(id)
             WHERE id IS NOT NULL
        ), matching_blocks AS MATERIALIZED (
            -- Match identities once. Neither canonical text nor vector arrays
            -- are projected here; fingerprints are necessary conditions only.
            -- Separate scope filtering from this source/fingerprint probe:
            -- DISTINCT on an unnest otherwise estimates only 200 requested IDs
            -- and can select one physical index rescan per occurrence.
            SELECT block.occurrence_id, block.source_id,
                   block.artifact_version_id, block.block_order
              FROM public.storage_v2_compact_lexical_block block
             WHERE block.source_id = ANY(v_source_ids)
               AND block.fingerprints @> lazy_fingerprints
        ), candidates AS MATERIALIZED (
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
                   min(block.block_order) AS first_block_order
              FROM matching_blocks block
             WHERE EXISTS (SELECT 1 FROM requested
                            WHERE requested.id = block.occurrence_id)
             GROUP BY block.occurrence_id,block.source_id,block.artifact_version_id
        )
        SELECT candidate.* FROM candidates candidate
    LOOP
        -- The scope cursor was custom-planned once with the real arrays.
        -- Point lookups use reusable plans rather than replanning per document.
        -- The function's SET plan_cache_mode restores the caller on return.
        IF NOT lazy_point_plan THEN
            PERFORM set_config('plan_cache_mode','force_generic_plan',TRUE);
            lazy_point_plan := TRUE;
        END IF;
        -- Published blocks are numbered from zero. -1 includes block zero;
        -- using NULL here would exclude every block through SQL three-valued
        -- logic. Do not use a truthiness test for a valid zero order.
        lazy_after_block := -1;
        lazy_next_block := lazy_candidate.first_block_order;
        LOOP
            IF lazy_next_block IS NULL OR lazy_next_block <= lazy_after_block THEN
                RAISE EXCEPTION 'ordered canonical lexical block required';
            END IF;

            -- A separate SPI statement is an execution boundary: fetching or
            -- checking any later vector payload cannot move above the seek.
            SELECT block.segment_orders, block.fts_vectors INTO lazy_payload
              FROM public.storage_v2_compact_lexical_block block
             WHERE block.occurrence_id = lazy_candidate.occurrence_id
               AND block.block_order = lazy_next_block
               AND block.source_id = lazy_candidate.source_id
               AND block.artifact_version_id = lazy_candidate.artifact_version_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'authorized canonical lexical block required'
                    USING ERRCODE = '42501';
            END IF;

            SELECT value.segment_order INTO lazy_first_order
              FROM unnest(lazy_payload.segment_orders, lazy_payload.fts_vectors)
                   value(segment_order, fts_vector)
             WHERE value.fts_vector @@ v_query
             ORDER BY value.segment_order
             LIMIT 1;
            IF FOUND THEN
                occurrence_id := lazy_candidate.occurrence_id;
                source_id := lazy_candidate.source_id;
                artifact_version_id := lazy_candidate.artifact_version_id;
                segment_order := lazy_first_order;
                lexical_score := 0.0::REAL;
                RETURN NEXT;
                EXIT;
            END IF;
            lazy_after_block := lazy_next_block;
            -- The first key was already resolved in the collective scan. Seek
            -- later keys only after an exact miss; keep provenance as a filter
            -- so it cannot displace the ordered occurrence/block PK with a
            -- source-index scan and per-occurrence sort.
            SELECT block.block_order INTO lazy_next_block
              FROM public.storage_v2_compact_lexical_block block
             WHERE block.occurrence_id = lazy_candidate.occurrence_id
               AND block.block_order > lazy_after_block
               AND CASE WHEN block.source_id = lazy_candidate.source_id
                        THEN block.artifact_version_id = lazy_candidate.artifact_version_id
                        ELSE FALSE END
               AND block.fingerprints @> lazy_fingerprints
             ORDER BY block.block_order
             LIMIT 1;
            IF NOT FOUND THEN EXIT; END IF;
        END LOOP;
    END LOOP;
END;
$new$);
END $lazy$;
-- Materialize authorized narrow identities before applying occurrence scope.
DO $derived$
DECLARE definition TEXT;
        marker TEXT := $old$
 WITH authorized_sources AS MATERIALIZED (
  SELECT DISTINCT id FROM unnest(p_source_ids) input(id)
   WHERE storage_v2_can_access_source(id,'read')
 ), first_blocks AS MATERIALIZED (
  -- Sort identities only, then fetch one payload per occurrence. Carrying all
  -- compatible block arrays through DISTINCT ON can spill wide tuples.
  SELECT DISTINCT ON(stored.occurrence_id,stored.source_id,stored.artifact_version_id)
    stored.occurrence_id,stored.source_id,stored.artifact_version_id,stored.block_order
  FROM public.storage_v2_derived_lexical_block stored
  JOIN authorized_sources source ON source.id=stored.source_id
  WHERE stored.occurrence_id=ANY((SELECT p_occurrence_ids OFFSET 0)::BIGINT[])
    AND (p_fingerprints IS NULL OR stored.fingerprints @> p_fingerprints)
  ORDER BY stored.occurrence_id,stored.source_id,stored.artifact_version_id,stored.block_order
 )
 SELECT matching.occurrence_id,matching.source_id,matching.artifact_version_id,stored AS first_block
 FROM first_blocks matching JOIN public.storage_v2_derived_lexical_block stored
  ON stored.occurrence_id=matching.occurrence_id AND stored.block_order=matching.block_order
  AND stored.source_id=matching.source_id AND stored.artifact_version_id=matching.artifact_version_id
$old$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_derived_lexical_candidate_occurrences(bigint[],bigint[],integer[])'::REGPROCEDURE);
    IF encode(sha256(convert_to(definition,'UTF8')),'hex')
       IS DISTINCT FROM 'eb615838cd2c2fdba5dd7ff8e7af548e9cc30392e341355509b607a218bdff21'
       OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
        RAISE EXCEPTION 'derived candidate predecessor differs';
    END IF;
    EXECUTE replace(definition,marker,$new$
WITH authorized_sources AS MATERIALIZED (
    SELECT DISTINCT id FROM unnest(p_source_ids) input(id)
     WHERE storage_v2_can_access_source(id, 'read')
), matching_blocks AS MATERIALIZED (
    -- Source/lexical fingerprint selection is physically separated from
    -- occurrence scoping. An opaque occurrence ANY operand in this physical
    -- scan can be estimated as only a handful of IDs although the actual caller
    -- supplies19053; move it below the materialization boundary.
    -- Project no arrays and no composite block payload.
    SELECT stored.occurrence_id, stored.source_id,
           stored.artifact_version_id, stored.block_order
      FROM public.storage_v2_derived_lexical_block stored
     WHERE cardinality(p_occurrence_ids) > 0
       AND stored.source_id = ANY(ARRAY(SELECT source.id FROM authorized_sources source))
       AND (p_fingerprints IS NULL OR stored.fingerprints @> p_fingerprints)
), first_blocks AS MATERIALIZED (
    -- Scope/dedup/sort only narrow materialized identities. No request-dependent
    -- lookup can be pushed back into the physical derived-block scan above.
    SELECT DISTINCT ON (matching.occurrence_id, matching.source_id,
                        matching.artifact_version_id)
           matching.occurrence_id, matching.source_id,
           matching.artifact_version_id, matching.block_order
      FROM matching_blocks matching
     WHERE matching.occurrence_id = ANY((SELECT p_occurrence_ids OFFSET 0)::BIGINT[])
     ORDER BY matching.occurrence_id, matching.source_id,
              matching.artifact_version_id, matching.block_order
)
SELECT matching.occurrence_id, matching.source_id, matching.artifact_version_id,
       stored AS first_block
  FROM first_blocks matching
  JOIN public.storage_v2_derived_lexical_block stored
    ON stored.occurrence_id = matching.occurrence_id
   AND stored.block_order = matching.block_order
   AND stored.source_id = matching.source_id
   AND stored.artifact_version_id = matching.artifact_version_id
$new$);
END $derived$;
-- A pinned lowercase-only parser permits a necessary literal test on an exact
-- segment window. It preserves cut-boundary lexemes and still rechecks positives.
DO $literal$
DECLARE definition TEXT;
        vector_marker TEXT := $old$     vector:=setweight(to_tsvector('simple',convert_from(
        substring(bytes FROM item.start FOR item.length),'UTF8') COLLATE "default"),'A')
       ||prefix_vector||kind_vector;$old$;
        declaration_marker TEXT := '        prefix_vector TSVECTOR; kind_vector TSVECTOR; matched BOOLEAN;';
        scan_marker TEXT := ' -- Batch the earliest fingerprint-compatible block and its canonical window.';
BEGIN
    definition:=pg_get_functiondef('storage_v2_derived_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
    IF encode(sha256(convert_to(definition,'UTF8')),'hex')
       IS DISTINCT FROM 'ca973beb3a6f822d42f418d5dce9d52a06599969eda74b945da3fb054ebf8635'
       OR (length(definition)-length(replace(definition,vector_marker,'')))/length(vector_marker)<>1
       OR strpos(definition,declaration_marker)=0 OR strpos(definition,scan_marker)=0 THEN
        RAISE EXCEPTION 'derived literal predecessor differs';
    END IF;
    definition:=replace(definition,declaration_marker,declaration_marker||'
        literal_guard BOOLEAN := FALSE; segment_text TEXT;');
    definition:=replace(definition,scan_marker,$guard$ -- The built-in parser returns original text slices; the simple dictionary
 -- and lower(default) use the same libc, per-codepoint lowercase conversion.
 -- Any other parser, dictionary, mapping, provider or major version falls back.
 IF query_term COLLATE "C" ~ '^[a-z0-9_]+$'
    AND current_setting('server_version_num')::INTEGER BETWEEN 180000 AND 189999
    AND getdatabaseencoding()='UTF8' THEN
  SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_database
                  WHERE datname=current_database() AND datlocprovider='c')
     AND EXISTS(SELECT 1 FROM pg_catalog.pg_ts_config config
                 JOIN pg_catalog.pg_ts_parser parser ON parser.oid=config.cfgparser
                 WHERE config.oid='pg_catalog.simple'::REGCONFIG
                   AND parser.prsnamespace='pg_catalog'::REGNAMESPACE
                   AND parser.prsname='default')
     AND EXISTS(SELECT 1 FROM pg_catalog.pg_ts_dict dict
                 JOIN pg_catalog.pg_ts_template template ON template.oid=dict.dicttemplate
                 WHERE dict.oid='pg_catalog.simple'::REGDICTIONARY
                   AND dict.dictinitoption IS NULL
                   AND template.tmplnamespace='pg_catalog'::REGNAMESPACE
                   AND template.tmplname='simple')
     AND (SELECT count(*) FROM pg_catalog.pg_ts_config_map
           WHERE mapcfg='pg_catalog.simple'::REGCONFIG)=19
     AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_ts_config_map
                      WHERE mapcfg='pg_catalog.simple'::REGCONFIG
                        AND (mapdict<>'pg_catalog.simple'::REGDICTIONARY OR mapseqno<>1))
    INTO literal_guard;
 END IF;
$guard$||scan_marker);
    definition:=replace(definition,vector_marker,$new$     segment_text:=convert_from(substring(bytes FROM item.start FOR item.length),'UTF8');
     IF literal_guard AND strpos(lower(segment_text COLLATE "default"),query_term)=0 THEN
      -- Metadata was already tested independently. Absence is only a necessary
      -- body condition; substring presence never establishes a lexical match.
      CONTINUE;
     END IF;
     vector:=setweight(to_tsvector('simple',segment_text COLLATE "default"),'A')
       ||prefix_vector||kind_vector;$new$);
    EXECUTE definition;
END $literal$;
-- Fingerprint collisions are not literal term matches. Recheck within the
-- scoped point lookup before returning wide arrays to the PL/pgSQL decoder.
DO $posting$
DECLARE definition TEXT;
        marker TEXT := $old$            FROM public.storage_v2_compact_posting_block block
           WHERE block.document_id=candidate.document_id
             AND block.block_order=candidate.block_order OFFSET 0$old$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_scoped_query_posting(bigint[],text[])'::REGPROCEDURE);
    IF encode(sha256(convert_to(definition,'UTF8')),'hex')
       IS DISTINCT FROM '11670417e8d17d42b93b82d5fc0be204b62e421cecc5e482ae5a94f77de78059'
       OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
        RAISE EXCEPTION 'scoped posting predecessor differs';
    END IF;
    EXECUTE replace(definition,marker,$new$            FROM public.storage_v2_compact_posting_block block
           WHERE block.document_id=candidate.document_id
             AND block.block_order=candidate.block_order
             AND block.terms && v_terms OFFSET 0$new$);
END $posting$;
COMMIT;
