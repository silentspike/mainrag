-- Reuse complete generation metadata. This stores no text, postings or scores.
-- Source revisions invalidate publication atomically with metadata mutations.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE required RECORD; routine RECORD;
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'generation metadata installation requires database administrator authority';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
         'e0097690ecda8b7ca22bff70eca8b8fdc526cc0f592dc5f44c3ae1f60fcddfdc',TRUE),
        ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)',
         '3f9a6c88fa373569a86d5d4e1c98945648283f9d1c20255607ce904d55da577c',FALSE),
        ('storage_v2_verify_generation(bigint,text)',
         '9f7c19194ccfe1486029cf03d3de2dfcb4f9f47acc262917a2ecd24afedc0b1d',TRUE),
        ('storage_v2_requalify_generation(bigint,text)',
         '3995d359f32666a79e160578c44eb003188ba939e56796d47e4485e3ba44af49',TRUE)
    ) expected(signature,definition_sha256,public_execute) LOOP
        SELECT * INTO STRICT routine FROM pg_proc WHERE oid=required.signature::REGPROCEDURE;
        IF routine.proowner<>'mainrag'::REGROLE OR NOT routine.prosecdef
           OR encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')
                <>required.definition_sha256 THEN
            RAISE EXCEPTION 'generation metadata reader definition differs';
        END IF;
        IF EXISTS (SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
            WHERE (a.grantee<>routine.proowner AND NOT (required.public_execute AND a.grantee=0::OID))
               OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>routine.proowner))
           OR NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
                WHERE a.grantee=routine.proowner AND a.privilege_type='EXECUTE')
           OR (required.public_execute AND NOT EXISTS(SELECT 1
                FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
                WHERE a.grantee=0::OID AND a.privilege_type='EXECUTE')) THEN
            RAISE EXCEPTION 'generation metadata reader authority differs';
        END IF;
    END LOOP;
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mainrag_v2_metadata_reader')
       OR to_regclass('storage_v2_reader_metadata_header') IS NOT NULL
       OR to_regclass('storage_v2_reader_metadata_epoch') IS NOT NULL THEN
        RAISE EXCEPTION 'generation metadata namespace already exists';
    END IF;
    -- Invalidation must probe existing indexed metadata, not scan every source
    -- after each newly published binding or occurrence.
    IF NOT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        WHERE i.indrelid='occurrence'::REGCLASS AND c.relname='idx_occurrence_view'
          AND i.indisvalid AND i.indisready AND pg_get_indexdef(i.indexrelid)=
            'CREATE INDEX idx_occurrence_view ON public.occurrence USING btree (view_id, source_id)')
       OR NOT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        WHERE i.indrelid='storage_v2_search_view_document'::REGCLASS
          AND c.relname='idx_storage_v2_search_view_document_document'
          AND i.indisvalid AND i.indisready AND pg_get_indexdef(i.indexrelid)=
            'CREATE INDEX idx_storage_v2_search_view_document_document ON public.storage_v2_search_view_document USING btree (document_id, view_id)') THEN
        RAISE EXCEPTION 'generation metadata invalidation indexes differ';
    END IF;
END $guard$;

CREATE TABLE storage_v2_reader_metadata_epoch (
    source_id BIGINT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    revision BIGINT NOT NULL CHECK(revision>=0)
);
CREATE TABLE storage_v2_reader_metadata_header (
    generation_id BIGINT PRIMARY KEY REFERENCES source_generation(id) ON DELETE CASCADE,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    generation_seq BIGINT NOT NULL,
    item_count BIGINT NOT NULL CHECK(item_count>=0),
    sealed_at TIMESTAMPTZ NOT NULL,
    revision BIGINT NOT NULL CHECK(revision>=0),
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    missing_documents BOOLEAN NOT NULL DEFAULT TRUE,
    binding_count BIGINT NOT NULL DEFAULT 0 CHECK(binding_count>=0),
    view_count BIGINT NOT NULL DEFAULT 0 CHECK(view_count>=0)
);
CREATE TABLE storage_v2_reader_metadata_binding (
    generation_id BIGINT NOT NULL REFERENCES storage_v2_reader_metadata_header(generation_id) ON DELETE CASCADE,
    occurrence_id BIGINT NOT NULL,
    component_ordinal BIGINT NOT NULL CHECK(component_ordinal>=0),
    document_id BIGINT NOT NULL,
    role_weight DOUBLE PRECISION NOT NULL,
    token_count BIGINT NOT NULL CHECK(token_count>=0),
    PRIMARY KEY(generation_id,occurrence_id,component_ordinal)
);
CREATE TABLE storage_v2_reader_metadata_view (
    generation_id BIGINT NOT NULL REFERENCES storage_v2_reader_metadata_header(generation_id) ON DELETE CASCADE,
    occurrence_id BIGINT NOT NULL,
    view_length DOUBLE PRECISION NOT NULL CHECK(view_length>=0),
    PRIMARY KEY(generation_id,occurrence_id)
);

