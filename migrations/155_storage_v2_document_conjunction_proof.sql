-- Preserve complete-document proof and stop reading after the first exact match.
-- Fingerprints only prune; complete vectors and validated exact caches decide.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $guard$
DECLARE expected RECORD;
BEGIN
 FOR expected IN SELECT * FROM (VALUES
 ('storage_v2_source_segment_ranks(bigint[],text)','8491631262a565afc249899cb254872778bdb5a723bf9615df2dd58a4269869e'),
 ('storage_v2_source_segment_ranks_precise(bigint[],text)','f9d61f6bbbf42fdbe598a2becc3d0c27b95fb10f1b85f33b1d01771ea93c58ba'),
 ('storage_v2_source_segment_rank_candidates(bigint[],text)','761b947723abe44065e1bb97564123e80d0ea7406fe9a099d331b29b80d7c13b'),
 ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','a98eca978b246d2b77d10a7d62354e2d068b82ad2bf20ef13bfa3cf7d3eb700f'),
 ('storage_v2_prepare_document_vector()','056de7c14c553e0636bdefbdbb7e3006904174deda2a760809439745d891c546'),
 ('storage_v2_reject_document_mutation()','cbfe2f543c07120feda05b18e48fe4e092a34b44b05fb37ba70f0f7e447c8f20'),
 ('storage_v2_source_segment_body_matches(bigint,text)','a002916abb1bac11ef3f4a32323785b8dc1f25637bfb116e5ea2e713deb294a9'),
 ('storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)','0417d382a3880f6632a13a3972d8df0850dbfb5f372ba9155850586904591b64'),
 ('storage_v2_derived_lexical_first_candidates(bigint[],bigint[],text)','9a4c42cf5fed475c5aff8f11449ef17d56aed8865a413f5578ddab52cf673a56')) value(signature,digest) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(expected.signature::REGPROCEDURE),'UTF8')),'hex')
       IS DISTINCT FROM expected.digest THEN
   RAISE EXCEPTION 'document conjunction predecessor differs: %',expected.signature;
  END IF;
 END LOOP;
END $guard$;

