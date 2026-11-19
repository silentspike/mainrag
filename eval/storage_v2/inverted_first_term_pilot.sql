-- Include after the native pilot stages and before its enclosing ROLLBACK.
-- This extension stores only temporary relations and emits aggregate evidence.
\set ON_ERROR_STOP on

\echo native_pilot_inverted_replacement
CREATE TEMP TABLE native_pilot_inverted_blocks ON COMMIT DROP AS
SELECT (row_number() OVER (ORDER BY occurrence_id, block_order) - 1)::integer AS block_id,
       occurrence_id, source_id, artifact_version_id, block_order, segment_orders,
       text_starts, text_lengths, text_hashes, context_prefixes, chunk_types,
       text_byte_starts, text_byte_lengths, segment_masks, first_terms, first_term_ordinals
  FROM pg_temp.native_pilot_original_blocks;
ALTER TABLE pg_temp.native_pilot_inverted_blocks ADD PRIMARY KEY (occurrence_id, block_order);
CREATE UNIQUE INDEX ON pg_temp.native_pilot_inverted_blocks (block_id);
CREATE INDEX ON pg_temp.native_pilot_inverted_blocks (source_id, occurrence_id);

-- Six bytes per coarse fingerprint posting. Batch and bucket keys supply the
-- upper bits. The payload holds the fingerprint suffix, local block number,
-- SHA-256 suffix and first ordinal or ambiguity marker. The prefix B-tree
-- replaces the original block-array GIN for both single words and conjunctions.
CREATE TEMP TABLE native_pilot_inverted_rows ON COMMIT DROP AS
SELECT block.block_id >> 12 AS batch, entry.fingerprint >> 8 AS prefix,
       decode(string_agg(
           lpad(to_hex(entry.fingerprint & 255), 2, '0')
           || lpad(to_hex(block.block_id & 4095), 4, '0')
           || encode(substring(hashed.suffixes_and_ordinals FROM 3 * (entry.ordinal::integer - 1) + 1 FOR 3), 'hex'),
           '' ORDER BY entry.fingerprint, block.block_id), 'hex') AS postings
  FROM pg_temp.native_pilot_inverted_blocks block
  JOIN pg_temp.native_pilot_hash32_blocks hashed USING (occurrence_id, block_order)
 CROSS JOIN LATERAL unnest(hashed.fingerprints) WITH ORDINALITY AS entry(fingerprint, ordinal)
 GROUP BY block.block_id >> 12, entry.fingerprint >> 8;
ALTER TABLE pg_temp.native_pilot_inverted_rows ADD PRIMARY KEY (prefix, batch);
ANALYZE pg_temp.native_pilot_inverted_blocks;
ANALYZE pg_temp.native_pilot_inverted_rows;

