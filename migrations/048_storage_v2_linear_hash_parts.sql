-- Migration 048: preserve canonical digests without repeatedly copying roots
--
-- Generation roots can contain hundreds of thousands of parts. Appending each
-- part to an immutable bytea repeatedly copies the growing prefix. Keep the
-- established small-key path, and aggregate large frames into one payload.
-- The domain, signed int8 length/count encoding, row-major array order, and
-- SHA-256 wire format are unchanged. This does not relax statement timeouts.

CREATE OR REPLACE FUNCTION storage_v2_hash_parts(
    p_domain TEXT,
    p_parts BYTEA[]
) RETURNS BYTEA
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_result BYTEA;
    v_part BYTEA;
    v_payload BYTEA;
    v_has_null BOOLEAN;
BEGIN
    v_result := int8send(octet_length(convert_to(p_domain, 'UTF8')))
        || convert_to(p_domain, 'UTF8')
        || int8send(cardinality(p_parts));
    -- This is an implementation crossover, never a part-count limit.
    IF cardinality(p_parts) <= 64 THEN
        FOREACH v_part IN ARRAY p_parts LOOP
            IF v_part IS NULL THEN
                RAISE EXCEPTION 'canonical digest parts cannot be null';
            END IF;
            v_result := v_result || int8send(octet_length(v_part)) || v_part;
        END LOOP;
    ELSE
        -- WITH ORDINALITY preserves FOREACH's row-major order, including
        -- arrays with multiple dimensions or non-default lower bounds.
        -- Explicit ordering is required: unordered aggregation is not a hash
        -- contract. string_agg uses a growing buffer, not bytea concatenation
        -- of the complete accumulated prefix for every element.
        SELECT bool_or(part IS NULL),
               string_agg(int8send(octet_length(part)) || part, ''::BYTEA ORDER BY ordinal)
          INTO v_has_null, v_payload
          FROM unnest(p_parts) WITH ORDINALITY AS input(part, ordinal);
        -- string_agg ignores SQL nulls; reject them explicitly as before.
        IF v_has_null THEN
            RAISE EXCEPTION 'canonical digest parts cannot be null';
        END IF;
        v_result := v_result || v_payload;
    END IF;
    RETURN digest(v_result, 'sha256');
END
$$;
