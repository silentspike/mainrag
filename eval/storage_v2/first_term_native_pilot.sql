-- Same-population native first-hit pilot. Run in one local psql session.
-- Required psql variables are pilot_source_id, pilot_generation_seq,
-- pilot_term and pilot_user_id. Optional bounds below cover a sample only.
-- All writes are temporary and the transaction ends with ROLLBACK.
\set ON_ERROR_STOP on
\if :{?pilot_occurrence_limit}
\else
\set pilot_occurrence_limit 32
\endif
\if :{?pilot_scan_bytes}
\else
\set pilot_scan_bytes 67108864
\endif
\if :{?pilot_statement_timeout}
\else
\set pilot_statement_timeout 120s
\endif

BEGIN ISOLATION LEVEL REPEATABLE READ;
SET LOCAL search_path = pg_catalog, public, pg_temp;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = :'pilot_statement_timeout';
SET LOCAL idle_in_transaction_session_timeout = '30s';
SET LOCAL work_mem = '16MB';
SET LOCAL maintenance_work_mem = '32MB';
SET LOCAL temp_file_limit = '256MB';
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL jit = off;
SET LOCAL app.user_id = :'pilot_user_id';

CREATE TEMP TABLE native_pilot_scope ON COMMIT DROP AS
SELECT generation.id AS generation_id, generation.source_id,
       :'pilot_term'::text AS query,
       (tsvector_to_array(to_tsvector('simple', :'pilot_term')))[1] COLLATE "C" AS lexeme,
       :'pilot_occurrence_limit'::integer AS occurrence_limit,
       :'pilot_scan_bytes'::bigint AS scan_byte_limit
  FROM public.source_generation generation
 WHERE generation.source_id = :'pilot_source_id'::bigint
   AND generation.generation_seq = :'pilot_generation_seq'::bigint;

DO $scope$
DECLARE requested pg_temp.native_pilot_scope%ROWTYPE;
BEGIN
    SELECT * INTO STRICT requested FROM pg_temp.native_pilot_scope;
    IF requested.occurrence_limit IS NULL OR requested.occurrence_limit NOT BETWEEN 1 AND 128
       OR requested.scan_byte_limit IS NULL OR requested.scan_byte_limit NOT BETWEEN 1 AND 268435456 THEN
        RAISE EXCEPTION 'pilot requires a bounded occurrence and logical-byte sample';
    END IF;
    IF requested.query !~ '^[[:alnum:]_]+$'
       OR lower(requested.query) = 'or'
       OR requested.lexeme IS NULL
       OR numnode(websearch_to_tsquery('simple', requested.query)) <> 1
       OR websearch_to_tsquery('simple', requested.query)
          IS DISTINCT FROM plainto_tsquery('simple', requested.lexeme) THEN
        RAISE EXCEPTION 'pilot requires one positive lexical term';
    END IF;
    IF public.storage_v2_reader_metadata_ready(ARRAY[requested.generation_id]) IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'pilot requires authorized complete metadata for the specified generation';
    END IF;
END
$scope$;

\timing on
\echo native_pilot_roster
-- Make the GIN restriction visible as a constant before joining the
-- generation roster. Only bounded identity lists reach the block PK probes.
SELECT generation_id AS native_pilot_generation_id, lexeme AS native_pilot_lexeme,
       public.storage_v2_posting_fingerprints(ARRAY[lexeme]) AS native_pilot_fingerprint
  FROM pg_temp.native_pilot_scope