CREATE ROLE mainrag_v2_metadata_reader NOLOGIN NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOINHERIT NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO mainrag_v2_metadata_reader;
GRANT SELECT(id,is_admin) ON users TO mainrag_v2_metadata_reader;
GRANT SELECT ON sources TO mainrag_v2_metadata_reader;
GRANT SELECT(id,source_id,generation_seq,status,item_count,sealed_at)
    ON source_generation TO mainrag_v2_metadata_reader;
DO $tables$
DECLARE relation REGCLASS;
BEGIN
    FOREACH relation IN ARRAY ARRAY['storage_v2_reader_metadata_epoch'::REGCLASS,
        'storage_v2_reader_metadata_header'::REGCLASS,'storage_v2_reader_metadata_binding'::REGCLASS,
        'storage_v2_reader_metadata_view'::REGCLASS] LOOP
        EXECUTE format('REVOKE ALL ON %s FROM PUBLIC,mainrag',relation);
        EXECUTE format('GRANT SELECT ON %s TO mainrag_v2_metadata_reader',relation);
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY',relation);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',relation);
        EXECUTE format('CREATE POLICY metadata_reader ON %s FOR SELECT TO mainrag_v2_metadata_reader USING(TRUE)',relation);
    END LOOP;
END $tables$;

CREATE FUNCTION storage_v2_invalidate_reader_metadata() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE affected BIGINT[];
BEGIN
    IF TG_OP='TRUNCATE' THEN
        UPDATE public.storage_v2_reader_metadata_epoch SET revision=revision+1;
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME IN ('occurrence','generation_item_version','artifact_version') THEN
        IF TG_OP='INSERT' THEN
            SELECT array_agg(DISTINCT source_id) INTO affected FROM storage_v2_reader_new;
        ELSIF TG_OP='DELETE' THEN
            SELECT array_agg(DISTINCT source_id) INTO affected FROM storage_v2_reader_old;
        ELSE
            SELECT array_agg(DISTINCT source_id) INTO affected FROM (
                SELECT source_id FROM storage_v2_reader_new UNION SELECT source_id FROM storage_v2_reader_old
            ) changed;
        END IF;
    ELSIF TG_TABLE_NAME='storage_v2_search_view_document' THEN
        IF TG_OP='INSERT' THEN
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o
             JOIN (SELECT DISTINCT view_id FROM storage_v2_reader_new) changed ON changed.view_id=o.view_id;
        ELSIF TG_OP='DELETE' THEN
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o
             JOIN (SELECT DISTINCT view_id FROM storage_v2_reader_old) changed ON changed.view_id=o.view_id;
        ELSE
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o JOIN (
                SELECT view_id FROM storage_v2_reader_new UNION SELECT view_id FROM storage_v2_reader_old
            ) changed ON changed.view_id=o.view_id;
        END IF;
    ELSIF TG_TABLE_NAME='storage_v2_search_document' THEN
        IF TG_OP='INSERT' THEN RETURN NULL;
        ELSIF TG_OP='DELETE' THEN
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o
             JOIN public.storage_v2_search_view_document binding ON binding.view_id=o.view_id
             JOIN storage_v2_reader_old changed ON changed.id=binding.document_id;
        ELSE
            SELECT array_agg(DISTINCT o.source_id) INTO affected FROM public.occurrence o
             JOIN public.storage_v2_search_view_document binding ON binding.view_id=o.view_id JOIN (
                SELECT id FROM storage_v2_reader_new UNION SELECT id FROM storage_v2_reader_old
             ) changed ON changed.id=binding.document_id;
        END IF;
    ELSE
        RAISE EXCEPTION 'unknown generation metadata invalidation relation';
    END IF;
    INSERT INTO public.storage_v2_reader_metadata_epoch(source_id,revision)
    SELECT source.id,1 FROM public.sources source WHERE source.id=ANY(affected) ORDER BY source.id
    ON CONFLICT(source_id) DO UPDATE SET revision=storage_v2_reader_metadata_epoch.revision+1;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION storage_v2_invalidate_reader_metadata() FROM PUBLIC;
