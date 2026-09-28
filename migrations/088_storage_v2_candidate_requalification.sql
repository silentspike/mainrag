-- Migration 088: requalify the same immutable generation without a rebuild.
-- Keep the current qualification slot and retain previous accepted envelopes.
CREATE TABLE IF NOT EXISTS storage_v2_release_candidate_evidence_history (
    id UUID PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    generation_id BIGINT NOT NULL REFERENCES source_generation(id) ON DELETE RESTRICT,
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    superseded_by UUID NOT NULL,
    superseded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE storage_v2_release_candidate_evidence_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS storage_v2_candidate_history_read
    ON storage_v2_release_candidate_evidence_history;
CREATE POLICY storage_v2_candidate_history_read
    ON storage_v2_release_candidate_evidence_history FOR SELECT
    USING (storage_v2_can_access_source(source_id,'read'));
REVOKE ALL ON storage_v2_release_candidate_evidence_history FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_release_candidate_evidence_history TO mainrag;

CREATE OR REPLACE FUNCTION storage_v2_archive_candidate_qualification()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public
AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'candidate qualification history is immutable';
    END IF;
    IF (NEW.source_id,NEW.generation_id,NEW.commit_sha,
        NEW.source_watermark_sha256,NEW.adapter_profile_id,
        NEW.analysis_profile_id,NEW.search_profile_id)
       IS DISTINCT FROM
       (OLD.source_id,OLD.generation_id,OLD.commit_sha,
        OLD.source_watermark_sha256,OLD.adapter_profile_id,
        OLD.analysis_profile_id,OLD.search_profile_id) THEN
        RAISE EXCEPTION 'candidate requalification cannot replace build identity';
    END IF;
    IF NEW.id=OLD.id THEN
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'candidate qualification identity collision';
        END IF;
        RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM storage_v2_release_candidate_evidence_history
                WHERE id=NEW.id) THEN
        RAISE EXCEPTION 'historical qualification identity cannot be reused';
    END IF;
    INSERT INTO storage_v2_release_candidate_evidence_history(
        id,source_id,generation_id,evidence,superseded_by)
    VALUES(OLD.id,OLD.source_id,OLD.generation_id,to_jsonb(OLD),NEW.id);
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION storage_v2_archive_candidate_qualification() FROM PUBLIC;
DROP TRIGGER IF EXISTS storage_v2_archive_candidate_qualification
    ON storage_v2_release_candidate_evidence;
CREATE TRIGGER storage_v2_archive_candidate_qualification
    BEFORE UPDATE OR DELETE ON storage_v2_release_candidate_evidence
    FOR EACH ROW EXECUTE FUNCTION storage_v2_archive_candidate_qualification();

CREATE OR REPLACE FUNCTION storage_v2_candidate_history_immutable()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path=pg_catalog,public
AS $$ BEGIN RAISE EXCEPTION 'candidate qualification history is immutable'; END $$;
REVOKE ALL ON FUNCTION storage_v2_candidate_history_immutable() FROM PUBLIC;
DROP TRIGGER IF EXISTS storage_v2_candidate_history_immutable
    ON storage_v2_release_candidate_evidence_history;
CREATE TRIGGER storage_v2_candidate_history_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_release_candidate_evidence_history
    FOR EACH ROW EXECUTE FUNCTION storage_v2_candidate_history_immutable();

DO $migration$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_record_dual_read_evidence(uuid,bigint,bigint,text,text,text,jsonb)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT := 'AND status = ''verified''';
    v_new TEXT := 'AND status IN (''verified'', ''release_candidate'')';
BEGIN
    IF strpos(v_definition,v_new)>0 AND strpos(v_definition,v_old)=0 THEN RETURN; END IF;
    IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
        RAISE EXCEPTION 'dual-read generation eligibility definition differs';
    END IF;
    EXECUTE replace(v_definition,v_old,v_new);
END
$migration$;

DO $migration$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_qualify_release_candidate(uuid,bigint,bigint,text,text,text,text,text,jsonb)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT := 'ON CONFLICT (source_id, generation_id) DO NOTHING';
    v_new TEXT := $new$ON CONFLICT (source_id, generation_id) DO UPDATE
      SET id=EXCLUDED.id, manifest=EXCLUDED.manifest,
          manifest_sha256=EXCLUDED.manifest_sha256,
          created_at=CASE WHEN storage_v2_release_candidate_evidence.id=EXCLUDED.id
                          THEN storage_v2_release_candidate_evidence.created_at
                          ELSE EXCLUDED.created_at END
      WHERE (storage_v2_release_candidate_evidence.commit_sha,
             storage_v2_release_candidate_evidence.source_watermark_sha256,
             storage_v2_release_candidate_evidence.adapter_profile_id,
             storage_v2_release_candidate_evidence.analysis_profile_id,
             storage_v2_release_candidate_evidence.search_profile_id)
        IS NOT DISTINCT FROM
            (EXCLUDED.commit_sha,EXCLUDED.source_watermark_sha256,
             EXCLUDED.adapter_profile_id,EXCLUDED.analysis_profile_id,EXCLUDED.search_profile_id)
        AND (storage_v2_release_candidate_evidence.id<>EXCLUDED.id OR
             storage_v2_release_candidate_evidence.manifest_sha256=EXCLUDED.manifest_sha256)$new$;
BEGIN
    IF strpos(v_definition,v_new)>0 AND strpos(v_definition,v_old)=0 THEN RETURN; END IF;
    IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1 THEN
        RAISE EXCEPTION 'candidate qualification conflict definition differs';
    END IF;
    EXECUTE replace(v_definition,v_old,v_new);
END
$migration$;