\gset
CREATE TEMP TABLE native_pilot_roster ON COMMIT DROP AS
WITH probe AS MATERIALIZED (
    SELECT block.occurrence_id, min(block.block_order) AS first_probe_block
      FROM public.storage_v2_derived_lexical_block block
     WHERE block.source_id = :'pilot_source_id'::bigint
       AND block.fingerprints @> :'native_pilot_fingerprint'::integer[]
     GROUP BY block.occurrence_id
), positive_pool AS MATERIALIZED (
    SELECT probe.occurrence_id, binding.document_id, probe.first_probe_block
      FROM probe
     CROSS JOIN LATERAL (
         SELECT metadata.document_id
           FROM public.storage_v2_reader_metadata_binding metadata
          WHERE metadata.generation_id = :'native_pilot_generation_id'::bigint
            AND metadata.occurrence_id = probe.occurrence_id
            AND metadata.component_ordinal = 0
          LIMIT 1
     ) binding
     ORDER BY md5(probe.occurrence_id::text)
     LIMIT 512
), negative_pool AS MATERIALIZED (
    SELECT metadata.occurrence_id, metadata.document_id
      FROM public.storage_v2_reader_metadata_binding metadata
     WHERE metadata.generation_id = :'native_pilot_generation_id'::bigint
       AND metadata.component_ordinal = 0
       AND NOT EXISTS (SELECT 1 FROM probe WHERE probe.occurrence_id = metadata.occurrence_id)
     ORDER BY md5(metadata.occurrence_id::text)
     LIMIT 512
), candidates AS (
    SELECT positive.occurrence_id, positive.document_id, block.artifact_version_id,
           TRUE AS fingerprint_candidate, block.cached
      FROM positive_pool positive
     CROSS JOIN LATERAL (
         SELECT derived.artifact_version_id, :'native_pilot_lexeme'::text = ANY(derived.first_terms) AS cached
           FROM public.storage_v2_derived_lexical_block derived
          WHERE derived.occurrence_id = positive.occurrence_id
            AND derived.block_order = positive.first_probe_block
            AND derived.source_id = :'pilot_source_id'::bigint
          LIMIT 1
     ) block
    UNION ALL
    SELECT negative.occurrence_id, negative.document_id, block.artifact_version_id,
           FALSE AS fingerprint_candidate, FALSE AS cached
      FROM negative_pool negative
     CROSS JOIN LATERAL (
         SELECT derived.artifact_version_id
           FROM public.storage_v2_derived_lexical_block derived
          WHERE derived.occurrence_id = negative.occurrence_id
            AND derived.source_id = :'pilot_source_id'::bigint
          ORDER BY derived.block_order
          LIMIT 1
     ) block
), ranked AS (
    SELECT candidates.*, row_number() OVER (
               PARTITION BY fingerprint_candidate
               ORDER BY cached DESC NULLS LAST, md5(occurrence_id::text)
           ) AS sample_order
      FROM candidates
)
SELECT ranked.occurrence_id, ranked.document_id, ranked.artifact_version_id, ranked.fingerprint_candidate
  FROM ranked
 ORDER BY ranked.sample_order, ranked.fingerprint_candidate DESC
 LIMIT (SELECT occurrence_limit FROM pg_temp.native_pilot_scope);

CREATE TEMP TABLE native_pilot_input_accounting ON COMMIT DROP AS
SELECT count(*) AS occurrences,
       count(*) FILTER (WHERE roster.fingerprint_candidate) AS fingerprint_candidates,
       coalesce(sum(octet_length(document.search_text)::bigint), 0) AS canonical_bytes
  FROM pg_temp.native_pilot_roster roster
  JOIN public.artifact_version artifact ON artifact.id = roster.artifact_version_id
  JOIN public.storage_v2_search_document document
    ON document.id = roster.document_id AND document.component_kind = 'node'
   AND document.node_id = artifact.content_root_node_id;

CREATE TEMP TABLE native_pilot_segment_accounting ON COMMIT DROP AS
SELECT count(*) AS blocks, coalesce(sum(cardinality(block.segment_orders)), 0) AS segments,
       coalesce(sum((
           SELECT sum(lengths.body_length::bigint + octet_length(lengths.prefix) + octet_length(lengths.kind))
             FROM unnest(block.text_byte_lengths, block.context_prefixes, block.chunk_types)
                  AS lengths(body_length, prefix, kind)
       )), 0) AS lexical_bytes
  FROM pg_temp.native_pilot_roster roster
  JOIN public.storage_v2_derived_lexical_block block
    ON block.occurrence_id = roster.occurrence_id AND block.artifact_version_id = roster.artifact_version_id;

