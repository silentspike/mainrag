-- Stage packed postings at the existing derived lexical write boundary.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_catalog_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $stage_inverted_on_ingest$
DECLARE
    declaration CONSTANT TEXT := '    v_byte_lengths INTEGER[];';
    derived_branch CONSTANT TEXT := '        SELECT array_agg(octet_length(value) ORDER BY ordinal) INTO v_byte_lengths';
    verified_block CONSTANT TEXT := $verified$               OR (v_existing_derived.first_terms IS NOT NULL
                   AND (v_existing_derived.first_terms,v_existing_derived.first_term_ordinals)
                       IS DISTINCT FROM (v_first_terms,v_first_ordinals)) THEN
                RAISE EXCEPTION 'lexical segment identity collision';
            END IF;$verified$;
    stage_block CONSTANT TEXT := verified_block||$stage$
            IF v_stage_inverted THEN
                PERFORM public.storage_v2_stage_inverted_projection(v_existing_derived, v_vectors[v_low:v_high]);
            END IF;$stage$;
    dependencies CONSTANT TEXT[] := ARRAY[
        'storage_v2_inverted_source_enabled(bigint)',
        'storage_v2_stage_inverted_projection(storage_v2_derived_lexical_block,tsvector[])',
        'storage_v2_set_inverted_source_enabled(bigint,boolean)',
        'storage_v2_pack_inverted_projection(tsvector[],integer[])'
    ];
    relations CONSTANT REGCLASS[] := ARRAY[
        'public.storage_v2_inverted_source'::REGCLASS,
        'public.storage_v2_inverted_projection'::REGCLASS,
        'public.storage_v2_inverted_binding'::REGCLASS
    ];
    definition TEXT;
    metadata JSONB;
    installed BYTEA;
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
    marker TEXT;
    routine RECORD;
    writer RECORD;
    target_relation REGCLASS;
BEGIN
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;
    FOR writer IN SELECT e.signature,decode(e.definition_sha256,'hex') AS predecessor FROM (VALUES
        ('storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])',
         '0e690d0167d23893c8d701097cb4dd1ac046eda19c455a25a7a83616558d7b78'),
        ('storage_v2_put_lexical_segments_document_private(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer)',
         '04aa8a7d581e5dfb32854e9c09f2a88cc6fef3a2e98a135a5dc259d5d6f33995')
    ) AS e(signature,definition_sha256) LOOP
        SELECT pg_get_functiondef(p.oid), to_jsonb(p)-'prosrc'
          INTO STRICT definition, metadata
          FROM pg_proc p
         WHERE p.oid=writer.signature::REGPROCEDURE AND p.proowner='mainrag_v2_frontier_owner'::REGROLE;
        IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM writer.predecessor THEN
            RAISE EXCEPTION 'inverted ingest writer predecessor differs: %',writer.signature;
        END IF;
        FOREACH marker IN ARRAY ARRAY[declaration,derived_branch,verified_block] LOOP
            IF length(definition)-length(replace(definition,marker,''))<>length(marker) THEN
                RAISE EXCEPTION 'inverted ingest writer boundary differs: %',writer.signature;
            END IF;
        END LOOP;
        definition := replace(definition,declaration,declaration||E'\n    v_stage_inverted BOOLEAN;');
        definition := replace(definition,derived_branch,
            E'        v_stage_inverted := public.storage_v2_inverted_source_enabled(v_source_id);\n'||derived_branch);
        EXECUTE replace(definition,verified_block,stage_block);
        SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
          FROM pg_proc p WHERE p.oid=writer.signature::REGPROCEDURE AND to_jsonb(p)-'prosrc'=metadata;
        UPDATE storage_v2_lexical_conversion_function_contract c SET definition_sha256=installed
         WHERE c.signature=writer.signature AND c.definition_sha256=writer.predecessor;
        IF NOT FOUND THEN RAISE EXCEPTION 'inverted ingest writer dependency differs: %',writer.signature; END IF;
    END LOOP;

    FOREACH marker IN ARRAY dependencies LOOP
        SELECT p.oid,p.proowner,p.proacl INTO STRICT routine FROM pg_proc p WHERE p.oid=marker::REGPROCEDURE;
        INSERT INTO storage_v2_lexical_conversion_function_contract(signature,definition_sha256,owner_oid,acl_sha256)
        VALUES (marker,sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),routine.proowner,
                sha256(convert_to(coalesce(routine.proacl::TEXT,''),'UTF8')));
    END LOOP;
    FOREACH target_relation IN ARRAY relations LOOP
        INSERT INTO storage_v2_lexical_conversion_catalog_contract(relation_oid,identity_sha256)
        VALUES (target_relation,storage_v2_posting_conversion_relation_sha256(target_relation));
    END LOOP;

    IF NOT EXISTS(SELECT 1 FROM pg_trigger
                  WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
                    AND tgname='storage_v2_lexical_conversion_seal_immutable'
                    AND tgenabled='O' AND tgtype=27
                    AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'inverted ingest requires the active lexical seal guard';
    END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE storage_v2_lexical_conversion_contract_seal s
       SET function_count=(SELECT count(*) FROM storage_v2_lexical_conversion_function_contract),
           catalog_count=(SELECT count(*) FROM storage_v2_lexical_conversion_catalog_contract),
           contract_sha256=sha256(convert_to(jsonb_build_object(
               'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                              FROM storage_v2_lexical_conversion_function_contract c),
               'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                              FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s)=to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted ingest seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $stage_inverted_on_ingest$;
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