CREATE FUNCTION pg_temp.native_pilot_inverted_first_blocks(
    target_source bigint, search_text text, requested_occurrences bigint[]
) RETURNS TABLE (
    occurrence_id bigint, source_id bigint, artifact_version_id bigint,
    segment_order bigint, resolution text
)
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp, public
SET jit = off
AS $reader$
WITH terms AS MATERIALIZED (
    SELECT websearch_to_tsquery('simple', search_text) AS predicate,
           CASE WHEN search_text ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
                  AND lower(search_text) !~ '(^|[[:space:]])or([[:space:]]|$)'
                THEN tsvector_to_array(to_tsvector('simple', search_text)) END AS lexemes
), options AS MATERIALIZED (
    SELECT terms.*, public.storage_v2_posting_fingerprints(terms.lexemes) AS fingerprints,
           CASE WHEN cardinality(terms.lexemes) = 1
                THEN substring(public.digest(terms.lexemes[1], 'sha256') FROM 3 FOR 2) END AS suffix
      FROM terms
), buckets AS MATERIALIZED (
    SELECT packed.batch, substring(packed.postings FROM 1 FOR octet_length(packed.postings)) AS postings,
           probe.fingerprint, options.suffix
      FROM options
     CROSS JOIN LATERAL unnest(options.fingerprints) AS probe(fingerprint)
      JOIN pg_temp.native_pilot_inverted_rows packed ON packed.prefix = probe.fingerprint >> 8
), entries AS MATERIALIZED (
    SELECT (bucket.batch << 12) + 256 * get_byte(bucket.postings, slot.byte_offset + 1)
                                + get_byte(bucket.postings, slot.byte_offset + 2) AS block_id,
           get_byte(bucket.postings, slot.byte_offset + 5) + 1 AS ordinal
      FROM buckets bucket
     CROSS JOIN LATERAL generate_series(0, octet_length(bucket.postings) - 6, 6) AS slot(byte_offset)
     WHERE get_byte(bucket.postings, slot.byte_offset) = (bucket.fingerprint & 255)
       AND (bucket.suffix IS NULL
            OR get_byte(bucket.postings, slot.byte_offset + 5) = 255
            OR substring(bucket.postings FROM slot.byte_offset + 4 FOR 2) = bucket.suffix)
), selected_keys AS (
    SELECT entries.block_id, CASE WHEN cardinality(options.lexemes) = 1 THEN min(entries.ordinal) END AS ordinal
      FROM entries CROSS JOIN options
     GROUP BY entries.block_id, cardinality(options.lexemes), cardinality(options.fingerprints)
    HAVING count(*) = cardinality(options.fingerprints)
    UNION ALL
    -- The current native reader does not use its coarse term filter for phrases
    -- or Boolean operators. Their eligible blocks remain exact-vector reads.
    SELECT block.block_id, NULL::integer
      FROM options
      JOIN pg_temp.native_pilot_inverted_blocks block ON block.source_id = target_source
     WHERE options.fingerprints IS NULL
       AND block.occurrence_id = ANY(requested_occurrences)
), checked AS MATERIALIZED (
    SELECT block.*, selected.ordinal, options.predicate,
           CASE WHEN selected.ordinal BETWEEN 1 AND 64 THEN
               (public.storage_v2_derived_lexical_vectors(block.occurrence_id,
                   ARRAY[block.text_byte_starts[selected.ordinal]], ARRAY[block.text_byte_lengths[selected.ordinal]],
                   ARRAY[block.context_prefixes[selected.ordinal]], ARRAY[block.chunk_types[selected.ordinal]]))[1]
                 @@ options.predicate
           END AS verified
      FROM selected_keys selected
      JOIN pg_temp.native_pilot_inverted_blocks block USING (block_id)
     CROSS JOIN options
     WHERE block.source_id = target_source AND block.occurrence_id = ANY(requested_occurrences)
       AND (SELECT public.storage_v2_can_access_source(target_source, 'read'))
), fallback_inputs AS MATERIALIZED (
    -- Recreate only requested blocks that need exact resolution. One call per
    -- occurrence reads its canonical text once across all those blocks.
    SELECT checked.occurrence_id,
           array_agg(checked.block_id ORDER BY checked.block_order, part.ordinal) AS block_ids,
           array_agg(part.segment_order ORDER BY checked.block_order, part.ordinal) AS segment_orders,
           array_agg(part.start ORDER BY checked.block_order, part.ordinal) AS starts,
           array_agg(part.length ORDER BY checked.block_order, part.ordinal) AS lengths,
           array_agg(part.prefix ORDER BY checked.block_order, part.ordinal) AS prefixes,
           array_agg(part.kind ORDER BY checked.block_order, part.ordinal) AS kinds
      FROM checked
     CROSS JOIN LATERAL unnest(checked.segment_orders, checked.text_byte_starts, checked.text_byte_lengths,
                               checked.context_prefixes, checked.chunk_types) WITH ORDINALITY
                        AS part(segment_order, start, length, prefix, kind, ordinal)
     WHERE checked.verified IS DISTINCT FROM TRUE
     GROUP BY checked.occurrence_id
), exact_hits AS (
    SELECT part.block_id, min(part.segment_order) AS first_order
      FROM fallback_inputs inputs
     CROSS JOIN LATERAL unnest(inputs.block_ids, inputs.segment_orders,
                   public.storage_v2_derived_lexical_vectors(inputs.occurrence_id, inputs.starts,
                       inputs.lengths, inputs.prefixes, inputs.kinds)) AS part(block_id, segment_order, vector)
     CROSS JOIN options
     WHERE part.vector @@ options.predicate
     GROUP BY part.block_id
)
SELECT checked.occurrence_id, checked.source_id, checked.artifact_version_id,
       CASE WHEN checked.verified THEN checked.segment_orders[checked.ordinal]
            ELSE exact_hits.first_order END,
       CASE WHEN checked.verified THEN 'verified'
            WHEN checked.ordinal = 256 THEN 'ambiguous'
            WHEN checked.ordinal IS NULL THEN 'complex'
            ELSE 'failed_recheck' END
  FROM checked LEFT JOIN exact_hits USING (block_id)
