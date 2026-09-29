-- Read postings directly from the authorized generation's document set.
-- A corpus-wide probe followed by a scoped retry repeats work for frequent
-- terms; the scoped helper already preserves exact posting frequencies.
BEGIN;
DO $direct_scoped_postings$
DECLARE
    signature TEXT;
    definition TEXT;
    old_fragment TEXT;
    first_position INTEGER;
    last_position INTEGER;
    replacement TEXT := $new$    scoped_document AS MATERIALIZED (
        SELECT DISTINCT document_id FROM scoped_binding ORDER BY document_id
    ),
    query_posting AS MATERIALIZED (
        SELECT posting.document_id,posting.term,posting.term_frequency
          FROM query_values query
          CROSS JOIN unnest(query.terms) requested_term(value)
          CROSS JOIN LATERAL storage_v2_scoped_term_posting(
              ARRAY(SELECT document_id FROM scoped_document),requested_term.value
          ) posting
    ),
$new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition := pg_get_functiondef(signature::REGPROCEDURE);
        IF strpos(definition,replacement)>0 THEN CONTINUE; END IF;
        IF (length(definition)-length(replace(definition,
            '    query_posting_probe AS MATERIALIZED (','')))
            /length('    query_posting_probe AS MATERIALIZED (')<>1
           OR (length(definition)-length(replace(definition,
            '    scoped_posting AS MATERIALIZED (','')))
            /length('    scoped_posting AS MATERIALIZED (')<>1 THEN
            RAISE EXCEPTION 'scoped posting reader definition differs';
        END IF;
        first_position := strpos(definition,'    query_posting_probe AS MATERIALIZED (');
        last_position := strpos(definition,'    scoped_posting AS MATERIALIZED (');
        IF first_position=0 OR last_position<=first_position THEN
            RAISE EXCEPTION 'scoped posting reader order differs';
        END IF;
        old_fragment := substring(definition FROM first_position FOR last_position-first_position);
        IF encode(sha256(convert_to(old_fragment,'UTF8')),'hex')
             <> '7011c4b8bb9d6ecf0a262d4d83df8f3937355c452fcb5fe70d136cb5e547a114' THEN
            RAISE EXCEPTION 'scoped posting reader body differs';
        END IF;
        EXECUTE replace(definition,old_fragment,replacement);
    END LOOP;
END
$direct_scoped_postings$;
COMMIT;