SELECT input.*, segment.* FROM pg_temp.native_pilot_input_accounting input
 CROSS JOIN pg_temp.native_pilot_segment_accounting segment;

DO $sample$
BEGIN
    IF (SELECT occurrences FROM pg_temp.native_pilot_input_accounting) = 0
       OR (SELECT occurrences FROM pg_temp.native_pilot_input_accounting)
          <> (SELECT count(*) FROM pg_temp.native_pilot_roster)
       OR (SELECT blocks FROM pg_temp.native_pilot_segment_accounting) = 0 THEN
        RAISE EXCEPTION 'pilot requires a nonempty sample of canonical derived blocks';
    END IF;
    IF (SELECT canonical_bytes FROM pg_temp.native_pilot_input_accounting)
       + (SELECT lexical_bytes FROM pg_temp.native_pilot_segment_accounting)
       > (SELECT scan_byte_limit FROM pg_temp.native_pilot_scope) THEN
        RAISE EXCEPTION 'pilot sample exceeds its logical input-byte budget';
    END IF;
END
$sample$;

\echo native_pilot_original_lookup
CREATE TEMP TABLE native_pilot_original_hits ON COMMIT DROP AS
SELECT hits.* FROM pg_temp.native_pilot_scope scope
 CROSS JOIN LATERAL public.storage_v2_derived_lexical_first_candidates(
     ARRAY(SELECT occurrence_id FROM pg_temp.native_pilot_roster ORDER BY occurrence_id),
     ARRAY[scope.source_id], scope.query
 ) hits;

\echo native_pilot_original_storage
CREATE TEMP TABLE native_pilot_original_blocks ON COMMIT DROP AS
SELECT block.* FROM pg_temp.native_pilot_roster roster
  JOIN public.storage_v2_derived_lexical_block block
    ON block.occurrence_id = roster.occurrence_id AND block.artifact_version_id = roster.artifact_version_id;
ALTER TABLE pg_temp.native_pilot_original_blocks ADD PRIMARY KEY (occurrence_id, block_order);
CREATE INDEX ON pg_temp.native_pilot_original_blocks (source_id, occurrence_id);
CREATE INDEX ON pg_temp.native_pilot_original_blocks USING gin (fingerprints);

\echo native_pilot_prepare_all_terms
-- Reuse the full weighted vector producer once per occurrence. Flatten segment
-- arrays across its blocks, then map each vector back to its block and ordinal.
CREATE TEMP TABLE native_pilot_block_terms ON COMMIT DROP AS
WITH inputs AS MATERIALIZED (
    SELECT block.occurrence_id,
           array_agg(block.block_order ORDER BY block.block_order, part.ordinal) AS block_orders,
           array_agg(part.ordinal ORDER BY block.block_order, part.ordinal) AS ordinals,
           array_agg(part.start ORDER BY block.block_order, part.ordinal) AS starts,
           array_agg(part.length ORDER BY block.block_order, part.ordinal) AS lengths,
           array_agg(part.prefix ORDER BY block.block_order, part.ordinal) AS prefixes,
           array_agg(part.kind ORDER BY block.block_order, part.ordinal) AS kinds
      FROM pg_temp.native_pilot_original_blocks block
     CROSS JOIN LATERAL unnest(block.text_byte_starts, block.text_byte_lengths,
                               block.context_prefixes, block.chunk_types) WITH ORDINALITY
                        AS part(start, length, prefix, kind, ordinal)
     GROUP BY block.occurrence_id
)
SELECT inputs.occurrence_id, part.block_order, word.lexeme COLLATE "C" AS lexeme,
       min(part.ordinal)::smallint AS first_ordinal
  FROM inputs
 CROSS JOIN LATERAL unnest(inputs.block_orders, inputs.ordinals,
              public.storage_v2_derived_lexical_vectors(inputs.occurrence_id, inputs.starts,
                  inputs.lengths, inputs.prefixes, inputs.kinds))
                    AS part(block_order, ordinal, vector)
 CROSS JOIN LATERAL unnest(tsvector_to_array(part.vector)) AS word(lexeme)
 GROUP BY inputs.occurrence_id, part.block_order, word.lexeme COLLATE "C";

