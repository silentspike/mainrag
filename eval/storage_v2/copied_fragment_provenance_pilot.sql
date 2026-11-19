\set ON_ERROR_STOP on
\if :{?max_source_rows}
\else
\set max_source_rows 50000
\endif

-- Invoke psql over the local socket as the administrative operator with
-- source_id, generation_seq, actor_id and query_term. Emits aggregates only.
-- This is a replay of the copied rank branch for one unfiltered named
-- generation and simple ASCII word. The reader and all evidence stay in one
-- snapshot. Closing the connection also rolls back every object in the pilot.
BEGIN ISOLATION LEVEL REPEATABLE READ;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '90s';
SET LOCAL idle_in_transaction_session_timeout = '2min';
SET LOCAL search_path = pg_catalog, public;
SET LOCAL work_mem = '16MB';
SET LOCAL jit = off;
SELECT set_config('app.user_id', :'actor_id', TRUE) IS NOT NULL AS actor_installed \gset

CREATE TEMP TABLE copied_pilot_input ON COMMIT DROP AS
SELECT :'source_id'::BIGINT AS source_id,
       :'generation_seq'::BIGINT AS generation_seq,
       :'query_term'::TEXT AS search_word,
       :'max_source_rows'::INTEGER AS max_source_rows;

DO $scope_guard$
DECLARE
    input RECORD;
BEGIN
    SELECT * INTO STRICT input FROM pg_temp.copied_pilot_input;
    IF inet_client_addr() IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper)
       OR storage_v2_is_admin() IS DISTINCT FROM TRUE
       OR storage_v2_can_access_source(input.source_id, 'read') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'the copied provenance pilot requires an authorized local operator';
    END IF;
    IF input.source_id <= 0 OR input.generation_seq <= 0
       OR input.max_source_rows NOT BETWEEN 1 AND 100000
       OR octet_length(input.search_word) NOT BETWEEN 1 AND 128
       OR input.search_word COLLATE "C" !~ '^[a-z0-9_]+$'
       OR input.search_word IN ('and', 'or', 'not')
       OR numnode(websearch_to_tsquery('simple', input.search_word)) <> 1 THEN
        RAISE EXCEPTION 'the copied provenance pilot requires a bounded named generation and one lowercase ASCII word';
    END IF;
END
$scope_guard$;

-- Match the exact search's unfiltered visible-occurrence membership. The
-- additional limit aborts oversized cohorts; it never truncates an admitted
-- cohort or silently drops siblings of a selected fragmented file.
CREATE TEMP TABLE copied_pilot_scope ON COMMIT DROP AS
SELECT generation.id AS generation_id, input.source_id, input.search_word,
       ARRAY(
           SELECT DISTINCT occurrence.id
           FROM public.occurrence
           JOIN public.generation_item_version membership
             ON membership.source_id = input.source_id
            AND membership.artifact_version_id = occurrence.artifact_version_id
           WHERE occurrence.source_id = input.source_id
             AND membership.valid_from_seq <= generation.generation_seq
             AND (membership.valid_to_seq IS NULL
                  OR membership.valid_to_seq > generation.generation_seq)
           ORDER BY occurrence.id
           LIMIT input.max_source_rows + 1
       ) AS occurrence_ids
FROM pg_temp.copied_pilot_input input
CROSS JOIN LATERAL public.storage_v2_resolve_generation(
    input.source_id, input.generation_seq::TEXT
) generation;

DO $validate_scope$
BEGIN
    IF (SELECT count(*) FROM pg_temp.copied_pilot_scope) <> 1
       OR (SELECT cardinality(occurrence_ids) FROM pg_temp.copied_pilot_scope)
          NOT BETWEEN 1 AND (SELECT max_source_rows FROM pg_temp.copied_pilot_input) THEN
        RAISE EXCEPTION 'copied provenance pilot scope is empty or exceeds its bound';
    END IF;
END
$validate_scope$;

GRANT SELECT ON TABLE pg_temp.copied_pilot_scope TO mainrag, mainrag_v2_frontier_owner;