$reader$;

\echo native_pilot_inverted_lookup
CREATE TEMP TABLE native_pilot_inverted_readback ON COMMIT DROP AS
SELECT result.* FROM pg_temp.native_pilot_scope scope
 CROSS JOIN LATERAL pg_temp.native_pilot_inverted_first_blocks(
     scope.source_id, scope.query, ARRAY(SELECT occurrence_id FROM pg_temp.native_pilot_roster)
 ) result;

\echo native_pilot_inverted_parity_and_sizes
WITH hits AS (
    SELECT occurrence_id, source_id, artifact_version_id, min(segment_order) AS segment_order, 0.0::real AS lexical_score
      FROM pg_temp.native_pilot_inverted_readback WHERE segment_order IS NOT NULL
     GROUP BY occurrence_id, source_id, artifact_version_id
), differences AS (
    (SELECT * FROM hits EXCEPT ALL SELECT * FROM pg_temp.native_pilot_original_hits)
    UNION ALL
    (SELECT * FROM pg_temp.native_pilot_original_hits EXCEPT ALL SELECT * FROM hits)
)
SELECT (SELECT count(*) FROM hits) AS hits, count(*) AS mismatches,
       (SELECT count(*) FROM pg_temp.native_pilot_inverted_readback WHERE resolution = 'verified') AS verified_blocks,
       (SELECT count(*) FROM pg_temp.native_pilot_inverted_readback WHERE resolution = 'ambiguous') AS ambiguous_blocks,
       (SELECT count(*) FROM pg_temp.native_pilot_inverted_readback WHERE resolution = 'failed_recheck') AS failed_rechecks,
       pg_total_relation_size('pg_temp.native_pilot_original_blocks') AS original_all_bytes,
       pg_total_relation_size('pg_temp.native_pilot_inverted_blocks') AS replacement_blocks_bytes,
       pg_total_relation_size('pg_temp.native_pilot_inverted_rows') AS replacement_postings_bytes,
       pg_total_relation_size('pg_temp.native_pilot_inverted_blocks')
         + pg_total_relation_size('pg_temp.native_pilot_inverted_rows')
         - pg_total_relation_size('pg_temp.native_pilot_original_blocks') AS replacement_minus_original_bytes,
       count(*) = 0 AS native_inverted_equal
  FROM differences
\gset
\echo hits :hits mismatches :mismatches verified_blocks :verified_blocks ambiguous_blocks :ambiguous_blocks failed_rechecks :failed_rechecks
\echo original_all_bytes :original_all_bytes replacement_blocks_bytes :replacement_blocks_bytes replacement_postings_bytes :replacement_postings_bytes replacement_minus_original_bytes :replacement_minus_original_bytes
\if :native_inverted_equal
\else
DO $$ BEGIN RAISE EXCEPTION 'Inverted reader diverged from original native rows'; END $$;
\endif