\echo native_pilot_dictionary_and_replacement_storage
CREATE TEMP TABLE native_pilot_dictionary ON COMMIT DROP AS
SELECT row_number() OVER (ORDER BY words.lexeme)::integer AS term_id, words.lexeme
  FROM (SELECT DISTINCT lexeme FROM pg_temp.native_pilot_block_terms) words;
ALTER TABLE pg_temp.native_pilot_dictionary ADD PRIMARY KEY (lexeme);

-- Retain both BTrees, the positional metadata, masks and existing first-term
-- cache for the other lexical readers. Replace only the fingerprint array and
-- its GIN with the exact term-ID array, its GIN and one byte per first ordinal.
CREATE TEMP TABLE native_pilot_exact_blocks ON COMMIT DROP AS
WITH packed AS (
    SELECT terms.occurrence_id, terms.block_order,
           array_agg(dictionary.term_id ORDER BY dictionary.term_id) AS term_ids,
           decode(string_agg(lpad(to_hex(terms.first_ordinal - 1), 2, '0'), ''
                         ORDER BY dictionary.term_id), 'hex') AS first_ordinals
      FROM pg_temp.native_pilot_block_terms terms
      JOIN pg_temp.native_pilot_dictionary dictionary USING (lexeme)
     GROUP BY terms.occurrence_id, terms.block_order
)
SELECT block.occurrence_id, block.source_id, block.artifact_version_id,
       block.block_order, block.segment_orders, block.text_starts, block.text_lengths,
       block.text_hashes, block.context_prefixes, block.chunk_types,
       coalesce(packed.term_ids, ARRAY[]::integer[]) AS term_ids,
       coalesce(packed.first_ordinals, '\x'::bytea) AS first_ordinals,
       block.text_byte_starts, block.text_byte_lengths, block.segment_masks,
       block.first_terms, block.first_term_ordinals
  FROM pg_temp.native_pilot_original_blocks block
  LEFT JOIN packed USING (occurrence_id, block_order);
ALTER TABLE pg_temp.native_pilot_exact_blocks ADD PRIMARY KEY (occurrence_id, block_order);
CREATE INDEX ON pg_temp.native_pilot_exact_blocks (source_id, occurrence_id);
CREATE INDEX ON pg_temp.native_pilot_exact_blocks USING gin (term_ids);
ANALYZE pg_temp.native_pilot_dictionary;
ANALYZE pg_temp.native_pilot_exact_blocks;

\echo native_pilot_exact_lookup
CREATE TEMP TABLE native_pilot_exact_hits ON COMMIT DROP AS
SELECT block.occurrence_id, block.source_id, block.artifact_version_id,
       min(block.segment_orders[
           1 + get_byte(block.first_ordinals, array_position(block.term_ids, dictionary.term_id) - 1)
       ]) AS segment_order, 0.0::real AS lexical_score
  FROM pg_temp.native_pilot_scope scope
  JOIN pg_temp.native_pilot_dictionary dictionary ON dictionary.lexeme = scope.lexeme
  JOIN pg_temp.native_pilot_exact_blocks block ON block.source_id = scope.source_id
   AND block.term_ids @> ARRAY[dictionary.term_id]
 GROUP BY block.occurrence_id, block.source_id, block.artifact_version_id;

\echo native_pilot_parity_and_sizes
WITH differences AS (
    (SELECT * FROM pg_temp.native_pilot_exact_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_original_hits)
    UNION ALL
    (SELECT * FROM pg_temp.native_pilot_original_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_exact_hits)
)
SELECT (SELECT count(*) FROM pg_temp.native_pilot_original_hits) AS original_hits,
       (SELECT count(*) FROM pg_temp.native_pilot_exact_hits) AS exact_hits,
       count(*) AS mismatches
  FROM differences;

