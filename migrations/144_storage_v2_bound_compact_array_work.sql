-- Avoid hashing toasted dictionaries for an unused Memoize cache and reading
-- their lower bounds again for each exact match. Preserve every term position.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE routine REGPROCEDURE := 'storage_v2_scoped_query_posting(bigint[],text[])'::REGPROCEDURE;
        required RECORD;
BEGIN
    IF encode(sha256(convert_to(pg_get_functiondef(routine),'UTF8')),'hex')
        <>'4152d4f3194bfcd58525b12d649da3ef7936e507e5982a3473c845271ec71556' THEN
        RAISE EXCEPTION 'compact array reader definition differs';
    END IF;
    IF (SELECT proowner FROM pg_proc WHERE oid=routine)<>'mainrag'::REGROLE
       OR NOT has_function_privilege('mainrag',routine,'EXECUTE')
       OR EXISTS (SELECT 1 FROM pg_proc function CROSS JOIN LATERAL
            aclexplode(coalesce(function.proacl,acldefault('f',function.proowner))) permission
            WHERE function.oid=routine AND (permission.grantee<>'mainrag'::REGROLE
                OR permission.privilege_type<>'EXECUTE')) THEN
        RAISE EXCEPTION 'compact array reader authority differs';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('storage_v2_compact_posting_block_terms_check',
         'a4cdca0f1a4aae13746685bb008036894ba47692b45039c0a1492694a114f7f1'),
        ('storage_v2_compact_posting_block_check',
         'f9956d88b804475ce586bdcc42bb6427ba04ca427669291ff769f40a595bda5f')
    ) expected(name,definition_sha256) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
            WHERE conrelid='storage_v2_compact_posting_block'::REGCLASS
              AND conname=required.name AND contype='c' AND convalidated
              AND encode(sha256(convert_to(pg_get_constraintdef(oid),'UTF8')),'hex')
                  =required.definition_sha256) THEN
            RAISE EXCEPTION 'compact array reader bounds constraints differ';
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
        WHERE attrelid='storage_v2_compact_posting_block'::REGCLASS
          AND attname='terms' AND attnotnull AND atttypid='TEXT[]'::REGTYPE)
       OR NOT EXISTS (SELECT 1 FROM pg_attribute
        WHERE attrelid='storage_v2_compact_posting_block'::REGCLASS
          AND attname='term_frequencies' AND attnotnull AND atttypid='BIGINT[]'::REGTYPE) THEN
        RAISE EXCEPTION 'compact array reader bounds constraints differ';
    END IF;
END $guard$;

DO $reader$
DECLARE definition TEXT;
        old_index TEXT := $old$block.term_frequencies[array_lower(block.term_frequencies,1)
               + position.ordinal-array_lower(block.terms,1)]$old$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_scoped_query_posting(bigint[],text[])'::REGPROCEDURE);
    IF (length(definition)-length(replace(definition,old_index,'')))/length(old_index)<>1 THEN
        RAISE EXCEPTION 'compact array reader index boundary differs';
    END IF;
    -- Validated one-dimensional arrays both start at one. array_positions is
    -- retained so even repeated dictionary terms keep their exact frequencies.
    EXECUTE replace(definition,old_index,'block.term_frequencies[position.ordinal]');
END $reader$;
ALTER FUNCTION storage_v2_scoped_query_posting(bigint[],text[]) SET enable_memoize TO off;
COMMIT;