\echo native_pilot_inverted_semantic_checks
-- Generate an actual ambiguous query from a small set of sampled blocks. Generate
-- the other query forms from consecutive native-vector positions. Keep
-- each supplemental comparison to one sampled occurrence.
CREATE TEMP TABLE native_pilot_inverted_cases ON COMMIT DROP AS
WITH witnesses AS MATERIALIZED (
    SELECT hashed.occurrence_id, hashed.block_order, hashed.fingerprints,
           substring(hashed.suffixes_and_ordinals FROM 1 FOR octet_length(hashed.suffixes_and_ordinals)) AS packed
      FROM (
          SELECT occurrence_id, block_order, fingerprints, suffixes_and_ordinals
            FROM pg_temp.native_pilot_hash32_blocks ORDER BY occurrence_id, block_order LIMIT 8
      ) hashed
), ambiguous_buckets AS MATERIALIZED (
    SELECT witness.occurrence_id, witness.block_order, entry.fingerprint
      FROM witnesses witness
     CROSS JOIN LATERAL unnest(witness.fingerprints) WITH ORDINALITY AS entry(fingerprint, ordinal)
     WHERE get_byte(witness.packed, 3 * (entry.ordinal::integer - 1) + 2) = 255
     LIMIT 32
), ambiguous_blocks AS MATERIALIZED (
    SELECT DISTINCT occurrence_id, block_order FROM ambiguous_buckets
     ORDER BY occurrence_id, block_order LIMIT 2
), witness_vectors AS MATERIALIZED (
    SELECT block.occurrence_id, block.block_order,
           public.storage_v2_derived_lexical_vectors(block.occurrence_id, block.text_byte_starts,
               block.text_byte_lengths, block.context_prefixes, block.chunk_types) AS vectors
      FROM ambiguous_blocks witness
      JOIN pg_temp.native_pilot_inverted_blocks block USING (occurrence_id, block_order)
), ambiguous_case AS (
    SELECT term.lexeme AS query, witness.occurrence_id
      FROM ambiguous_buckets bucket
      JOIN witness_vectors witness USING (occurrence_id, block_order)
     CROSS JOIN LATERAL unnest(witness.vectors) AS segment(vector)
     CROSS JOIN LATERAL unnest(tsvector_to_array(segment.vector)) AS term(lexeme)
     WHERE term.lexeme ~ '^[[:alnum:]_]+$' AND lower(term.lexeme) <> 'or'
       AND numnode(websearch_to_tsquery('simple', term.lexeme)) = 1
       AND public.storage_v2_posting_fingerprint(term.lexeme) = bucket.fingerprint
     LIMIT 1
), sampled_vector AS MATERIALIZED (
    SELECT block.occurrence_id,
           (public.storage_v2_derived_lexical_vectors(block.occurrence_id, block.text_byte_starts[1:1],
                 block.text_byte_lengths[1:1], block.context_prefixes[1:1], block.chunk_types[1:1]))[1] AS vector
      FROM pg_temp.native_pilot_inverted_blocks block ORDER BY block.block_id LIMIT 1
), positions AS MATERIALIZED (
    SELECT sample.occurrence_id, term.lexeme, lexeme_position.ordinal
      FROM sampled_vector sample CROSS JOIN LATERAL unnest(sample.vector) AS term
     CROSS JOIN LATERAL unnest(term.positions) AS lexeme_position(ordinal)
     WHERE term.lexeme ~ '^[[:alnum:]_]+$' AND lower(term.lexeme) <> 'or'
       AND numnode(websearch_to_tsquery('simple', term.lexeme)) = 1
), pair AS (
    SELECT first_word.occurrence_id, first_word.lexeme || ' ' || next_word.lexeme AS words
      FROM positions first_word
      JOIN positions next_word
        ON next_word.occurrence_id = first_word.occurrence_id AND next_word.ordinal = first_word.ordinal + 1
     WHERE first_word.lexeme <> next_word.lexeme
     LIMIT 1
)
SELECT 'ambiguous'::text AS test, query, occurrence_id FROM ambiguous_case
UNION ALL SELECT 'conjunction', words, occurrence_id FROM pair
UNION ALL SELECT 'phrase', '"' || words || '"', occurrence_id FROM pair;