SELECT pg_total_relation_size('pg_temp.native_pilot_original_blocks') AS sampled_original_all_bytes,
       pg_total_relation_size('pg_temp.native_pilot_exact_blocks') AS sampled_candidate_blocks_bytes,
       pg_total_relation_size('pg_temp.native_pilot_dictionary') AS sampled_candidate_dictionary_bytes,
       pg_total_relation_size('pg_temp.native_pilot_exact_blocks')
         + pg_total_relation_size('pg_temp.native_pilot_dictionary')
         - pg_total_relation_size('pg_temp.native_pilot_original_blocks') AS sampled_candidate_minus_original_bytes,
       pg_total_relation_size('pg_temp.native_pilot_block_terms') AS scratch_term_rows_bytes,
       pg_total_relation_size('public.storage_v2_derived_lexical_block') AS live_all_sources_derived_relation_bytes,
       pg_relation_size('public.idx_storage_v2_derived_lexical_fingerprint') AS live_all_sources_fingerprint_index_bytes;

DO $parity$
BEGIN
    IF EXISTS (
        (SELECT * FROM pg_temp.native_pilot_exact_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_original_hits)
        UNION ALL
        (SELECT * FROM pg_temp.native_pilot_original_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_exact_hits)
    ) THEN
        RAISE EXCEPTION 'native first-hit rows differ from the existing reader';
    END IF;
END
$parity$;

\echo native_pilot_hash32_replacement_storage
-- Reuse each existing 16-bit GIN bucket. A singleton bucket gets a two-byte
-- SHA-256 suffix and a zero-based ordinal. Byte 255 marks ambiguous buckets.
CREATE TEMP TABLE native_pilot_hash32_blocks ON COMMIT DROP AS
WITH hashes AS MATERIALIZED (
    SELECT occurrence_id, block_order, first_ordinal,
           encode(substring(public.digest(lexeme, 'sha256') FROM 1 FOR 4), 'hex') AS key
      FROM pg_temp.native_pilot_block_terms
), buckets AS (
    SELECT occurrence_id, block_order, ('x' || left(key, 4))::bit(16)::integer AS fingerprint,
           CASE WHEN count(*) = 1 THEN right(min(key), 4) || lpad(to_hex(min(first_ordinal) - 1), 2, '0')
                ELSE '0000ff' END AS packed
      FROM hashes
     GROUP BY occurrence_id, block_order, ('x' || left(key, 4))::bit(16)::integer
), packed_blocks AS (
    SELECT occurrence_id, block_order, array_agg(fingerprint ORDER BY fingerprint) AS fingerprints,
           decode(string_agg(packed, '' ORDER BY fingerprint), 'hex') AS suffixes_and_ordinals
      FROM buckets
     GROUP BY occurrence_id, block_order
)
SELECT block.*, coalesce(packed_blocks.suffixes_and_ordinals, '\x'::bytea) AS suffixes_and_ordinals
  FROM pg_temp.native_pilot_original_blocks block
  LEFT JOIN packed_blocks USING (occurrence_id, block_order)
 WHERE block.fingerprints = coalesce(packed_blocks.fingerprints, ARRAY[]::integer[]);
DO $hash32_coverage$
BEGIN
    IF (SELECT count(*) FROM pg_temp.native_pilot_hash32_blocks)
       <> (SELECT count(*) FROM pg_temp.native_pilot_original_blocks) THEN
        RAISE EXCEPTION 'reconstructed hash32 buckets differ from stored fingerprints';
    END IF;
END
$hash32_coverage$;
ALTER TABLE pg_temp.native_pilot_hash32_blocks ADD PRIMARY KEY (occurrence_id, block_order);
CREATE INDEX ON pg_temp.native_pilot_hash32_blocks (source_id, occurrence_id);
CREATE INDEX ON pg_temp.native_pilot_hash32_blocks USING gin (fingerprints);
ANALYZE pg_temp.native_pilot_hash32_blocks;