DO $triggers$
DECLARE relation REGCLASS;
BEGIN
    FOREACH relation IN ARRAY ARRAY['occurrence'::REGCLASS,'generation_item_version'::REGCLASS,'artifact_version'::REGCLASS,
        'storage_v2_search_document'::REGCLASS,'storage_v2_search_view_document'::REGCLASS] LOOP
        EXECUTE format('CREATE TRIGGER storage_v2_reader_metadata_insert AFTER INSERT ON %s '
            'REFERENCING NEW TABLE AS storage_v2_reader_new FOR EACH STATEMENT '
            'EXECUTE FUNCTION storage_v2_invalidate_reader_metadata()',relation);
        EXECUTE format('CREATE TRIGGER storage_v2_reader_metadata_update AFTER UPDATE ON %s '
            'REFERENCING OLD TABLE AS storage_v2_reader_old NEW TABLE AS storage_v2_reader_new '
            'FOR EACH STATEMENT EXECUTE FUNCTION storage_v2_invalidate_reader_metadata()',relation);
        EXECUTE format('CREATE TRIGGER storage_v2_reader_metadata_delete AFTER DELETE ON %s '
            'REFERENCING OLD TABLE AS storage_v2_reader_old FOR EACH STATEMENT '
            'EXECUTE FUNCTION storage_v2_invalidate_reader_metadata()',relation);
        EXECUTE format('CREATE TRIGGER storage_v2_reader_metadata_truncate AFTER TRUNCATE ON %s '
            'FOR EACH STATEMENT EXECUTE FUNCTION storage_v2_invalidate_reader_metadata()',relation);
    END LOOP;
END $triggers$;

CREATE FUNCTION storage_v2_reader_metadata_ready(p_generation_ids BIGINT[]) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE requested BIGINT; generation RECORD; header RECORD;
BEGIN
    IF p_generation_ids IS NULL OR array_position(p_generation_ids,NULL) IS NOT NULL THEN RETURN FALSE; END IF;
    FOR requested IN SELECT DISTINCT id FROM unnest(p_generation_ids) input(id) LOOP
        SELECT id,source_id,generation_seq,status,item_count,sealed_at INTO generation
          FROM public.source_generation WHERE id=requested;
        IF NOT FOUND OR public.storage_v2_can_access_source(generation.source_id,'read') IS DISTINCT FROM TRUE
           OR generation.status NOT IN ('sealed','verified','release_candidate','active','superseded') THEN RETURN FALSE; END IF;
        SELECT h.* INTO header FROM public.storage_v2_reader_metadata_header h
          JOIN public.storage_v2_reader_metadata_epoch epoch
            ON epoch.source_id=h.source_id AND epoch.revision=h.revision
         WHERE h.generation_id=requested AND h.complete;
        IF NOT FOUND OR (header.source_id,header.generation_seq,header.item_count,header.sealed_at)
            IS DISTINCT FROM (generation.source_id,generation.generation_seq,generation.item_count,generation.sealed_at)
           OR header.binding_count<>(SELECT count(*) FROM public.storage_v2_reader_metadata_binding WHERE generation_id=requested)
           OR header.view_count<>(SELECT count(*) FROM public.storage_v2_reader_metadata_view WHERE generation_id=requested) THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
END $$;

CREATE FUNCTION storage_v2_reader_metadata_status(p_generation_ids BIGINT[])
RETURNS TABLE(ready BOOLEAN,missing_documents BOOLEAN)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
BEGIN
    ready:=public.storage_v2_reader_metadata_ready(p_generation_ids);
    missing_documents:=TRUE;
    IF ready THEN
        -- Completeness, authorization and all revision/identity checks above
        -- and this header read share the same command snapshot.
        SELECT COALESCE(bool_or(header.missing_documents),FALSE) INTO missing_documents
          FROM public.storage_v2_reader_metadata_header header
         WHERE header.generation_id=ANY(p_generation_ids);
    END IF;
    RETURN NEXT;
