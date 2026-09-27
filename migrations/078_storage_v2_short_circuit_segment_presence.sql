-- Keep conjunctive lexical presence authorization-bound while stopping at the
-- first matching segment for each requested occurrence. The previous set
-- based implementation joined every segment row and then DISTINCTed the
-- occurrence ids, which made presence cost proportional to every segment in a
-- large source even though callers only need a boolean existence check.
DO $$
BEGIN
    IF to_regprocedure('storage_v2_source_segment_presence(bigint[])') IS NULL
       OR NOT EXISTS (
           SELECT 1
             FROM pg_policy
            WHERE polrelid = 'storage_v2_lexical_segment'::REGCLASS
              AND polname = 'storage_v2_lexical_segment_source'
       ) THEN
        RAISE EXCEPTION 'storage-v2 segment presence prerequisites missing';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_source_segment_presence(
    p_occurrence_ids BIGINT[]
) RETURNS TABLE(occurrence_id BIGINT)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
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
         SELECT 1
           FROM storage_v2_lexical_segment segment
          WHERE segment.occurrence_id = occurrence_row.id
            AND segment.source_id = occurrence_row.source_id
            AND segment.artifact_version_id = occurrence_row.artifact_version_id
     )
$$;

ALTER FUNCTION storage_v2_source_segment_presence(BIGINT[]) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_source_segment_presence(BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_presence(BIGINT[]) TO mainrag;
