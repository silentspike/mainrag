-- Probe query terms once, then intersect their postings with authorized corpus
-- bindings. Keep both term and digest checks, full corpus statistics, scoring,
-- tie boundaries, and external identities. Presence needs one existence probe
-- per requested occurrence, not enumeration of all immutable lexical segments.
DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := $old$    scoped_posting AS MATERIALIZED (
        SELECT binding.occurrence_id, binding.component_ordinal, binding.role_weight,
               posting.term, posting.term_frequency
          FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN LATERAL (
              SELECT term, term_frequency FROM storage_v2_search_posting
               WHERE document_id = binding.document_id
                 AND term_sha256 = ANY(query.term_hashes)
               OFFSET 0
          ) posting
         WHERE posting.term = ANY(query.terms)
    ),$old$;
    v_new TEXT := $new$    query_posting AS MATERIALIZED (
        SELECT posting.document_id, posting.term, posting.term_frequency
          FROM query_values query
          CROSS JOIN unnest(query.terms) AS requested_term(value)
          CROSS JOIN LATERAL (
              SELECT document_id, term, term_frequency
                FROM storage_v2_search_posting
               WHERE term = requested_term.value
                 AND term_sha256 = ANY(query.term_hashes)
               OFFSET 0
          ) posting
    ),
    scoped_posting AS MATERIALIZED (
        SELECT binding.occurrence_id, binding.component_ordinal, binding.role_weight,
               posting.term, posting.term_frequency
          FROM scoped_binding binding
          JOIN query_posting posting ON posting.document_id = binding.document_id
    ),$new$;
    v_old_presence TEXT := $old_presence$
BEGIN
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT DISTINCT projection.occurrence_id
      FROM requested
      JOIN storage_v2_legacy_lexical_segment projection
        ON projection.occurrence_id = requested.id
      JOIN occurrence occurrence_row
        ON occurrence_row.id = projection.occurrence_id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id;

    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT DISTINCT occurrence_row.id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id
     WHERE NOT EXISTS (
               SELECT 1
                 FROM storage_v2_legacy_lexical_segment projection
                WHERE projection.occurrence_id = occurrence_row.id
           )
       AND EXISTS (
               SELECT 1
                 FROM storage_v2_lexical_segment segment
                WHERE segment.occurrence_id = occurrence_row.id
                  AND segment.source_id = occurrence_row.source_id
                  AND segment.artifact_version_id = occurrence_row.artifact_version_id
           );
END
$old_presence$;
    v_new_presence TEXT := $new_presence$
BEGIN
    -- One authorized requested item yields one presence row. Presence does not
    -- need to enumerate all of its immutable lexical segments.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT occurrence_row.id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source ON authorized_source.id = occurrence_row.source_id
     WHERE EXISTS (
               SELECT 1 FROM storage_v2_legacy_lexical_segment projection
                WHERE projection.occurrence_id = occurrence_row.id
           ) OR EXISTS (
               SELECT 1 FROM storage_v2_lexical_segment segment
                WHERE segment.occurrence_id = occurrence_row.id
                  AND segment.source_id = occurrence_row.source_id
                  AND segment.artifact_version_id = occurrence_row.artifact_version_id
           );
END
$new_presence$;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=1
           AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
           OR strpos(v_definition,v_new)>0 THEN
            RAISE EXCEPTION 'scoped posting definition differs before term probe reuse';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
    v_signature := 'storage_v2_source_segment_presence(bigint[])';
    SELECT prosrc INTO v_definition FROM pg_proc WHERE oid=v_signature::REGPROCEDURE;
    IF v_definition=v_new_presence THEN RETURN; END IF;
    IF v_definition<>v_old_presence THEN
        RAISE EXCEPTION 'source presence definition differs before existence probes';
    END IF;
    v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
    EXECUTE replace(v_definition,v_old_presence,v_new_presence);
END
$migration$;
