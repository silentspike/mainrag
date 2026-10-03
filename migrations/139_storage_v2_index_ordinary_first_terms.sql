-- Exact first-position metadata for ordinary vectors; full readers remain the
-- fallback until bounded materialization covers the complete requested set.
BEGIN;

DO $guard$
DECLARE routine RECORD;
BEGIN
    SELECT * INTO STRICT routine FROM pg_proc
     WHERE oid='storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE;
    IF encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')
       <>'0d903fc40825a41877fef8c07f3d433513fc73968cf696c1367d84dee60e90ea' THEN
        RAISE EXCEPTION 'ordinary first-term reader definition differs';
    END IF;
    IF routine.proowner<>'mainrag_v2_lexical_rank_owner'::REGROLE OR NOT routine.prosecdef
       OR NOT has_function_privilege('mainrag_v2_frontier_owner',routine.oid,'EXECUTE')
       OR EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
          WHERE permission.grantee NOT IN ('mainrag_v2_lexical_rank_owner'::REGROLE,
                                          'mainrag_v2_frontier_owner'::REGROLE)
             OR permission.privilege_type<>'EXECUTE') THEN
        RAISE EXCEPTION 'ordinary first-term reader authority differs';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_trigger
        WHERE tgrelid='storage_v2_lexical_segment'::REGCLASS
          AND tgname='storage_v2_lexical_segment_immutable' AND tgenabled='O'
          AND tgtype=27 AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE)
       OR NOT EXISTS(SELECT 1 FROM pg_trigger
        WHERE tgrelid='storage_v2_lexical_segment'::REGCLASS
          AND tgname='storage_v2_flat_lexical_identity' AND tgenabled='O'
          AND tgtype=7 AND tgfoid='storage_v2_guard_flat_lexical_insert()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'ordinary first-term vector immutability differs';
    END IF;
END $guard$;

CREATE TABLE storage_v2_ordinary_first_coverage (
    occurrence_id BIGINT PRIMARY KEY REFERENCES occurrence(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    segment_count BIGINT NOT NULL CHECK(segment_count>=0),
    term_count BIGINT NOT NULL CHECK(term_count>=0),
    FOREIGN KEY(occurrence_id,source_id,artifact_version_id)
        REFERENCES occurrence(id,source_id,artifact_version_id) ON DELETE RESTRICT
);
CREATE TABLE storage_v2_ordinary_first_term (
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    lexeme TEXT NOT NULL CHECK(lexeme<>''),
    segment_order BIGINT NOT NULL CHECK(segment_order>=0),
    PRIMARY KEY(occurrence_id,lexeme),
    FOREIGN KEY(occurrence_id,source_id,artifact_version_id)
        REFERENCES occurrence(id,source_id,artifact_version_id) ON DELETE RESTRICT
);
CREATE INDEX storage_v2_ordinary_first_term_lookup
    ON storage_v2_ordinary_first_term(lexeme,occurrence_id) INCLUDE(segment_order,source_id,artifact_version_id);
REVOKE ALL ON storage_v2_ordinary_first_coverage,storage_v2_ordinary_first_term FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_ordinary_first_coverage,storage_v2_ordinary_first_term TO mainrag_v2_lexical_rank_owner;
ALTER TABLE storage_v2_ordinary_first_coverage ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_ordinary_first_coverage FORCE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_ordinary_first_term ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_ordinary_first_term FORCE ROW LEVEL SECURITY;
CREATE POLICY ordinary_first_coverage_read ON storage_v2_ordinary_first_coverage FOR SELECT
    USING(storage_v2_can_access_source(source_id,'read'));
CREATE POLICY ordinary_first_term_read ON storage_v2_ordinary_first_term FOR SELECT
    USING(storage_v2_can_access_source(source_id,'read'));

CREATE FUNCTION storage_v2_invalidate_ordinary_first_terms() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
    -- Existing BEFORE INSERT identity guards serialize each occurrence with
    -- the same advisory lock used by the materializer. Invalidate atomically
    -- with publication; unchanged ON CONFLICT inserts do not invalidate.
    DELETE FROM storage_v2_ordinary_first_term projection
     WHERE projection.occurrence_id IN(SELECT added.occurrence_id FROM storage_v2_new_ordinary_segments added);
    DELETE FROM storage_v2_ordinary_first_coverage projection
     WHERE projection.occurrence_id IN(SELECT added.occurrence_id FROM storage_v2_new_ordinary_segments added);
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION storage_v2_invalidate_ordinary_first_terms() FROM PUBLIC;
CREATE TRIGGER storage_v2_ordinary_first_invalidate AFTER INSERT ON storage_v2_lexical_segment
    REFERENCING NEW TABLE AS storage_v2_new_ordinary_segments FOR EACH STATEMENT
    EXECUTE FUNCTION storage_v2_invalidate_ordinary_first_terms();

CREATE FUNCTION storage_v2_materialize_ordinary_first_terms(
    p_source_id BIGINT,p_after_id BIGINT,p_limit INTEGER
) RETURNS TABLE(scanned BIGINT,materialized BIGINT,last_occurrence_id BIGINT,inserted_terms BIGINT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE candidate RECORD; locked RECORD; term_rows BIGINT; segments BIGINT;
BEGIN
    IF storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'ordinary first-term materialization requires administrator authority' USING ERRCODE='42501';
    END IF;
    IF p_source_id IS NULL OR p_after_id IS NULL OR p_after_id<0
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'bounded ordinary first-term cursor required';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM sources WHERE id=p_source_id) THEN
        RAISE EXCEPTION 'ordinary first-term source does not exist';
    END IF;
    scanned:=0; materialized:=0; last_occurrence_id:=p_after_id; inserted_terms:=0;
    FOR candidate IN SELECT id FROM occurrence WHERE source_id=p_source_id AND id>p_after_id
         ORDER BY id LIMIT p_limit LOOP
        -- Match the established writer lock order: advisory lock before the
        -- FK parent row. The call holds no more than 128 occurrence locks.
        PERFORM pg_advisory_xact_lock(hashtextextended('mainrag.lexical-segment:'||candidate.id::TEXT,0));
        SELECT * INTO STRICT locked FROM occurrence WHERE id=candidate.id FOR UPDATE;
        scanned:=scanned+1; last_occurrence_id:=locked.id;
        IF EXISTS(SELECT 1 FROM storage_v2_ordinary_first_coverage WHERE occurrence_id=locked.id) THEN
            CONTINUE;
        END IF;
        SELECT count(*) INTO segments FROM storage_v2_lexical_segment WHERE occurrence_id=locked.id;
        INSERT INTO storage_v2_ordinary_first_term(occurrence_id,source_id,artifact_version_id,lexeme,segment_order)
        SELECT locked.id,locked.source_id,locked.artifact_version_id,item.lexeme,min(segment.segment_order)
          FROM storage_v2_lexical_segment segment CROSS JOIN LATERAL unnest(segment.fts_vector) item
         WHERE segment.occurrence_id=locked.id GROUP BY item.lexeme;
        GET DIAGNOSTICS term_rows=ROW_COUNT;
        INSERT INTO storage_v2_ordinary_first_coverage
            VALUES(locked.id,locked.source_id,locked.artifact_version_id,segments,term_rows);
        materialized:=materialized+1; inserted_terms:=inserted_terms+term_rows;
    END LOOP;
    RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION storage_v2_materialize_ordinary_first_terms(BIGINT,BIGINT,INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_materialize_ordinary_first_terms(BIGINT,BIGINT,INTEGER) TO mainrag;

DO $reader$
DECLARE definition TEXT; marker TEXT:=E'    IF v_has_ordinary THEN\n    RETURN QUERY';
    ending TEXT:=E'    END IF;\n\n    IF v_has_compact THEN';
    replacement TEXT:=$indexed$    IF v_has_ordinary THEN
    IF v_query::TEXT=quote_literal(lower(p_query)) AND EXISTS(
        SELECT 1 FROM public.storage_v2_ordinary_first_coverage coverage
         WHERE coverage.occurrence_id=ANY(p_occurrence_ids) AND coverage.source_id=ANY(v_source_ids)) THEN
        RETURN QUERY
        WITH requested AS MATERIALIZED (
            SELECT DISTINCT id FROM unnest(p_occurrence_ids) input(id) WHERE id IS NOT NULL
        ), covered AS MATERIALIZED (
            SELECT coverage.occurrence_id FROM public.storage_v2_ordinary_first_coverage coverage
             WHERE coverage.source_id=ANY(v_source_ids)
               AND coverage.occurrence_id=ANY(p_occurrence_ids)
        ), uncovered AS MATERIALIZED (
            SELECT requested.id FROM requested
             WHERE NOT EXISTS(SELECT 1 FROM covered WHERE covered.occurrence_id=requested.id)
        ), indexed AS (
            SELECT projection.occurrence_id,projection.source_id,projection.artifact_version_id,
                   projection.segment_order
          FROM public.storage_v2_ordinary_first_term projection
         WHERE projection.lexeme=lower(p_query) AND projection.source_id=ANY(v_source_ids)
               AND projection.occurrence_id=ANY(ARRAY(SELECT covered.occurrence_id FROM covered))
        ), fallback AS (
            SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
                   min(segment.segment_order) segment_order
              FROM public.storage_v2_lexical_segment segment
             WHERE EXISTS(SELECT 1 FROM uncovered)
               AND segment.source_id=ANY(v_source_ids) AND segment.fts_vector@@v_query
               AND segment.occurrence_id=ANY(ARRAY(SELECT uncovered.id FROM uncovered))
             GROUP BY segment.occurrence_id,segment.source_id,segment.artifact_version_id
        )
        SELECT indexed.*,0.0::REAL FROM indexed
        UNION ALL SELECT fallback.*,0.0::REAL FROM fallback;
    ELSE
    RETURN QUERY$indexed$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE);
    IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1
       OR (length(definition)-length(replace(definition,ending,'')))/length(ending)<>1 THEN
        RAISE EXCEPTION 'ordinary first-term reader boundary differs';
    END IF;
    definition:=replace(definition,marker,replacement);
    definition:=replace(definition,ending,E'    END IF;\n'||ending);
    EXECUTE definition;
END $reader$;

COMMENT ON TABLE storage_v2_ordinary_first_coverage IS
    'Complete ordinary-vector projection at publication; future INSERT atomically invalidates coverage and terms.';
COMMENT ON FUNCTION storage_v2_materialize_ordinary_first_terms(BIGINT,BIGINT,INTEGER) IS
    'Bounded exact projection; preserve full vector fallback until coverage is complete.';
COMMIT;
