-- A candidate is staged in one transaction. Holding one advisory lock per
-- occurrence exhausts PostgreSQL's shared lock table for large sources.
-- Both lexical constructors serialize on the source instead; the existing
-- composite occurrence/source foreign key keeps the lock identity bound.
BEGIN;
DO $source_scoped_lexical_lock$
DECLARE
    signature TEXT;
    definition TEXT;
    old_lock TEXT;
    new_lock TEXT;
    expected_sha256 TEXT;
    already_applied BOOLEAN;
BEGIN
    FOR signature, old_lock, new_lock, expected_sha256 IN
        SELECT * FROM (VALUES
            (
                'storage_v2_guard_flat_lexical_insert()',
                '''mainrag.lexical-segment:''||NEW.occurrence_id::TEXT',
                '''mainrag.lexical-segment-source:''||NEW.source_id::TEXT',
                'bffb7fe895332962a3df4e8b25e71eceeb1407e9f09bf487bffa79bf7593b875'
            ),
            (
                'storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])',
                '''mainrag.lexical-segment:''||p_occurrence_id::TEXT',
                '''mainrag.lexical-segment-source:''||v_source_id::TEXT',
                'f81106124f70509cdec721e68e27d86fc15c85bc76c16430c590ca3dc6070613'
            )
        ) AS guarded(function_signature, previous_lock, source_lock, body_sha256)
    LOOP
        definition := pg_get_functiondef(signature::REGPROCEDURE);
        already_applied := strpos(definition, new_lock) > 0;
        IF already_applied THEN
            IF strpos(definition, old_lock) > 0 THEN
                RAISE EXCEPTION 'mixed lexical lock scopes in %', signature;
            END IF;
            definition := replace(definition, new_lock, old_lock);
        END IF;
        IF encode(sha256(convert_to(definition, 'UTF8')), 'hex') <> expected_sha256
           OR (length(definition) - length(replace(definition, old_lock, '')))
              / length(old_lock) <> 1
           OR (length(definition) - length(replace(definition,
                  'pg_advisory_xact_lock(', '')))
              / length('pg_advisory_xact_lock(') <> 1 THEN
            RAISE EXCEPTION 'lexical lock definition differs for %', signature;
        END IF;
        IF NOT already_applied THEN
            EXECUTE replace(definition, old_lock, new_lock);
        END IF;
    END LOOP;
END
$source_scoped_lexical_lock$;
COMMIT;
