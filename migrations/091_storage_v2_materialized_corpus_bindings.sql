-- Reuse narrow corpus bindings across exact scoring branches and avoid JIT
-- compilation for the bounded interactive read path. Full corpus statistics,
-- authorization, scoring, tie boundaries and external identities are unchanged.
DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := '    scoped_binding AS (';
    v_new TEXT := '    scoped_binding AS MATERIALIZED (';
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF strpos(v_definition,v_new)>0 AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'corpus binding definition differs before materialization';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;
ALTER FUNCTION storage_v2_search_exact(BIGINT,TEXT,JSONB,JSONB,BIGINT) SET jit TO off;
ALTER FUNCTION storage_v2_search_active_unchecked(TEXT,JSONB,JSONB,BIGINT,BIGINT,BOOLEAN) SET jit TO off;
