-- Lock the protected managed frontier through its dedicated table owner.
-- The prefix copier retains the ordinary ingest owner and all its complete
-- source, generation, prefix, analysis and immutable reuse checks.
BEGIN;

DO $authority$
BEGIN
    IF (SELECT relowner<>'mainrag_v2_frontier_owner'::REGROLE
          FROM pg_class WHERE oid='storage_v2_managed_append_frontier'::REGCLASS)
       OR (SELECT proowner<>'mainrag'::REGROLE FROM pg_proc WHERE oid=
           'storage_v2_copy_managed_append_prefix(bigint,bigint,text[],bytea[],bigint[])'::REGPROCEDURE)
       OR pg_has_role('mainrag','mainrag_v2_frontier_owner','MEMBER') THEN
        RAISE EXCEPTION 'managed append owner boundary differs';
    END IF;
END
$authority$;

CREATE OR REPLACE FUNCTION storage_v2_lock_managed_append_frontier(
    p_source_id BIGINT,p_adapter_profile_id TEXT
) RETURNS SETOF storage_v2_managed_append_frontier
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp
SET row_security=on
AS $$
BEGIN
    IF NOT public.storage_v2_can_access_source(p_source_id,'write')
       OR p_adapter_profile_id NOT LIKE 'mainrag.managed-append-%.v2.manifest'
       OR NOT EXISTS (SELECT 1 FROM public.sources
                       WHERE id=p_source_id AND type='managed_append') THEN
        RAISE EXCEPTION 'authorized managed append frontier required'
            USING ERRCODE='42501';
    END IF;
    RETURN QUERY
    SELECT frontier.* FROM public.storage_v2_managed_append_frontier frontier
     WHERE frontier.source_id=p_source_id
       AND frontier.adapter_profile_id=p_adapter_profile_id
     FOR UPDATE;
END
$$;
ALTER FUNCTION storage_v2_lock_managed_append_frontier(BIGINT,TEXT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_lock_managed_append_frontier(BIGINT,TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_lock_managed_append_frontier(BIGINT,TEXT)
    TO mainrag;

DO $copier$
DECLARE
    definition TEXT;
    old TEXT := $old$    SELECT * INTO v_frontier FROM storage_v2_managed_append_frontier
     WHERE source_id = v_run.source_id AND adapter_profile_id = v_run.adapter_profile_id
     FOR UPDATE;$old$;
    replacement TEXT := $new$    SELECT * INTO v_frontier
      FROM public.storage_v2_lock_managed_append_frontier(
          v_run.source_id,v_run.adapter_profile_id
      );$new$;
BEGIN
    definition:=pg_get_functiondef(
        'storage_v2_copy_managed_append_prefix(bigint,bigint,text[],bytea[],bigint[])'::REGPROCEDURE
    );
    IF strpos(definition,replacement)>0 AND strpos(definition,old)=0 THEN RETURN; END IF;
    IF (length(definition)-length(replace(definition,old,'')))/length(old)<>1
       OR strpos(definition,replacement)>0 THEN
        RAISE EXCEPTION 'managed append prefix copier definition differs';
    END IF;
    EXECUTE replace(definition,old,replacement);
END
$copier$;

COMMIT;
