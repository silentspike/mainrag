-- Preserve immutable comparison versions and copied-byte lexical provenance.
-- Source generations, build identities, scoring and acceptance thresholds stay
-- unchanged. All changes are transactional and replayable.
BEGIN;

DO $migration$
DECLARE
    v_old_key SMALLINT[];
    v_new_key SMALLINT[];
    v_constraint NAME;
    v_old_count INTEGER;
    v_new_count INTEGER;
BEGIN
    SELECT array_agg(attribute.attnum ORDER BY input.ordinal)
      INTO v_old_key
      FROM unnest(ARRAY['source_id','generation_id','commit_sha',
                       'fixture_sha256','query_set_sha256']) WITH ORDINALITY input(name,ordinal)
      JOIN pg_attribute attribute ON attribute.attrelid='storage_v2_dual_read_evidence'::REGCLASS
       AND attribute.attname=input.name AND NOT attribute.attisdropped;
    SELECT v_old_key || attribute.attnum INTO v_new_key
      FROM pg_attribute attribute
     WHERE attribute.attrelid='storage_v2_dual_read_evidence'::REGCLASS
       AND attribute.attname='artifact_sha256' AND NOT attribute.attisdropped;
    SELECT count(*),min(conname) INTO v_old_count,v_constraint FROM pg_constraint
     WHERE conrelid='storage_v2_dual_read_evidence'::REGCLASS
       AND contype='u' AND conkey=v_old_key;
    SELECT count(*) INTO v_new_count FROM pg_constraint
     WHERE conrelid='storage_v2_dual_read_evidence'::REGCLASS
       AND contype='u' AND conkey=v_new_key;
    IF v_old_count=0 AND v_new_count=1 THEN RETURN; END IF;
    IF cardinality(v_old_key)<>5 OR cardinality(v_new_key)<>6
       OR v_old_count<>1 OR v_new_count<>0 THEN
        RAISE EXCEPTION 'dual-read artifact identity constraint differs';
    END IF;
    ALTER TABLE storage_v2_dual_read_evidence ADD CONSTRAINT storage_v2_dual_read_artifact_identity
        UNIQUE(source_id,generation_id,commit_sha,fixture_sha256,query_set_sha256,artifact_sha256);
    EXECUTE format('ALTER TABLE storage_v2_dual_read_evidence DROP CONSTRAINT %I',v_constraint);
END
$migration$;

DO $migration$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_record_dual_read_evidence(uuid,bigint,bigint,text,text,text,jsonb)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_declaration TEXT := E'    v_evidence storage_v2_dual_read_evidence;\n';
    v_old TEXT := $old$    v_hash := digest(convert_to(p_artifact::TEXT, 'UTF8'), 'sha256');
    INSERT INTO storage_v2_dual_read_evidence(
        id, source_id, generation_id, commit_sha, fixture_sha256,
        query_set_sha256, artifact, artifact_sha256
    ) VALUES (
        p_id, p_source_id, p_generation_id, p_commit_sha, p_fixture_sha256,
        p_query_set_sha256, p_artifact, v_hash
    ) ON CONFLICT (source_id, generation_id, commit_sha, fixture_sha256, query_set_sha256)
      DO NOTHING
    RETURNING * INTO v_evidence;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_evidence FROM storage_v2_dual_read_evidence
         WHERE source_id = p_source_id AND generation_id = p_generation_id
           AND commit_sha = p_commit_sha AND fixture_sha256 = p_fixture_sha256
           AND query_set_sha256 = p_query_set_sha256;
        IF v_evidence.artifact_sha256 <> v_hash THEN
            RAISE EXCEPTION 'dual-read evidence identity collision' USING ERRCODE = '22000';
        END IF;
    END IF;
