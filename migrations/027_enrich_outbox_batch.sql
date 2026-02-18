-- Migration 027: Enrich claim_outbox_batch with chunk_type and language
-- These fields are included in Qdrant payload for future server-side filtering

DROP FUNCTION IF EXISTS claim_outbox_batch(integer);

CREATE FUNCTION claim_outbox_batch(batch_size integer DEFAULT 100)
RETURNS TABLE(
    outbox_id bigint,
    action character varying,
    chunk_id bigint,
    file_id bigint,
    source_id bigint,
    payload jsonb,
    vector vector,
    user_id uuid,
    chunk_type text,
    language text
)
LANGUAGE plpgsql
AS $function$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        UPDATE indexing_outbox o
        SET status = 'processing',
            processing_started_at = NOW()
        WHERE o.id IN (
            SELECT id FROM indexing_outbox
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING o.*
    )
    SELECT
        c.id as outbox_id,
        c.action,
        c.chunk_id,
        c.file_id,
        c.source_id,
        c.payload,
        ce.vector,
        s.user_id,
        ch.chunk_type,
        f.language
    FROM claimed c
    LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
    LEFT JOIN sources s ON c.source_id = s.id
    LEFT JOIN chunks ch ON ch.id = c.chunk_id
    LEFT JOIN files f ON ch.file_id = f.id;
END;
$function$;