-- Use the installed and sealed reader as the oracle. Split at the original
-- SQL183 proof boundaries and change one CTE range in the candidate. The
-- canonical/legacy full-text arms and the SQL184 literal/decoded JSONL arm
-- remain byte-for-byte the same, including the selected-fragment identity
-- test inside the latter arm.
DO $replay_readers$
DECLARE
    target_signature CONSTANT REGPROCEDURE :=
        'public.storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::REGPROCEDURE;
    live_name CONSTANT TEXT := 'public.storage_v2_source_segment_rank_candidates(';
    result_type CONSTANT TEXT := 'RETURNS TABLE(occurrence_id bigint, score double precision, segment_order bigint)';
    proof_start CONSTANT TEXT := '), fragment_meta AS MATERIALIZED (';
    proof_end CONSTANT TEXT := '), copied_result AS MATERIALIZED (';
    result_end CONSTANT TEXT := 'SELECT * FROM copied_result UNION ALL SELECT * FROM native_result;';
    legacy_check CONSTANT TEXT := 'storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)';
    legacy_zero_guard CONSTANT TEXT :=
        'IF EXISTS ( SELECT 1 FROM public.storage_v2_lexical_segment generated '
        || 'WHERE generated.occurrence_id=p_occurrence_id AND generated.segment_order=0 '
        || 'UNION ALL SELECT 1 FROM public.storage_v2_lexical_block_all block '
        || 'WHERE block.occurrence_id=p_occurrence_id AND block.block_order=0 '
        || 'AND 0=ANY(block.segment_orders) ) THEN RETURN FALSE; END IF;';
    definition TEXT := pg_get_functiondef(target_signature);
    marker TEXT;
    materialize_definition TEXT;
    candidate_definition TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc routine
        JOIN public.storage_v2_lexical_conversion_function_contract pin
          ON pin.signature = 'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'
        WHERE routine.oid = target_signature AND routine.prosecdef
          AND routine.provolatile = 's'
          AND routine.proowner = 'mainrag_v2_frontier_owner'::REGROLE
          AND pin.definition_sha256 = sha256(convert_to(definition, 'UTF8'))
    ) OR strpos(definition, 'regexp_like(lower(literal_document.search_text)') = 0
      OR strpos(definition, 'STRICT') = 0 THEN
        RAISE EXCEPTION 'copied provenance pilot requires the current SQL184 reader and its catalog pin';
    END IF;
    FOREACH marker IN ARRAY ARRAY[live_name, result_type, proof_start, proof_end, result_end, legacy_check,
                                  'fragment_verified AS MATERIALIZED ('] LOOP
        IF (length(definition) - length(replace(definition, marker, ''))) <> length(marker) THEN
            RAISE EXCEPTION 'copied provenance pilot reader boundary differs: %', marker;
        END IF;
    END LOOP;
    IF strpos(definition, proof_start) >= strpos(definition, proof_end) THEN
        RAISE EXCEPTION 'copied provenance pilot proof boundaries are out of order';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc helper
        WHERE helper.oid = 'public.storage_v2_source_legacy_segment_matches(bigint,text)'::REGPROCEDURE
          AND helper.proowner = 'mainrag_v2_frontier_owner'::REGROLE
          AND helper.prosecdef AND helper.proisstrict AND helper.provolatile = 's'
          AND helper.proconfig @> ARRAY['row_security=on']::TEXT[]
          AND starts_with(btrim(regexp_replace(regexp_replace(
              helper.prosrc, '--[^\n\r]*', '', 'g'), '[[:space:]]+', ' ', 'g')),
              'BEGIN ' || legacy_zero_guard)
    ) THEN
        RAISE EXCEPTION 'copied provenance pilot legacy zero-ordinal guard differs';
    END IF;

    EXECUTE replace(replace(definition, live_name, 'pg_temp.copied_pilot_baseline('),
                    result_end, 'SELECT * FROM copied_result;');

    -- Use the original completeness, 8 MiB per-file/32 MiB per-request
    -- admission and full-file/fragment SHA256 checks to build the certificate.
    -- The query selects which file identities to prepare. The certificate
    -- describes the complete admitted file with all its ordered siblings.
    materialize_definition := left(definition, strpos(definition, proof_end) - 1)
        || $certificate$)
    SELECT member.source_id, member.source_path, member.legacy_file_hash,
           member.occurrence_id, member.artifact_id, member.document_id,
           member.expected_content_hash, member.raw_body_digest,
           member.byte_start, member.byte_end
      FROM fragment_verified verified
      JOIN fragment_bytes member USING(source_id, source_path, legacy_file_hash);