-- A conjunction may be supported by the immutable complete document even when
-- its terms occur in different lexical segments. The independent body proof
-- still uses actual text, never fingerprints or the indexed posting result.
DO $coverage$
DECLARE definition TEXT; marker TEXT:='    SELECT EXISTS (';
BEGIN
 definition:=pg_get_functiondef('storage_v2_source_segment_body_matches(bigint,text)'::REGPROCEDURE);
 IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'document coverage predecessor boundary differs'; END IF;
 definition:=replace(definition,marker,$match$    SELECT EXISTS (
     SELECT 1 FROM occurrence occurrence_row
      JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
      JOIN storage_v2_search_document document ON document.id=binding.document_id
       AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
     WHERE occurrence_row.id=p_occurrence_id
       AND storage_v2_can_access_source(occurrence_row.source_id,'read')
       AND storage_v2_safe_tsvector(document.search_text)@@websearch_to_tsquery('simple',p_query)
    ) OR EXISTS ($match$);
 EXECUTE definition;
END $coverage$;
-- A first-match reader must stop fetching vectors once its earliest matching
-- block has been found. Blocks and segment orders are immutable and ordered.
DO $compact_first$
DECLARE definition TEXT; marker TEXT := $old$            WITH matching_blocks AS MATERIALIZED (
                SELECT block.* FROM public.storage_v2_compact_lexical_block block
                 WHERE block.source_id=ANY(v_source_ids)
                   AND block.fingerprints @> public.storage_v2_posting_fingerprints(
                       tsvector_to_array(to_tsvector('simple',p_query)))
            ), requested AS MATERIALIZED (
                SELECT id FROM unnest(p_occurrence_ids) input(id)
            ), eligible_blocks AS MATERIALIZED (
                SELECT block.* FROM matching_blocks block
                 WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
            )
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
                   min(item.segment_order),0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query
             GROUP BY block.occurrence_id,block.source_id,block.artifact_version_id$old$;
BEGIN
 definition:=pg_get_functiondef('storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'compact first conjunction boundary differs';
 END IF;
 EXECUTE replace(definition,marker,$new$            WITH requested AS MATERIALIZED (
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
              ) matched$new$);
END $compact_first$;

-- The exact first-term cache can also prove a positive conjunction when all
-- terms first occur in the same segment. Different ordinals require fallback.
CREATE FUNCTION storage_v2_cached_first_conjunction_order(
 p_cached_terms TEXT[],p_ordinals SMALLINT[],p_segment_orders BIGINT[],p_terms TEXT[]
) RETURNS BIGINT LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $$
 SELECT CASE WHEN cardinality(p_terms)>1 AND p_cached_terms @> p_terms
                   AND count(DISTINCT ordinal)=1 AND min(ordinal)>0
                   AND min(ordinal)<=cardinality(p_segment_orders)
             THEN p_segment_orders[min(ordinal)] ELSE NULL END
 FROM (SELECT p_ordinals[array_position(p_cached_terms,term)] AS ordinal
         FROM unnest(p_terms) input(term)) ordinals
$$;
ALTER FUNCTION storage_v2_cached_first_conjunction_order(TEXT[],SMALLINT[],BIGINT[],TEXT[]) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_cached_first_conjunction_order(TEXT[],SMALLINT[],BIGINT[],TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_cached_first_conjunction_order(TEXT[],SMALLINT[],BIGINT[],TEXT[])
 TO mainrag,mainrag_v2_frontier_owner,mainrag_v2_lexical_rank_owner;

DO $derived_first$
DECLARE definition TEXT; old_order TEXT; call_first TEXT; call_block TEXT; marker TEXT;
BEGIN
 definition:=pg_get_functiondef('storage_v2_derived_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
 old_order:='(candidate.first_block).segment_orders[
                           (candidate.first_block).first_term_ordinals[
                             array_position((candidate.first_block).first_terms,query_term)]]';
 call_first:='storage_v2_cached_first_conjunction_order((candidate.first_block).first_terms,(candidate.first_block).first_term_ordinals,(candidate.first_block).segment_orders,CASE WHEN p_query !~* ''(^| )(and|or|not)( |$)'' THEN query_terms ELSE NULL END)';
 call_block:='storage_v2_cached_first_conjunction_order(block.first_terms,block.first_term_ordinals,block.segment_orders,CASE WHEN p_query !~* ''(^| )(and|or|not)( |$)'' THEN query_terms ELSE NULL END)';
 IF strpos(definition,old_order)=0 THEN RAISE EXCEPTION 'derived conjunction projection boundary differs'; END IF;
 definition:=replace(definition,old_order,'coalesce('||call_first||','||old_order||')');
 marker:='array_position((candidate.first_block).first_terms,query_term) IS NOT NULL';
 IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'derived conjunction body boundary differs'; END IF;
 definition:=replace(definition,marker,'('||call_first||' IS NOT NULL OR '||marker||')');
 marker:='array_position(block.first_terms,query_term) IS NOT NULL';
 IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'derived conjunction fallback boundary differs'; END IF;
 definition:=replace(definition,marker,'('||call_block||' IS NOT NULL OR '||marker||')');
 marker:='block.segment_orders[block.first_term_ordinals[array_position(block.first_terms,query_term)]]';
 IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'derived conjunction ordinal boundary differs'; END IF;
 definition:=replace(definition,marker,'coalesce('||call_block||','||marker||')');
 EXECUTE definition;
END $derived_first$;

-- Keep every exact normalized lexeme, without positions or duplicate locations.
-- Logical phrase vectors still reconstruct their original complete positions.
DO $lexeme_cache$
DECLARE definition TEXT; marker TEXT;
BEGIN
 definition:=pg_get_functiondef('storage_v2_prepare_document_vector()'::REGPROCEDURE);
 marker:='NEW.fts_simple:=NULL;';
 IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'document lexeme constructor boundary differs'; END IF;
 EXECUTE replace(definition,marker,'NEW.fts_simple:=strip(vector);');
 definition:=pg_get_functiondef('storage_v2_reject_document_mutation()'::REGPROCEDURE);
 marker:='BEGIN
    IF TG_OP<>''UPDATE'' THEN';
 IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'document lexeme mutation boundary differs'; END IF;
 EXECUTE replace(definition,marker,$cache$BEGIN
    -- The only added transition materializes the exact positional-free cache.
    -- Original text, flags, identities and all other columns remain immutable.
    IF TG_OP='UPDATE' AND OLD.fts_simple_derived AND OLD.fts_simple IS NULL
       AND NEW.fts_simple IS NOT NULL
       AND (to_jsonb(NEW)-'fts_simple')=(to_jsonb(OLD)-'fts_simple')
       AND NEW.fts_simple=strip(storage_v2_safe_tsvector(OLD.search_text)) THEN
        RETURN NEW;
    END IF;
    IF TG_OP<>'UPDATE' THEN$cache$);
END $lexeme_cache$;

DO $cached_ranks$
DECLARE signature TEXT; definition TEXT; alias_name TEXT; marker TEXT; changed BOOLEAN;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_source_segment_ranks(bigint[],text)',
  'storage_v2_source_segment_ranks_precise(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);changed:=FALSE;
  FOREACH alias_name IN ARRAY ARRAY['document','candidate'] LOOP
   marker:=alias_name||'.fts_simple_derived AND CASE WHEN';
   IF strpos(definition,marker)>0 THEN
    definition:=replace(definition,marker,alias_name||'.fts_simple_derived AND ('||alias_name||'.fts_simple IS NULL OR public.storage_v2_plain_document_fingerprints(p_query) IS NULL) AND CASE WHEN');
    changed:=TRUE;
   END IF;
  END LOOP;
  IF NOT changed THEN RAISE EXCEPTION 'cached document rank boundary differs: %',signature; END IF;
  EXECUTE definition;
 END LOOP;
END $cached_ranks$;

COMMIT;
