-- Scope copied projection guards and materialize only returned explanations.
-- Reader-only changes preserve immutable rows, producer identities and pointers.
BEGIN;
DO $query_scope$
DECLARE
 signature TEXT;
 definition TEXT;
 old_authorization TEXT := $old$        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')$old$;
 new_authorization TEXT := $new$        SELECT source.id FROM sources source
         WHERE source.id=ANY(v_requested_sources)
           AND storage_v2_can_access_source(source.id, 'read')$new$;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_source_segment_ranks(bigint[],text)',
  'storage_v2_source_segment_ranks_precise(bigint[],text)'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);
  IF strpos(definition,new_authorization)>0 AND strpos(definition,'>=1024')>0
     AND strpos(definition,'>=32768')=0 THEN CONTINUE; END IF;
  IF (length(definition)-length(replace(definition,old_authorization,'')))/length(old_authorization)<>2
     OR (length(definition)-length(replace(definition,'>=32768','')))/length('>=32768')<>2 THEN
   RAISE EXCEPTION 'copied lexical requested source or matching document scope differs';
  END IF;
  EXECUTE replace(replace(definition,old_authorization,new_authorization),'>=32768','>=1024');
 END LOOP;
END
$query_scope$;


-- The authorized requested occurrence already supplied the immutable view.
-- Carry that identity through ranking instead of reading its heap row again.
DO $carried_view$
DECLARE
    signature TEXT;
    definition TEXT;
    old TEXT := $old$               projection.legacy_chunk_id
          FROM requested$old$;
    replacement TEXT := $new$               projection.legacy_chunk_id,occurrence_row.view_id
          FROM requested$new$;
    redundant TEXT := '      JOIN occurrence occurrence_row ON occurrence_row.id=projection.occurrence_id';
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,replacement)>0 AND strpos(definition,redundant)=0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
           OR (length(definition)-length(replace(definition,redundant,'')))/length(redundant)<>1 THEN
            RAISE EXCEPTION 'authorized copied view identity differs';
        END IF;
        definition:=replace(definition,old,replacement);
        definition:=replace(definition,redundant,E'');
        definition:=replace(definition,
            E'\n        ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0',
            E'\n        ON binding.view_id=projection.view_id AND binding.ordinal=0');
        definition:=replace(definition,
            'storage_v2_source_legacy_segment_matches(occurrence_row.id,p_query)',
            'storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)');
        EXECUTE definition;
    END LOOP;
END
$carried_view$;

DO $canonical_projection$
DECLARE
 definition TEXT;
 original_body TEXT;
 replacement_body TEXT;
 marker TEXT := '-- Reject complete canonical projections before body or vector reads.';
BEGIN
 definition:=pg_get_functiondef('storage_v2_source_legacy_segment_matches(bigint,text)'::REGPROCEDURE);
 IF strpos(definition,marker)>0 THEN RETURN; END IF;
 IF strpos(definition,' LANGUAGE sql')=0
    OR strpos(definition,'AND segment.segment_order>0')=0
    OR strpos(definition,'AND NOT EXISTS (SELECT 1 FROM public.storage_v2_lexical_segment_all generated')=0
    OR strpos(definition,'AND segment.text_sha256=sha256')=0 THEN
   RAISE EXCEPTION 'source-backed offset segment guard identity differs';
 END IF;
 original_body:=split_part(definition,'$function$',2);
 replacement_body:=E'\nBEGIN\n    '||marker||E'\n    IF EXISTS (\n'
    ||'        SELECT 1 FROM public.storage_v2_lexical_segment generated'
    ||E'\n         WHERE generated.occurrence_id=p_occurrence_id AND generated.segment_order=0\n'
    ||E'        UNION ALL\n'
    ||'        SELECT 1 FROM public.storage_v2_compact_lexical_block block'
    ||E'\n         WHERE block.occurrence_id=p_occurrence_id AND block.block_order=0\n'
    ||E'           AND 0=ANY(block.segment_orders)\n'
    ||E'    ) THEN RETURN FALSE; END IF;\n    RETURN '
    ||regexp_replace(regexp_replace(btrim(original_body),';[[:space:]]*$',''),'^[[:space:]]*SELECT[[:space:]]+','')||E';\nEND;\n';
 EXECUTE replace(replace(definition,' LANGUAGE sql',' LANGUAGE plpgsql'),original_body,replacement_body);