\echo native_pilot_hash32_lookup
CREATE TEMP TABLE native_pilot_hash32_hits ON COMMIT DROP AS
WITH query AS MATERIALIZED (
    SELECT *, substring(public.digest(lexeme, 'sha256') FROM 3 FOR 2) AS suffix
      FROM pg_temp.native_pilot_scope
), probes AS MATERIALIZED (
    SELECT block.*, query.query, query.suffix,
           3 * (array_position(block.fingerprints, (:'native_pilot_fingerprint'::integer[])[1]) - 1) AS slot
      FROM pg_temp.native_pilot_hash32_blocks block
      JOIN query ON query.source_id = block.source_id
     WHERE block.fingerprints @> :'native_pilot_fingerprint'::integer[]
), possible AS (
    SELECT probes.*, get_byte(suffixes_and_ordinals, slot + 2) + 1 AS ordinal
      FROM probes
     WHERE get_byte(suffixes_and_ordinals, slot + 2) = 255
        OR substring(suffixes_and_ordinals FROM slot + 1 FOR 2) = suffix
), verified AS MATERIALIZED (
    SELECT occurrence_id, source_id, artifact_version_id, segment_orders[ordinal] AS segment_order,
           CASE WHEN ordinal <= 64 THEN
               (public.storage_v2_derived_lexical_vectors(occurrence_id,
                   ARRAY[text_byte_starts[ordinal]], ARRAY[text_byte_lengths[ordinal]],
                   ARRAY[context_prefixes[ordinal]], ARRAY[chunk_types[ordinal]]))[1]
                 @@ websearch_to_tsquery('simple', query)
           END AS exact_match
      FROM possible
), resolved AS MATERIALIZED (
    SELECT occurrence_id, source_id, artifact_version_id, min(segment_order) AS segment_order,
           bool_and(exact_match IS TRUE) AS certain
      FROM verified
     GROUP BY occurrence_id, source_id, artifact_version_id
)
SELECT occurrence_id, source_id, artifact_version_id, segment_order, 0.0::real AS lexical_score
  FROM resolved WHERE certain
UNION ALL
SELECT fallback.* FROM query
 CROSS JOIN LATERAL public.storage_v2_derived_lexical_first_candidates(
     ARRAY(SELECT occurrence_id FROM resolved WHERE NOT certain), ARRAY[query.source_id], query.query
 ) fallback;

\echo native_pilot_hash32_parity_and_sizes
WITH differences AS (
    (SELECT * FROM pg_temp.native_pilot_hash32_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_original_hits)
    UNION ALL
    (SELECT * FROM pg_temp.native_pilot_original_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_hash32_hits)
)
SELECT (SELECT count(*) FROM pg_temp.native_pilot_original_hits) AS original_hits,
       (SELECT count(*) FROM pg_temp.native_pilot_hash32_hits) AS hash32_hits, count(*) AS mismatches,
       pg_total_relation_size('pg_temp.native_pilot_original_blocks') AS sampled_original_all_bytes,
       pg_total_relation_size('pg_temp.native_pilot_hash32_blocks') AS sampled_hash32_all_bytes,
       pg_total_relation_size('pg_temp.native_pilot_hash32_blocks')
         - pg_total_relation_size('pg_temp.native_pilot_original_blocks') AS sampled_hash32_minus_original_bytes
  FROM differences;
DO $hash32_parity$
BEGIN
    IF EXISTS (
        (SELECT * FROM pg_temp.native_pilot_hash32_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_original_hits)
        UNION ALL
        (SELECT * FROM pg_temp.native_pilot_original_hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_hash32_hits)
    ) THEN
        RAISE EXCEPTION 'verified hash32 first-hit rows differ from the existing reader';
    END IF;
END
$hash32_parity$;

ROLLBACK;