END $$;

CREATE FUNCTION storage_v2_reader_metadata_bindings(p_generation_ids BIGINT[])
RETURNS TABLE(occurrence_id BIGINT,component_ordinal BIGINT,document_id BIGINT,
              role_weight DOUBLE PRECISION,token_count BIGINT)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
BEGIN
    IF public.storage_v2_reader_metadata_ready(p_generation_ids) IS DISTINCT FROM TRUE THEN RETURN; END IF;
    RETURN QUERY SELECT row.occurrence_id,row.component_ordinal,row.document_id,row.role_weight,row.token_count
      FROM public.storage_v2_reader_metadata_binding row WHERE row.generation_id=ANY(p_generation_ids);
END $$;
CREATE FUNCTION storage_v2_reader_metadata_views(p_generation_ids BIGINT[])
RETURNS TABLE(occurrence_id BIGINT,view_length DOUBLE PRECISION)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
BEGIN
    IF public.storage_v2_reader_metadata_ready(p_generation_ids) IS DISTINCT FROM TRUE THEN RETURN; END IF;
    RETURN QUERY SELECT row.occurrence_id,row.view_length FROM public.storage_v2_reader_metadata_view row
      WHERE row.generation_id=ANY(p_generation_ids);
END $$;
ALTER FUNCTION storage_v2_reader_metadata_ready(BIGINT[]) OWNER TO mainrag_v2_metadata_reader;
ALTER FUNCTION storage_v2_reader_metadata_status(BIGINT[]) OWNER TO mainrag_v2_metadata_reader;
ALTER FUNCTION storage_v2_reader_metadata_bindings(BIGINT[]) OWNER TO mainrag_v2_metadata_reader;
ALTER FUNCTION storage_v2_reader_metadata_views(BIGINT[]) OWNER TO mainrag_v2_metadata_reader;
REVOKE ALL ON FUNCTION storage_v2_reader_metadata_ready(BIGINT[]),
    storage_v2_reader_metadata_status(BIGINT[]),
    storage_v2_reader_metadata_bindings(BIGINT[]),storage_v2_reader_metadata_views(BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_reader_metadata_ready(BIGINT[]),
    storage_v2_reader_metadata_status(BIGINT[]),
    storage_v2_reader_metadata_bindings(BIGINT[]),storage_v2_reader_metadata_views(BIGINT[]) TO mainrag;

CREATE FUNCTION storage_v2_materialize_reader_metadata(p_generation_id BIGINT)
RETURNS TABLE(binding_count BIGINT,view_count BIGINT,reused BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE generation public.source_generation; revision BIGINT;
BEGIN
    -- Share the lifecycle writer's generation-before-source lock order.
    SELECT * INTO generation FROM public.source_generation WHERE id=p_generation_id FOR SHARE;
    IF NOT FOUND OR public.storage_v2_can_access_source(generation.source_id,'write') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'generation metadata materialization requires source write authority' USING ERRCODE='42501';
    END IF;
    IF generation.status NOT IN ('sealed','verified','release_candidate','active','superseded')
       OR generation.item_count IS NULL OR generation.sealed_at IS NULL THEN
        RAISE EXCEPTION 'sealed generation metadata required';
    END IF;
    INSERT INTO public.storage_v2_reader_metadata_epoch(source_id,revision)
      VALUES(generation.source_id,0) ON CONFLICT DO NOTHING;
    SELECT epoch.revision INTO revision FROM public.storage_v2_reader_metadata_epoch epoch
      WHERE epoch.source_id=generation.source_id FOR UPDATE;
    IF public.storage_v2_reader_metadata_ready(ARRAY[p_generation_id]) THEN
        RETURN QUERY SELECT h.binding_count,h.view_count,TRUE
          FROM public.storage_v2_reader_metadata_header h WHERE h.generation_id=p_generation_id;
        RETURN;
    END IF;
    DELETE FROM public.storage_v2_reader_metadata_header WHERE generation_id=p_generation_id;
    INSERT INTO public.storage_v2_reader_metadata_header
        (generation_id,source_id,generation_seq,item_count,sealed_at,revision)
    VALUES(generation.id,generation.source_id,generation.generation_seq,generation.item_count,generation.sealed_at,revision);
    INSERT INTO public.storage_v2_reader_metadata_binding
    SELECT generation.id,o.id,b.ordinal,b.document_id,b.role_weight,d.token_count
      FROM public.occurrence o JOIN public.generation_item_version membership
        ON membership.source_id=generation.source_id AND membership.artifact_version_id=o.artifact_version_id
      JOIN (SELECT view_id,ordinal,document_id,role_weight FROM public.storage_v2_search_view_document OFFSET 0) b
        ON b.view_id=o.view_id
      JOIN (SELECT id,token_count FROM public.storage_v2_search_document OFFSET 0) d ON d.id=b.document_id
     WHERE o.source_id=generation.source_id AND membership.valid_from_seq<=generation.generation_seq
       AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>generation.generation_seq);
    GET DIAGNOSTICS binding_count=ROW_COUNT;
    INSERT INTO public.storage_v2_reader_metadata_view
    SELECT p_generation_id,row.occurrence_id,SUM(row.token_count)::DOUBLE PRECISION
      FROM public.storage_v2_reader_metadata_binding row WHERE row.generation_id=p_generation_id GROUP BY row.occurrence_id;
    GET DIAGNOSTICS view_count=ROW_COUNT;
    UPDATE public.storage_v2_reader_metadata_header h SET complete=TRUE,
        binding_count=storage_v2_materialize_reader_metadata.binding_count,
        view_count=storage_v2_materialize_reader_metadata.view_count,
        missing_documents=EXISTS (
            SELECT 1 FROM public.occurrence o JOIN public.artifact_version artifact
              ON artifact.id=o.artifact_version_id
            JOIN public.generation_item_version membership
              ON membership.source_id=generation.source_id
             AND membership.source_item_id=artifact.item_id AND membership.artifact_version_id=artifact.id
            WHERE o.source_id=generation.source_id AND membership.valid_from_seq<=generation.generation_seq
              AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>generation.generation_seq)
              AND NOT EXISTS(SELECT 1 FROM public.storage_v2_search_view_document binding WHERE binding.view_id=o.view_id)
        ) WHERE h.generation_id=p_generation_id;
    reused:=FALSE; RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION storage_v2_materialize_reader_metadata(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_materialize_reader_metadata(BIGINT) TO mainrag;

DO $reader$
DECLARE signature TEXT; definition TEXT; first_boundary INTEGER; last_boundary INTEGER;
        scope TEXT; cached_start TEXT;
        missing_start TEXT:=$missing$    IF EXISTS (
        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id$missing$;
        old_views TEXT := $old$    view_stats AS (
        SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length
          FROM scoped_binding GROUP BY occurrence_id
    ),$old$;
        new_views TEXT := $new$    view_stats AS (
        SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length
          FROM scoped_binding WHERE v_metadata_generation_ids IS NULL GROUP BY occurrence_id
        UNION ALL
        SELECT occurrence_id,view_length FROM storage_v2_reader_metadata_views(v_metadata_generation_ids)
    ),$new$;
BEGIN
    FOREACH signature IN ARRAY ARRAY['storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,old_views,'')))/length(old_views)<>1
           OR (length(definition)-length(replace(definition,'    v_batch_metadata BOOLEAN := FALSE;','')))
                /length('    v_batch_metadata BOOLEAN := FALSE;')<>1 THEN
            RAISE EXCEPTION 'generation metadata reader boundary differs';
        END IF;
        first_boundary:=strpos(definition,'    scoped_binding AS MATERIALIZED (');
        last_boundary:=strpos(definition,'    view_stats AS (');
        scope:=substr(definition,first_boundary,last_boundary-first_boundary);
        IF first_boundary=0 OR last_boundary<=first_boundary
           OR strpos(scope,'WHERE NOT v_batch_metadata')=0 OR strpos(scope,'WHERE v_batch_metadata')=0 THEN
            RAISE EXCEPTION 'generation metadata binding boundary differs';
        END IF;
        scope:=replace(scope,'WHERE NOT v_batch_metadata','WHERE NOT v_batch_metadata AND v_metadata_generation_ids IS NULL');
        scope:=replace(scope,'WHERE v_batch_metadata','WHERE v_batch_metadata AND v_metadata_generation_ids IS NULL');
        scope:=left(scope,length(scope)-length(E'    ),\n'))||$cached$
        UNION ALL
        SELECT occurrence_id,component_ordinal,document_id,role_weight,token_count
          FROM storage_v2_reader_metadata_bindings(v_metadata_generation_ids)
    ),
$cached$;
        definition:=left(definition,first_boundary-1)||scope||substr(definition,last_boundary);
        definition:=replace(definition,'    v_batch_metadata BOOLEAN := FALSE;',
            E'    v_batch_metadata BOOLEAN := FALSE;\n    v_metadata_generation_ids BIGINT[];\n    v_metadata_ready BOOLEAN;\n    v_missing_documents BOOLEAN;');
        definition:=replace(definition,old_views,new_views);
        IF signature LIKE 'storage_v2_search_exact(%' THEN
            cached_start:=$exact$    IF NOT (p_filters ?| ARRAY['path_prefix','role','occurred_from','occurred_to']) THEN
        SELECT ready,missing_documents INTO v_metadata_ready,v_missing_documents
          FROM storage_v2_reader_metadata_status(ARRAY[v_generation.id]);
        IF v_metadata_ready THEN v_metadata_generation_ids:=ARRAY[v_generation.id]; END IF;
    END IF;
$exact$;
        ELSE
            cached_start:=$active$    IF NOT (p_filters ?| ARRAY['path_prefix','role','occurred_from','occurred_to']) THEN
        SELECT COALESCE(array_agg(generation.id),ARRAY[]::BIGINT[]) INTO v_metadata_generation_ids
          FROM sources source JOIN logical_source pointer ON pointer.id=source.id
          JOIN source_generation generation ON generation.id=pointer.active_generation_id
           AND generation.source_id=source.id AND generation.status='active'
         WHERE (p_source_id IS NULL OR source.id=p_source_id)
           AND storage_v2_can_access_source(source.id,'read')
           AND (p_include_test OR NOT source.is_test);
        SELECT ready,missing_documents INTO v_metadata_ready,v_missing_documents
          FROM storage_v2_reader_metadata_status(v_metadata_generation_ids);
        IF NOT v_metadata_ready THEN
            v_metadata_generation_ids:=NULL;
        END IF;
    END IF;
$active$;
        END IF;
        IF (length(definition)-length(replace(definition,'    WITH RECURSIVE','')))/length('    WITH RECURSIVE')<>1 THEN
            RAISE EXCEPTION 'generation metadata request boundary differs';
        END IF;
        definition:=replace(definition,'    WITH RECURSIVE',cached_start||E'\n    WITH RECURSIVE');
        IF (length(definition)-length(replace(definition,missing_start,'')))/length(missing_start)<>1
           OR (length(definition)-length(replace(definition,'    RETURN v_result;','')))
                /length('    RETURN v_result;')<>1 THEN
            RAISE EXCEPTION 'generation metadata missing-document boundary differs';
        END IF;
        definition:=replace(definition,missing_start,
            E'    IF v_metadata_generation_ids IS NOT NULL THEN\n        IF v_missing_documents THEN\n'
            ||E'            RAISE EXCEPTION ''required lexical search document missing'';\n        END IF;\n    ELSE\n'
            ||missing_start);
        definition:=replace(definition,'    RETURN v_result;',E'    END IF;\n    RETURN v_result;');
        EXECUTE definition;
    END LOOP;
END $reader$;

-- Regular verification and requalification publish the same derived metadata;
-- existing candidates can call the idempotent materializer without rebuilding.
DO $lifecycle$
DECLARE signature TEXT; definition TEXT;
BEGIN
    FOREACH signature IN ARRAY ARRAY['storage_v2_verify_generation(bigint,text)',
        'storage_v2_requalify_generation(bigint,text)'] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,'    RETURN v_generation;','')))
            /length('    RETURN v_generation;')<>1 THEN
            RAISE EXCEPTION 'generation metadata lifecycle boundary differs';
        END IF;
        EXECUTE replace(definition,'    RETURN v_generation;',
            E'    PERFORM public.storage_v2_materialize_reader_metadata(p_generation_id);\n    RETURN v_generation;');
    END LOOP;
END $lifecycle$;
COMMIT;