END
$canonical_projection$;



-- Copied projections already provide the complete lexical rank tier. Defer
-- their term explanations until the final result set, retaining corpus-wide
-- normalization and complete scoring for generated and unprojected rows.
DO $late_explanations$
DECLARE
    signature TEXT;
    definition TEXT;
    original_rows TEXT;
    original_best TEXT;
    ranked_rows TEXT;
    returned_rows TEXT;
    returned_best TEXT;
    returned_aggregate TEXT;
    marker TEXT := 'returned_term_aggregate AS MATERIALIZED';
    lexical TEXT := $new$'lexical', CASE WHEN ordered.segment_score>=1000000.0 THEN
                    COALESCE((SELECT lexical_terms FROM returned_term_aggregate explanation
                               WHERE explanation.occurrence_id=ordered.id),0.0)
                    +1.5*cardinality(ordered.matched_phrases)
                    +2.0*cardinality(ordered.matched_exact)
                    ELSE lexical_score END,$new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,marker)>0 THEN CONTINUE; END IF;
        IF strpos(definition,'WHEN staged.segment_score >= 1000000.0')=0
           OR strpos(definition,'lexical_ranks AS MATERIALIZED')=0
           OR strpos(definition,'score_boundary AS MATERIALIZED')=0
           OR (length(definition)-length(replace(definition,'''lexical'', lexical_score,','')))
                /length('''lexical'', lexical_score,')<>1
           OR (length(definition)-length(replace(definition,'FROM best_term detail','')))
                /length('FROM best_term detail')<>1 THEN
            RAISE EXCEPTION 'late lexical explanation identity differs';
        END IF;
        original_rows:=substring(definition FROM strpos(definition,'    term_rows AS (')
            FOR strpos(definition,'    term_match_aggregate AS MATERIALIZED (')-strpos(definition,'    term_rows AS ('));
        original_best:=substring(definition FROM strpos(definition,'    best_term AS (')
            FOR strpos(definition,'    term_aggregate AS MATERIALIZED (')-strpos(definition,'    best_term AS ('));
        IF strpos(original_rows,'WHERE posting.term = ANY(query.score_terms)')=0
           OR strpos(original_rows,'FROM scoped_posting posting')=0
           OR strpos(original_best,'FROM term_rows')=0 THEN
            RAISE EXCEPTION 'term contribution definition differs';
        END IF;
        ranked_rows:=replace(original_rows,'WHERE posting.term = ANY(query.score_terms)',
            'WHERE posting.term = ANY(query.score_terms)'||E'
'
            ||'           AND posting.occurrence_id NOT IN (SELECT occurrence_id FROM lexical_ranks copied'
            ||E'
              WHERE copied.score>=1000000.0 AND copied.occurrence_id IS NOT NULL)');
        returned_rows:=replace(replace(original_rows,'term_rows AS (','returned_term_rows AS ('),
            'FROM scoped_posting posting','FROM scoped_posting posting'||E'
'
            ||'          JOIN ordered returned ON returned.id=posting.occurrence_id');
        returned_best:=replace(replace(original_best,'best_term AS (','returned_best_term AS ('),
            'FROM term_rows','FROM returned_term_rows');
        returned_aggregate:=$returned$    returned_term_aggregate AS MATERIALIZED (
        SELECT occurrence_id,SUM(contribution) AS lexical_terms
          FROM returned_best_term GROUP BY occurrence_id
    ),
$returned$;
        definition:=replace(definition,original_rows,ranked_rows);
        definition:=replace(definition,'    results AS (',
            returned_rows||returned_best||returned_aggregate||'    results AS (');
        definition:=replace(definition,'''lexical'', lexical_score,',lexical);
        definition:=replace(definition,'FROM best_term detail','FROM returned_best_term detail');
        EXECUTE definition;
    END LOOP;
END
$late_explanations$;

COMMIT;