END
$function$
$certificate$;
    materialize_definition := replace(materialize_definition, live_name, 'pg_temp.copied_pilot_prepare(');
    materialize_definition := replace(materialize_definition, result_type,
        'RETURNS TABLE(cert_source_id bigint, cert_source_path text, cert_file_sha256 bytea, '
        || 'cert_occurrence_id bigint, cert_artifact_id bigint, cert_document_id bigint, '
        || 'cert_expected_hash text, cert_body_sha256 bytea, cert_byte_start bigint, cert_byte_end bigint)');
    EXECUTE materialize_definition;

    candidate_definition := overlay(definition PLACING $cached$), fragment_bytes AS MATERIALIZED (
        SELECT verified.source_id, verified.source_path, verified.legacy_file_hash,
               member.occurrence_id, member.artifact_id, member.document_id
          FROM fragment_targets target
          JOIN pg_temp.copied_pilot_files verified USING(source_id, source_path, legacy_file_hash)
          JOIN pg_temp.copied_pilot_scope scope ON scope.generation_id = verified.generation_id
          JOIN pg_temp.copied_pilot_members member ON member.file_id = verified.id
         WHERE scope.occurrence_ids = p_occurrence_ids
           AND scope.search_word = p_query
           AND ARRAY[scope.source_id]::BIGINT[] = p_source_ids
    ), fragment_verified AS MATERIALIZED (
        SELECT DISTINCT source_id, source_path, legacy_file_hash FROM fragment_bytes
    $cached$ FROM strpos(definition, proof_start)
         FOR strpos(definition, proof_end) - strpos(definition, proof_start));
    EXECUTE replace(replace(candidate_definition, live_name, 'pg_temp.copied_pilot_cached('),
                    result_end, 'SELECT * FROM copied_result;');

    -- The legacy helper returns false for these exact rows before opening any
    -- text. Project only the occurrence IDs and segment headers from the
    -- invoker view, which includes compact, encoded and derived blocks. CASE
    -- leaves the original helper and its exact hash check for all other rows.
    candidate_definition := replace(candidate_definition, proof_end, $zero_candidates$), copied_generated_zero AS MATERIALIZED (
        SELECT projection.occurrence_id
          FROM authorized_projection projection
         WHERE EXISTS (
                   SELECT 1 FROM pg_temp.copied_pilot_scope scope
                   WHERE scope.occurrence_ids = p_occurrence_ids
                     AND scope.search_word = p_query
                     AND ARRAY[scope.source_id]::BIGINT[] = p_source_ids
               )
           AND EXISTS (
                   SELECT 1 FROM public.storage_v2_lexical_segment generated
                   WHERE generated.occurrence_id = projection.occurrence_id
                     AND generated.segment_order = 0
                   UNION ALL
                   SELECT 1 FROM public.storage_v2_lexical_block_all block
                   WHERE block.occurrence_id = projection.occurrence_id
                     AND block.block_order = 0
                     AND 0 = ANY(block.segment_orders)
               )
    ), copied_result AS MATERIALIZED ($zero_candidates$);
    candidate_definition := replace(candidate_definition, legacy_check, $skip_zero$CASE WHEN EXISTS (
            SELECT 1 FROM copied_generated_zero generated
            WHERE generated.occurrence_id = projection.occurrence_id
        ) THEN FALSE ELSE storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query) END$skip_zero$);
    EXECUTE replace(replace(candidate_definition, live_name, 'pg_temp.copied_pilot_cached_zero('),
                    result_end, 'SELECT * FROM copied_result;');