$old$;
    v_new TEXT := $new$    v_hash := digest(convert_to(p_artifact::TEXT, 'UTF8'), 'sha256');
    v_identity := jsonb_build_array('mainrag.storage-v2.dual-read-artifact.v1',
        p_source_id,p_generation_id,p_commit_sha,p_fixture_sha256,p_query_set_sha256)::TEXT;
    -- Serialize only one comparison identity, including the first insertion.
    PERFORM pg_advisory_xact_lock(hashtextextended(v_identity,0));
    IF EXISTS (SELECT 1 FROM storage_v2_dual_read_evidence
                WHERE id=p_id AND (source_id,generation_id,commit_sha,
                      fixture_sha256,query_set_sha256) IS DISTINCT FROM
                     (p_source_id,p_generation_id,p_commit_sha,
                      p_fixture_sha256,p_query_set_sha256)) THEN
        RAISE EXCEPTION 'dual-read evidence identity collision' USING ERRCODE='22000';
    END IF;
    SELECT * INTO v_evidence FROM storage_v2_dual_read_evidence
     WHERE source_id=p_source_id AND generation_id=p_generation_id
       AND commit_sha=p_commit_sha AND fixture_sha256=p_fixture_sha256
       AND query_set_sha256=p_query_set_sha256 AND artifact_sha256=v_hash;
    IF FOUND THEN RETURN v_evidence; END IF;
    v_id := p_id;
    IF EXISTS (SELECT 1 FROM storage_v2_dual_read_evidence
                WHERE source_id=p_source_id AND generation_id=p_generation_id
                  AND commit_sha=p_commit_sha AND fixture_sha256=p_fixture_sha256
                  AND query_set_sha256=p_query_set_sha256) THEN
        -- A SHA-256-derived UUIDv8 identifies a new artifact without replacing
        -- the caller's historical UUID or requiring another extension.
        v_uuid_hash := encode(digest(convert_to(v_identity||':'||encode(v_hash,'hex'),
                                                'UTF8'),'sha256'),'hex');
        v_id := (substring(v_uuid_hash,1,12)||'8'||substring(v_uuid_hash,14,3)
                 ||'8'||substring(v_uuid_hash,18,15))::UUID;
    END IF;
    INSERT INTO storage_v2_dual_read_evidence(
        id,source_id,generation_id,commit_sha,fixture_sha256,
        query_set_sha256,artifact,artifact_sha256)
    VALUES(v_id,p_source_id,p_generation_id,p_commit_sha,p_fixture_sha256,
           p_query_set_sha256,p_artifact,v_hash)
    ON CONFLICT(source_id,generation_id,commit_sha,fixture_sha256,query_set_sha256,artifact_sha256)
      DO NOTHING RETURNING * INTO v_evidence;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_evidence FROM storage_v2_dual_read_evidence
         WHERE source_id=p_source_id AND generation_id=p_generation_id
           AND commit_sha=p_commit_sha AND fixture_sha256=p_fixture_sha256
           AND query_set_sha256=p_query_set_sha256 AND artifact_sha256=v_hash;
    END IF;
$new$;
BEGIN
    IF strpos(v_definition,v_new)>0 AND strpos(v_definition,v_old)=0 THEN RETURN; END IF;
    IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
       OR (length(v_definition)-length(replace(v_definition,v_declaration,'')))/length(v_declaration)<>1 THEN
        RAISE EXCEPTION 'dual-read evidence writer definition differs';
    END IF;
    v_definition := replace(v_definition,v_declaration,v_declaration
        || E'    v_identity TEXT;\n    v_uuid_hash TEXT;\n    v_id UUID;\n');
    EXECUTE replace(v_definition,v_old,v_new);
END
$migration$;

CREATE OR REPLACE FUNCTION storage_v2_dual_read_artifact_immutable()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path=pg_catalog,public
AS $$ BEGIN RAISE EXCEPTION 'dual-read artifacts are immutable'; END $$;
REVOKE ALL ON FUNCTION storage_v2_dual_read_artifact_immutable() FROM PUBLIC;
DROP TRIGGER IF EXISTS storage_v2_dual_read_artifact_immutable ON storage_v2_dual_read_evidence;
CREATE TRIGGER storage_v2_dual_read_artifact_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_dual_read_evidence
    FOR EACH ROW EXECUTE FUNCTION storage_v2_dual_read_artifact_immutable();

-- A copied chunk can contain a complete parser token that the surrounding
-- document does not. Check its exact immutable byte-range provenance instead
-- of treating the document's different FTS tokenization as a missing term.
-- This is explicitly copied-projection support, not a whole-body FTS claim.
CREATE OR REPLACE FUNCTION storage_v2_source_legacy_segment_matches(
    p_occurrence_id BIGINT,p_query TEXT
) RETURNS BOOLEAN
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path=pg_catalog,public
SET row_security=on
AS $$
    SELECT EXISTS (
        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
        JOIN storage_v2_search_view_document binding
          ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
        JOIN storage_v2_search_document document ON document.id=binding.document_id
         AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
        JOIN storage_v2_lexical_segment segment
          ON segment.occurrence_id=occurrence_row.id
         AND segment.source_id=occurrence_row.source_id
         AND segment.artifact_version_id=artifact.id
        WHERE occurrence_row.id=p_occurrence_id
          AND storage_v2_can_access_source(occurrence_row.source_id,'read')
          AND segment.segment_order>0
          AND NOT EXISTS (SELECT 1 FROM storage_v2_lexical_segment generated
                           WHERE generated.occurrence_id=occurrence_row.id
                             AND generated.segment_order=0)
          AND segment.text_sha256=sha256(convert_to(substring(document.search_text
              FROM segment.text_start::INTEGER FOR segment.text_length::INTEGER),'UTF8'))
          AND segment.fts_vector@@websearch_to_tsquery('simple',p_query)
    )
$$;

DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := '     WHERE document.fts_simple @@ v_query;';
    v_new TEXT := E'     WHERE document.fts_simple @@ v_query\n'
        || E'        OR storage_v2_source_legacy_segment_matches(occurrence_row.id,p_query);';
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_source_segment_ranks(bigint[],text)',
        'storage_v2_source_segment_ranks_precise(bigint[],text)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF strpos(v_definition,v_new)>0 AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'copied segment body guard definition differs';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;

-- Read an overflowing term's complete scoped posting set in primary-key order.
-- Hash aggregate iteration previously scattered document probes across pages.
DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := '        SELECT DISTINCT document_id FROM scoped_binding';
    v_new TEXT := '        SELECT DISTINCT document_id FROM scoped_binding ORDER BY document_id';
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=1 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
            RAISE EXCEPTION 'scoped posting order definition differs';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;
COMMIT;
