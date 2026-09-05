-- Migration 049: avoid preparing a reuse candidate for a missing symbol key.
-- Complete card reuse remains authoritative only after the full 047 lookup.
-- This existence probe is a cheap negative guard, never an acceptance shortcut.

DO $$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_put_structural_card_bundle(bigint,bigint,bigint,text,text,text,text,text,text,text,jsonb,jsonb,text,bytea,jsonb,jsonb,jsonb)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT := $old$       AND p_field_provenance = '{}'::JSONB THEN$old$;
    v_new TEXT := $new$       AND p_field_provenance = '{}'::JSONB
       AND EXISTS (
           SELECT 1 FROM storage_v2_symbol existing_symbol
            WHERE existing_symbol.source_id = p_source_id
              AND existing_symbol.symbol_key = p_symbol_key
       ) THEN$new$;
BEGIN
    IF strpos(v_definition, v_new) > 0 THEN
        RETURN;
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old, '')))
       / length(v_old) <> 1 THEN
        RAISE EXCEPTION 'storage-v2 structural-card reuse guard differs from the reviewed definition';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