END
$replay_readers$;

ALTER FUNCTION pg_temp.copied_pilot_baseline(BIGINT[], TEXT, BIGINT[]) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION pg_temp.copied_pilot_prepare(BIGINT[], TEXT, BIGINT[]) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION pg_temp.copied_pilot_cached(BIGINT[], TEXT, BIGINT[]) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION pg_temp.copied_pilot_cached_zero(BIGINT[], TEXT, BIGINT[]) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION pg_temp.copied_pilot_baseline(BIGINT[], TEXT, BIGINT[]),
    pg_temp.copied_pilot_prepare(BIGINT[], TEXT, BIGINT[]),
    pg_temp.copied_pilot_cached(BIGINT[], TEXT, BIGINT[]),
    pg_temp.copied_pilot_cached_zero(BIGINT[], TEXT, BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg_temp.copied_pilot_baseline(BIGINT[], TEXT, BIGINT[]),
    pg_temp.copied_pilot_prepare(BIGINT[], TEXT, BIGINT[]),
    pg_temp.copied_pilot_cached(BIGINT[], TEXT, BIGINT[]),
    pg_temp.copied_pilot_cached_zero(BIGINT[], TEXT, BIGINT[]) TO mainrag;

\timing on
\echo copied_proof_prepare
SET LOCAL ROLE mainrag;
CREATE TEMP TABLE copied_pilot_verified ON COMMIT DROP AS
SELECT prepared.*, scope.generation_id
FROM pg_temp.copied_pilot_scope scope
CROSS JOIN LATERAL pg_temp.copied_pilot_prepare(
    scope.occurrence_ids, scope.search_word, ARRAY[scope.source_id]::BIGINT[]
) prepared;

CREATE TEMP TABLE copied_pilot_files ON COMMIT DROP AS
SELECT row_number() OVER (ORDER BY generation_id, cert_source_id, cert_source_path, cert_file_sha256) AS id,
       generation_id, cert_source_id AS source_id, cert_source_path AS source_path,
       cert_file_sha256 AS legacy_file_hash, count(*) AS fragment_count,
       max(cert_byte_end) AS canonical_bytes,
       sha256(convert_to(jsonb_agg(jsonb_build_array(
           cert_occurrence_id, cert_artifact_id, cert_document_id, cert_expected_hash,
           encode(cert_body_sha256, 'hex'), cert_byte_start, cert_byte_end
       ) ORDER BY cert_byte_start, cert_occurrence_id)::TEXT, 'UTF8')) AS layout_sha256
FROM pg_temp.copied_pilot_verified
GROUP BY generation_id, cert_source_id, cert_source_path, cert_file_sha256;
ALTER TABLE pg_temp.copied_pilot_files ADD PRIMARY KEY (id);
CREATE UNIQUE INDEX copied_pilot_file_identity ON pg_temp.copied_pilot_files
    (generation_id, source_id, source_path, legacy_file_hash);

CREATE TEMP TABLE copied_pilot_members ON COMMIT DROP AS
SELECT file.id AS file_id, verified.cert_occurrence_id AS occurrence_id,
       verified.cert_artifact_id AS artifact_id, verified.cert_document_id AS document_id,
       verified.cert_expected_hash AS expected_content_hash,
       verified.cert_body_sha256 AS raw_body_digest, verified.cert_byte_start AS byte_start,
       verified.cert_byte_end AS byte_end
FROM pg_temp.copied_pilot_verified verified
JOIN pg_temp.copied_pilot_files file
  ON file.generation_id = verified.generation_id
 AND file.source_id = verified.cert_source_id
 AND file.source_path = verified.cert_source_path
 AND file.legacy_file_hash = verified.cert_file_sha256;
ALTER TABLE pg_temp.copied_pilot_members ADD PRIMARY KEY (file_id, occurrence_id);
ANALYZE pg_temp.copied_pilot_files;
ANALYZE pg_temp.copied_pilot_members;
GRANT SELECT ON TABLE pg_temp.copied_pilot_files, pg_temp.copied_pilot_members
    TO mainrag_v2_frontier_owner;
DROP TABLE pg_temp.copied_pilot_verified;

\echo copied_reader_existing
CREATE TEMP TABLE copied_pilot_before ON COMMIT DROP AS
SELECT found.*
FROM pg_temp.copied_pilot_scope scope
CROSS JOIN LATERAL pg_temp.copied_pilot_baseline(
    scope.occurrence_ids, scope.search_word, ARRAY[scope.source_id]::BIGINT[]
) found;

\echo copied_reader_prepared
CREATE TEMP TABLE copied_pilot_after ON COMMIT DROP AS
SELECT found.*
FROM pg_temp.copied_pilot_scope scope
CROSS JOIN LATERAL pg_temp.copied_pilot_cached(
    scope.occurrence_ids, scope.search_word, ARRAY[scope.source_id]::BIGINT[]
) found;
\timing off

\echo copied_reader_prepared_batched_zero
\timing on
CREATE TEMP TABLE copied_pilot_after_zero ON COMMIT DROP AS
SELECT found.*
FROM pg_temp.copied_pilot_scope scope
CROSS JOIN LATERAL pg_temp.copied_pilot_cached_zero(
    scope.occurrence_ids, scope.search_word, ARRAY[scope.source_id]::BIGINT[]
) found;
\timing off

CREATE TEMP TABLE copied_pilot_mismatches ON COMMIT DROP AS
(SELECT * FROM pg_temp.copied_pilot_before EXCEPT ALL SELECT * FROM pg_temp.copied_pilot_after)
UNION ALL
(SELECT * FROM pg_temp.copied_pilot_after EXCEPT ALL SELECT * FROM pg_temp.copied_pilot_before);

CREATE TEMP TABLE copied_pilot_zero_mismatches ON COMMIT DROP AS
(SELECT * FROM pg_temp.copied_pilot_after EXCEPT ALL SELECT * FROM pg_temp.copied_pilot_after_zero)
UNION ALL
(SELECT * FROM pg_temp.copied_pilot_after_zero EXCEPT ALL SELECT * FROM pg_temp.copied_pilot_after);

SELECT jsonb_build_object(
    'scope_occurrences', (SELECT cardinality(occurrence_ids) FROM pg_temp.copied_pilot_scope),
    'before_hits', (SELECT count(*) FROM pg_temp.copied_pilot_before),
    'after_hits', (SELECT count(*) FROM pg_temp.copied_pilot_after),
    'copied_rank_mismatches', (SELECT count(*) FROM pg_temp.copied_pilot_mismatches),
    'prepared_batched_zero_hits', (SELECT count(*) FROM pg_temp.copied_pilot_after_zero),
    'prepared_batched_zero_mismatches', (SELECT count(*) FROM pg_temp.copied_pilot_zero_mismatches),
    'verified_files', (SELECT count(*) FROM pg_temp.copied_pilot_files),
    'verified_fragments', (SELECT count(*) FROM pg_temp.copied_pilot_members),
    'verified_canonical_bytes', (SELECT coalesce(sum(canonical_bytes), 0) FROM pg_temp.copied_pilot_files),
    'certificate_table_index_bytes',
        pg_total_relation_size('pg_temp.copied_pilot_files'::REGCLASS)
        + pg_total_relation_size('pg_temp.copied_pilot_members'::REGCLASS)
) AS copied_provenance_pilot;

DO $require_exact_parity$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_temp.copied_pilot_mismatches) THEN
        RAISE EXCEPTION 'copied rank changed with prepared provenance';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_temp.copied_pilot_zero_mismatches) THEN
        RAISE EXCEPTION 'copied rank changed with the batched zero-ordinal check';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_temp.copied_pilot_files)
       OR NOT EXISTS (SELECT 1 FROM pg_temp.copied_pilot_before) THEN
        RAISE EXCEPTION 'copied provenance pilot did not exercise the prepared fragment branch';
    END IF;
END
$require_exact_parity$;

ROLLBACK;