CREATE TEMP TABLE native_pilot_inverted_semantics ON COMMIT DROP AS
SELECT tests.test, baseline.first_order AS expected, candidate.*
  FROM pg_temp.native_pilot_inverted_cases tests CROSS JOIN pg_temp.native_pilot_scope scope
 CROSS JOIN LATERAL (
     SELECT min(segment_order) AS first_order FROM public.storage_v2_derived_lexical_first_candidates(
         ARRAY[tests.occurrence_id], ARRAY[scope.source_id], tests.query
     )
 ) baseline
 CROSS JOIN LATERAL (
     SELECT min(segment_order) AS observed,
            count(*) FILTER (WHERE resolution IN ('ambiguous', 'complex')) AS fallback_blocks
       FROM pg_temp.native_pilot_inverted_first_blocks(scope.source_id, tests.query, ARRAY[tests.occurrence_id])
 ) candidate;

SELECT test, expected = observed AS same_first_order, fallback_blocks
  FROM pg_temp.native_pilot_inverted_semantics ORDER BY test;
DO $semantics$
BEGIN
    IF (SELECT count(*) FROM pg_temp.native_pilot_inverted_semantics
         WHERE expected IS NOT NULL AND expected = observed AND fallback_blocks > 0) <> 3 THEN
        RAISE EXCEPTION 'Inverted reader requires matching positive ambiguous, conjunction and phrase witnesses';
    END IF;
END
$semantics$;

\echo native_pilot_inverted_injected_hash_collision
-- Inject the packed slot for one of the repository's known colliding lexemes.
-- The chosen block has no matching coarse fingerprint. The injected posting
-- deliberately models a failed exact recheck which fallback must reject.
SAVEPOINT native_pilot_injected_collision;
CREATE TEMP TABLE native_pilot_injected_case ON COMMIT DROP AS
SELECT block.block_id, block.occurrence_id,
       public.storage_v2_posting_fingerprint('key32collision71533') AS fingerprint,
       encode(substring(public.digest('key32collision71533', 'sha256') FROM 3 FOR 2), 'hex') AS suffix
  FROM pg_temp.native_pilot_inverted_blocks block
  JOIN pg_temp.native_pilot_hash32_blocks original USING (occurrence_id, block_order)
 WHERE NOT original.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint('key32collision71533')]
 ORDER BY block.block_id
 LIMIT 1;

INSERT INTO pg_temp.native_pilot_inverted_rows AS stored (batch, prefix, postings)
SELECT injected.block_id >> 12, injected.fingerprint >> 8,
       decode(lpad(to_hex(injected.fingerprint & 255), 2, '0')
              || lpad(to_hex(injected.block_id & 4095), 4, '0') || injected.suffix || '00', 'hex')
  FROM pg_temp.native_pilot_injected_case injected
ON CONFLICT (prefix, batch) DO UPDATE SET postings = stored.postings || EXCLUDED.postings;

CREATE TEMP TABLE native_pilot_injected_readback ON COMMIT DROP AS
SELECT result.* FROM pg_temp.native_pilot_scope scope
 CROSS JOIN pg_temp.native_pilot_injected_case injected
 CROSS JOIN LATERAL pg_temp.native_pilot_inverted_first_blocks(
     scope.source_id, 'key32collision11821', ARRAY[injected.occurrence_id]
 ) result;
SELECT count(*) FILTER (WHERE resolution = 'failed_recheck') AS failed_exact_rechecks,
       count(*) FILTER (WHERE segment_order IS NOT NULL) AS unexpected_hits
  FROM pg_temp.native_pilot_injected_readback;
DO $collision$
BEGIN
    IF substring(public.digest('key32collision11821', 'sha256') FROM 1 FOR 4)
       <> substring(public.digest('key32collision71533', 'sha256') FROM 1 FOR 4)
       OR NOT EXISTS (SELECT 1 FROM pg_temp.native_pilot_injected_readback WHERE resolution = 'failed_recheck')
       OR EXISTS (SELECT 1 FROM pg_temp.native_pilot_injected_readback WHERE segment_order IS NOT NULL) THEN
        RAISE EXCEPTION 'Injected full-hash candidate must exercise exact fallback without returning a false hit';
    END IF;
END
$collision$;
ROLLBACK TO SAVEPOINT native_pilot_injected_collision;
RELEASE SAVEPOINT native_pilot_injected_collision;
