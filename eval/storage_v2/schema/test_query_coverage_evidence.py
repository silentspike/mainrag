"""Read-only query support must bind immutable text, identities and source scope."""
from __future__ import annotations

import json

from eval.storage_v2.schema import test_shadow_ingest_schema as schema

COMMIT = "a" * 40
MIGRATION = schema.ROOT / "migrations/046_storage_v2_query_coverage_evidence.sql"


class QueryCoverageTests(schema.ShadowIngestSchemaTests):
    def fixture(self) -> tuple[int, dict[str, int], int]:
        if not hasattr(self.__class__, "coverage_fixture"):
            self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                     "(17,'coverage-fixture','fixture','coverage'),"
                     "(18,'coverage-foreign','fixture','coverage-foreign')")
            run = self.begin(17, "e1" * 32, "e2" * 32, commit_sha=COMMIT)
            for path, content, search in (("known.txt", "alpha visible", "alpha visible"),
                                           ("new.txt", "alpha visible", "alpha visible"),
                                           ("wrong.txt", "alpha", "alpha injected"),
                                           ("other.txt", "beta", "beta")):
                node, view, digest = self.make_projection(content)
                self.stage(run, path, content, node, view, digest)
                self.complete_analysis(digest)
                # The wrong projection gets its own profile-independent node,
                # so it represents a real materialization mismatch, not a reuse collision.
                document = self.sql(self.admin(
                    f"SELECT id FROM storage_v2_put_search_document('mainrag.lexical-simple.v1',"
                    f"'node',{node},'{search}',ARRAY[]::TEXT[])"))
                self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
            self.commit(run, 4)
            generation = int(self.sql(f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
            self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'e3' * 32}')"))
            ids = json.loads(self.sql("SELECT json_object_agg(source_path,id) FROM occurrence WHERE source_id=17"))
            legacy = int(self.sql("""
WITH file AS (
 INSERT INTO files(source_id,path,hash,content,content_text,size_original,size_compressed,last_modified)
 VALUES(17,'/synthetic/known.txt',digest('alpha visible','sha256'),'','alpha visible',13,0,now()) RETURNING id
)
INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,content_text,start_line,end_line)
SELECT id,'text',digest('alpha visible','sha256'),'','alpha visible',1,1 FROM file RETURNING id;
"""))
            self.__class__.coverage_fixture = generation, ids, legacy
        return self.__class__.coverage_fixture

    def statement(self, ids: list[int], legacy: list[int], query: str = "alpha", **changes) -> str:
        generation, _, _ = self.fixture()
        source = changes.get("source", 17)
        generation = changes.get("generation", generation)
        commit = changes.get("commit", COMMIT)
        candidate_sql = changes.get("candidate_sql", f"ARRAY{ids}::BIGINT[]")
        current_sql = f"ARRAY{legacy}::BIGINT[]"
        return (f"SELECT storage_v2_candidate_query_evidence({source},{generation},'{commit}',"
                f"'{query.replace(chr(39), chr(39)*2)}',{candidate_sql},{current_sql})")

    def test_read_only_support_distinguishes_added_document_and_binds_external_identity(self) -> None:
        generation, ids, legacy = self.fixture()
        requested = [ids["/synthetic/known.txt"], ids["/synthetic/new.txt"]]
        state_sql = "SELECT string_agg(id::text||':'||status,',' ORDER BY id) FROM source_generation; " \
                    "SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL; " \
                    "SELECT count(*) FROM storage_v2_release_candidate_evidence"
        before = self.sql(state_sql)
        result = json.loads(self.sql(self.admin(self.statement(requested, [legacy]))))
        self.assertEqual(result["generation_id"], generation)
        self.assertEqual(result["commit_sha"], COMMIT)
        self.assertEqual(len(result["candidate"]), 2)
        for hit in result["candidate"]:
            self.assertTrue(hit["body_text_matches"])
            self.assertEqual(hit["reference_frequency"], 1)
            self.assertEqual(hit["reference_frequency"], hit["posting_frequency"])
            # Search and evidence must derive the same public identity.
            actual = self.exact_search({"type": "term", "value": "alpha"}, source_id=17)
            matching = next(row for row in actual["results"] if row["occurrence_id"] == hit["occurrence_id"])
            self.assertEqual(hit["external_hit_id"], matching["external_hit_id"])
        self.assertEqual(sorted(row["chunk_count"] for row in result["legacy_paths"]), [0, 1])
        self.assertEqual(sorted(row["literal_matches"] for row in result["legacy_paths"]), [0, 1])
        self.assertNotIn("/synthetic/", json.dumps(result))
        self.assertNotIn("alpha visible", json.dumps(result))
        self.assertEqual(before, self.sql(state_sql))
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(result, json.loads(self.sql(self.admin(self.statement(requested, [legacy])))))

    def test_evidence_rejects_wrong_source_commit_hit_and_lexical_projection(self) -> None:
        _, ids, legacy = self.fixture()
        good = [ids["/synthetic/known.txt"]]
        cases = [
            (self.statement(good, [legacy], source=18), "verified candidate identity"),
            (self.statement(good, [legacy], commit="b" * 40), "verified candidate identity"),
            (self.statement([999999], []), "outside the supported"),
            (self.statement(good, [999999]), "outside the supported"),
            (self.statement([ids["/synthetic/wrong.txt"]], []), "lexical support"),
            (self.statement([ids["/synthetic/other.txt"]], []), "lexical support"),
            (self.statement([], [legacy], query="beta"), "lexical support"),
            (self.statement(good * 2, []), "bounded literal"),
            (self.statement(good, [legacy, legacy]), "bounded literal"),
            (self.statement(list(range(1, 12)), []), "bounded literal"),
            (self.statement(good, [], query="alpha OR beta"), "bounded literal"),
            (self.statement(good, [], candidate_sql="ARRAY[NULL]::BIGINT[]"), "bounded literal"),
            (self.statement(good, [], candidate_sql="NULL::BIGINT[]"), "bounded literal"),
        ]
        for statement, error in cases:
            with self.subTest(error=error):
                self.assert_sql_fails(self.admin(statement), error)
        self.assert_sql_fails(self.actor(schema.WRITER_ID, self.statement(good, [legacy])),
                              "administrator source authority")

    def test_literal_support_handles_unicode_boundaries_and_empty_hits(self) -> None:
        self.assertEqual(self.sql("SELECT storage_v2_literal_term_count("
                                  "'Alpha alpha_beta alphabeta ALPHA; äpfel', 'alpha')"), "2")
        # Token boundaries and lowercasing follow the database locale, exactly
        # as the lexical profile does. A no-locale test cluster is ASCII-only.
        expected = self.sql("SELECT count(*) FROM (VALUES ('Äpfel'),('äpfel')) word(value) "
                            "WHERE lower(value)='äpfel' AND value ~ '^[[:alnum:]_]+$'")
        self.assertEqual(self.sql("SELECT storage_v2_literal_term_count('Äpfel äpfel','äpfel')"), expected)
        self.assertEqual(self.sql("SELECT storage_v2_literal_term_count('a_b a b','a_b')"), "1")
        self.fixture()
        result = json.loads(self.sql(self.admin(self.statement([], [], query="no_match_fixture"))))
        self.assertEqual(result["candidate"], [])
        self.assertEqual(result["current"], [])
        self.assertEqual(result["legacy_paths"], [])

    def test_y_source_backed_postgres_lexeme_keeps_chunk_order_without_legacy_reads(self) -> None:
        # PostgreSQL splits underscores into lexemes while the original sparse
        # posting tokenizer keeps them in one token. Both file versions are
        # identical, so this is a projection gap rather than source drift.
        self.fixture()
        fallback_conjunction = {"type": "and", "children": [
            {"type": "term", "value": "alpha"},
            {"type": "term", "value": "visible"}]}
        fallback_before = self.exact_search(fallback_conjunction, source_id=17)
        self.assertGreater(fallback_before["total"], 0)
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(19,'lexical-fixture','fixture','lexical-fixture')")
        content = "foo_3d alpha"
        run = self.begin(19, "d1" * 32, "d2" * 32, commit_sha=COMMIT)
        node, view, digest = self.make_projection(content)
        self.stage(run, "lexeme.txt", content, node, view, digest)
        self.complete_analysis(digest)
        document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{node},'{content}',ARRAY[]::TEXT[])"))
        self.sql(self.admin(
            f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        self.commit(run, 1)
        generation = int(self.sql(
            f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
        self.sql(self.admin(
            f"SELECT storage_v2_verify_generation({generation},'{'d3' * 32}')"))
        occurrence, artifact = map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT "
            "FROM occurrence WHERE source_id=19").split(":"))
        legacy = int(self.sql("""
WITH file AS (
 INSERT INTO files(source_id,path,hash,content,content_text,
                   size_original,size_compressed,last_modified)
 VALUES(19,'/synthetic/lexeme.txt',digest('foo_3d alpha','sha256'),'',
        'foo_3d alpha',12,0,now()) RETURNING id
)
INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,
                   content_text,start_line,end_line)
SELECT id,'text',digest('foo_3d alpha','sha256'),'','foo_3d alpha',1,1
  FROM file RETURNING id;
"""))
        self.assertEqual(self.exact_search(
            {"type": "term", "value": "3d"}, source_id=19)["results"], [])
        self.assertEqual(self.sql(
            "SELECT storage_v2_literal_term_count('foo_3d','3d')"), "0")
        self.file(schema.ROOT / "migrations/066_storage_v2_controlled_frontier_owner.sql")
        self.file(schema.ROOT / "migrations/067_storage_v2_active_ingest_receipt_owner.sql")
        self.file(schema.ROOT / "migrations/068_storage_v2_lexical_segments.sql")
        # The inherited schema harness uses a dedicated fixture worker in
        # place of the production runtime role and owns the base tables as
        # the fixture user instead of mainrag.
        self.sql("GRANT SELECT ON occurrence, artifact_version, source_generation, "
                 "generation_item_version, "
                 "storage_v2_search_view_document, storage_v2_search_document, "
                 "files, chunks TO mainrag")
        self.sql("GRANT EXECUTE ON FUNCTION "
                 "storage_v2_copy_legacy_lexical_segments(BIGINT,BIGINT), "
                 "storage_v2_put_lexical_segment(BIGINT,BIGINT,BIGINT,TEXT,TEXT,TEXT), "
                 "storage_v2_verify_lexical_segments(BIGINT), "
                 "storage_v2_source_segment_rank(BIGINT,TEXT) "
                 "TO storage_v2_shadow_worker")
        copy = (f"SELECT storage_v2_copy_legacy_lexical_segments({occurrence},{artifact})")
        self.assertEqual(self.sql(self.admin(copy)), "1")
        self.assertEqual(self.sql(self.admin(copy)), "1", "copy must be idempotent")
        verification = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_verify_lexical_segments({generation})")))
        self.assertEqual(verification["occurrence_count"], 1)
        self.assertEqual(verification["segment_count"], 1)
        self.assertEqual(verification["invalid_count"], 0)
        result = self.exact_search({"type": "term", "value": "3d"}, source_id=19)
        self.assertEqual([row["occurrence_id"] for row in result["results"]], [occurrence])
        evidence = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_candidate_query_evidence(19,{generation},'{COMMIT}',"
            f"'3d',ARRAY[{occurrence}]::BIGINT[],ARRAY[{legacy}]::BIGINT[])")))
        self.assertEqual(evidence["schema_version"], "mainrag.storage-v2.query-coverage.v2")
        self.assertEqual(evidence["candidate"][0]["reference_frequency"], 0)
        self.assertTrue(evidence["candidate"][0]["fts_body_matches"])
        self.assertTrue(evidence["candidate"][0]["segment_matches"])
        self.file(schema.ROOT / "migrations/069_storage_v2_conjunctive_lexical_parity.sql")
        self.file(schema.ROOT / "migrations/070_storage_v2_lexical_segment_rls.sql")
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_has_lexical_segment(BIGINT) "
                 "TO storage_v2_shadow_worker")
        conjunction = {"type": "and", "children": [
            {"type": "term", "value": "foo"}, {"type": "term", "value": "alpha"}]}
        before_conjunction = self.exact_search(conjunction, source_id=19)
        before_term = self.exact_search({"type": "term", "value": "3d"}, source_id=19)
        self.assertEqual([row["occurrence_id"] for row in
                          before_conjunction["results"]], [occurrence])
        self.file(schema.ROOT / "migrations/071_storage_v2_set_based_segment_ranking.sql")
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) "
                 "TO storage_v2_shadow_worker")
        self.assertEqual(self.exact_search(conjunction, source_id=19), before_conjunction)
        self.assertEqual(self.exact_search(
            {"type": "term", "value": "3d"}, source_id=19), before_term)
        premature_presence = self.command(
            "--file", str(schema.ROOT / "migrations/073_storage_v2_set_based_segment_presence.sql"),
            check=False,
        )
        self.assertNotEqual(premature_presence.returncode, 0)
        self.assertIn("set authorization must precede set presence",
                      premature_presence.stderr)
        self.file(schema.ROOT / "migrations/072_storage_v2_segment_authorization_set.sql")
        self.assertEqual(self.exact_search(conjunction, source_id=19), before_conjunction)
        self.assertEqual(self.exact_search(
            {"type": "term", "value": "3d"}, source_id=19), before_term)
        self.file(schema.ROOT / "migrations/073_storage_v2_set_based_segment_presence.sql")
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_source_segment_presence(BIGINT[]) "
                 "TO storage_v2_shadow_worker")
        self.assertEqual(self.exact_search(conjunction, source_id=19), before_conjunction)
        self.assertEqual(self.exact_search(fallback_conjunction, source_id=17), fallback_before)
        self.assertEqual(self.sql(self.admin(
            f"SELECT occurrence_id FROM storage_v2_source_segment_presence("
            f"ARRAY[{occurrence},999999]::BIGINT[])")), str(occurrence))
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,
            f"SELECT count(*) FROM storage_v2_source_segment_presence("
            f"ARRAY[{occurrence}]::BIGINT[])")), "0")
        self.assertEqual(self.sql(self.admin(
            f"SELECT occurrence_id FROM storage_v2_source_segment_ranks("
            f"ARRAY[{occurrence},999999]::BIGINT[],'foo alpha')")), str(occurrence))
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,
            f"SELECT count(*) FROM storage_v2_source_segment_ranks("
            f"ARRAY[{occurrence}]::BIGINT[],'foo alpha')")), "0")
        self.assertEqual(self.sql(
            f"SET ROLE mainrag; SET app.user_id='{schema.OTHER_ID}'; "
            f"SELECT count(*) FROM storage_v2_lexical_segment "
            f"WHERE occurrence_id={occurrence}"), "0")
        self.assertEqual(self.sql(
            f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
            f"SELECT count(*) FROM storage_v2_lexical_segment "
            f"WHERE occurrence_id={occurrence}"), "1")
        for signature in (
            "storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)",
            "storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)",
        ):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
            self.assertIn("lexical_ranks AS MATERIALIZED", definition)
            self.assertIn("lexical_presence AS MATERIALIZED", definition)
            self.assertNotIn("LEFT JOIN LATERAL storage_v2_source_segment_rank(", definition)
            self.assertNotIn("NOT storage_v2_has_lexical_segment(matched.id)", definition)
        self.assertEqual(self.exact_search(
            {"type": "term", "value": "missing"}, source_id=19)["results"], [])
        self.assertEqual(self.exact_search(
            {"type": "and", "children": [
                {"type": "term", "value": "foo"},
                {"type": "term", "value": "missing"}]}, source_id=19)["results"], [])
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_has_lexical_segment({occurrence})")), "t")
        legacy_score = float(self.sql(
            f"SELECT ts_rank_cd(fts_vector,websearch_to_tsquery('simple','foo alpha')) "
            f"FROM chunks WHERE id={legacy}"))
        segment_score = float(self.sql(self.admin(
            f"SELECT score FROM storage_v2_source_segment_rank({occurrence},'foo alpha')")))
        self.assertAlmostEqual(segment_score, legacy_score, places=6)
        multiple = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_candidate_query_evidence(19,{generation},'{COMMIT}',"
            f"'foo alpha',ARRAY[{occurrence}]::BIGINT[],ARRAY[{legacy}]::BIGINT[])")))
        self.assertEqual(multiple["schema_version"], "mainrag.storage-v2.query-coverage.v3")
        self.assertTrue(multiple["candidate"][0]["fts_body_matches"])
        self.assertTrue(multiple["candidate"][0]["segment_matches"])
        self.assertEqual(self.sql("SELECT storage_v2_simple_and_query("
                                  "'{\"type\":\"or\",\"children\":["
                                  "{\"type\":\"term\",\"value\":\"foo\"},"
                                  "{\"type\":\"term\",\"value\":\"alpha\"}]}'::jsonb) "
                                  "IS NULL"), "t")
        self.assert_sql_fails(self.admin(
            f"SELECT storage_v2_candidate_query_evidence(19,{generation},'{COMMIT}',"
            "'foo OR alpha',ARRAY[]::BIGINT[],ARRAY[]::BIGINT[])"),
            "simple query")
        self.assert_sql_fails(self.admin(
            f"SELECT storage_v2_put_lexical_segment({occurrence},{artifact},"
            "999,'injected','','text')"), "absent from immutable source text")
        self.assert_sql_fails(
            f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
            "INSERT INTO storage_v2_lexical_segment "
            "(occurrence_id,source_id,artifact_version_id,segment_order,text_start,"
            "text_length,text_sha256,context_prefix,chunk_type,fts_vector) "
            f"VALUES({occurrence},19,{artifact},999,1,1,digest('x','sha256'),"
            "'','text',to_tsvector('simple','x'))",
            "permission denied",
        )

        # A chunk boundary can turn a substring into a lexeme even when the
        # complete source document has no such lexeme. The sealed slice and
        # current chunk agree; the whole-document FTS prefilter must not veto it.
        boundary_chunk = int(self.sql("""
INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,
                   content_text,context_prefix,start_line,end_line)
SELECT id,'text',digest('oo','sha256'),'','oo','oo oo',1,1
  FROM files WHERE source_id=19 AND path='/synthetic/lexeme.txt'
RETURNING id;
"""))
        self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segment({occurrence},{artifact},"
            f"{boundary_chunk},'oo','oo oo','text')"))
        self.assertEqual(self.sql(
            "SELECT to_tsvector('simple','foo_3d alpha') @@ "
            "websearch_to_tsquery('simple','oo')"), "f")
        self.assertEqual(self.exact_search(
            {"type": "term", "value": "oo"}, source_id=19)["results"], [])
        self.file(schema.ROOT / "migrations/074_storage_v2_source_segment_boundary_parity.sql")
        self.assertEqual(self.sql(
            "SELECT count(*) FROM storage_v2_lexical_segment WHERE rank_vector IS NULL"), "0")
        self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segment({occurrence},{artifact},"
            f"{boundary_chunk},'oo','oo oo','text')"))
        self.assertEqual(json.loads(self.sql(self.admin(
            f"SELECT storage_v2_verify_lexical_segments({generation})")))["invalid_count"], 0)
        boundary_result = self.exact_search(
            {"type": "term", "value": "oo"}, source_id=19)
        self.assertEqual([row["occurrence_id"] for row in boundary_result["results"]],
                         [occurrence])
        boundary_legacy_score = float(self.sql(
            "SELECT ts_rank_cd(fts_vector,websearch_to_tsquery('simple','oo')) "
            f"FROM chunks WHERE id={boundary_chunk}"))
        boundary_segment_score = float(self.sql(self.admin(
            f"SELECT score FROM storage_v2_source_segment_rank({occurrence},'oo')")))
        self.assertAlmostEqual(boundary_segment_score, boundary_legacy_score, places=6)
        boundary_evidence = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_candidate_query_evidence(19,{generation},'{COMMIT}',"
            f"'oo',ARRAY[{occurrence}]::BIGINT[],ARRAY[{boundary_chunk}]::BIGINT[])")))
        self.assertTrue(boundary_evidence["candidate"][0]["body_text_matches"])
        self.assertTrue(boundary_evidence["candidate"][0]["fts_body_matches"])
        self.assertTrue(boundary_evidence["candidate"][0]["segment_matches"])

        # The installed legacy vector also indexes context_prefix at weight B.
        # The body-only rank vector from 074 drops a valid two-term hit.
        self.file(schema.ROOT / "migrations/025_fts_context_prefix.sql")
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(20,'context-fixture','fixture','context-fixture')")
        context_body = "prefixonly alpha"
        context_run = self.begin(20, "f1" * 32, "f2" * 32, commit_sha=COMMIT)
        context_node, context_view, context_digest = self.make_projection(context_body)
        self.stage(context_run, "context.txt", context_body, context_node,
                   context_view, context_digest)
        self.complete_analysis(context_digest)
        context_document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{context_node},"
            f"'{context_body}',ARRAY[]::TEXT[])"))
        self.sql(self.admin(
            f"SELECT storage_v2_bind_search_document({context_view},0,"
            f"{context_document},1.0)"))
        self.commit(context_run, 1)
        context_generation = int(self.sql(
            f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={context_run}"))
        self.sql(self.admin(
            f"SELECT storage_v2_verify_generation({context_generation},'{'f3' * 32}')"))
        context_occurrence, context_artifact = map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT "
            "FROM occurrence WHERE source_id=20").split(":"))
        context_chunk = int(self.sql("""
WITH file AS (
 INSERT INTO files(source_id,path,hash,content,content_text,
                   size_original,size_compressed,last_modified)
 VALUES(20,'/synthetic/context.txt',digest('prefixonly alpha','sha256'),'',
        'prefixonly alpha',16,0,now()) RETURNING id
)
INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,
                   content_text,context_prefix,start_line,end_line)
SELECT id,'text',digest('alpha','sha256'),'','alpha','prefixonly',1,1
  FROM file RETURNING id;
"""))
        self.assertEqual(self.sql(
            f"SELECT fts_vector @@ websearch_to_tsquery('simple',"
            f"'prefixonly alpha') FROM chunks WHERE id={context_chunk}"), "t")
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_copy_legacy_lexical_segments("
            f"{context_occurrence},{context_artifact})")), "1")
        context_ast = {"type": "and", "children": [
            {"type": "term", "value": "prefixonly"},
            {"type": "term", "value": "alpha"}]}
        self.assertEqual(self.exact_search(context_ast, source_id=20)["results"], [])
        self.file(schema.ROOT / "migrations/075_storage_v2_installed_chunk_projection_parity.sql")
        self.assertEqual(self.sql(
            "SELECT count(*) FROM pg_attribute WHERE attrelid="
            "'storage_v2_lexical_segment'::REGCLASS "
            "AND attname='rank_vector' AND NOT attisdropped"), "0")
        self.assertEqual([row["occurrence_id"] for row in
                          self.exact_search(context_ast, source_id=20)["results"]],
                         [context_occurrence])
        self.assertEqual(json.loads(self.sql(self.admin(
            f"SELECT storage_v2_verify_lexical_segments("
            f"{context_generation})")))["invalid_count"], 0)
        context_evidence = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_candidate_query_evidence(20,{context_generation},"
            f"'{COMMIT}','prefixonly alpha',ARRAY[{context_occurrence}]::BIGINT[],"
            f"ARRAY[{context_chunk}]::BIGINT[])")))
        self.assertEqual(context_evidence["schema_version"],
                         "mainrag.storage-v2.query-coverage.v4")
        self.assertFalse(context_evidence["candidate"][0]["fts_body_matches"])
        self.assertTrue(context_evidence["candidate"][0]["segment_matches"])
        self.assertTrue(context_evidence["candidate"][0]["legacy_segment_matches"])

        # A new generated segment may match the same query. Its order-zero
        # marker must keep it behind the copied legacy chunk, even after the
        # legacy chunk row is removed from the fixture database.
        _, fixture_ids, _ = self.fixture()
        generated_occurrence = fixture_ids["/synthetic/new.txt"]
        generated_artifact = int(self.sql(
            "SELECT artifact_version_id FROM occurrence "
            f"WHERE id={generated_occurrence}"))
        self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segment({generated_occurrence},"
            f"{generated_artifact},0,'alpha visible','','text')"))
        self.file(schema.ROOT / "migrations/076_storage_v2_copied_segment_rank_tier.sql")
        rank_query = (
            "SELECT occurrence_id FROM storage_v2_source_segment_ranks("
            f"ARRAY[{generated_occurrence},{context_occurrence}]::BIGINT[],'alpha') "
            "ORDER BY score DESC")
        self.assertEqual(self.sql(self.admin(rank_query)).splitlines(),
                         [str(context_occurrence), str(generated_occurrence)])

        # The batched writer must preserve the single-row identity and source
        # authorization checks across idempotent and conflicting groups.
        self.file(schema.ROOT / "migrations/077_storage_v2_batched_lexical_segments.sql")
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segments("
                 "BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[]) "
                 "TO storage_v2_shadow_worker")
        batch = (f"SELECT storage_v2_put_lexical_segments({generated_occurrence},"
                 f"{generated_artifact},ARRAY[0,1]::BIGINT[],"
                 "ARRAY['alpha visible','visible']::TEXT[],"
                 "ARRAY['','']::TEXT[],ARRAY['text','text']::TEXT[])")
        self.assertEqual(self.sql(self.admin(batch)), "2")
        old_rows=self.sql(f"SELECT row_to_json(segment)::TEXT FROM storage_v2_lexical_segment segment WHERE occurrence_id={generated_occurrence} ORDER BY segment_order")
        self.file(schema.ROOT / "migrations/083_storage_v2_staging_projection_reuse.sql")
        self.assertEqual(self.sql(self.admin(batch)), "2")
        self.assertEqual(self.sql(f"SELECT row_to_json(segment)::TEXT FROM storage_v2_lexical_segment segment WHERE occurrence_id={generated_occurrence} ORDER BY segment_order"),old_rows)
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_lexical_segment "
                                  f"WHERE occurrence_id={generated_occurrence}"), "2")
        self.assert_sql_fails(self.admin(batch.replace("'visible']", "'alpha']")),
                              "lexical segment identity collision")
        self.assert_sql_fails(self.actor(schema.OTHER_ID, batch),
                              "authorized source-backed lexical segment required")
        self.assert_sql_fails(self.admin(batch.replace("ARRAY[0,1]", "ARRAY[0,0]")),
                              "valid source-backed lexical segment group required")
        self.assert_sql_fails(self.admin(batch.replace("ARRAY['alpha visible','visible']",
                                                       "ARRAY['alpha visible',NULL]")),
                              "valid source-backed lexical segment group required")
        self.assert_sql_fails(self.admin(batch.replace("ARRAY['alpha visible','visible']",
                                                       "ARRAY['alpha visible','missing']")),
                              "valid source-backed lexical segment group required")

        self.sql("GRANT SELECT ON storage_v2_lexical_segment TO storage_v2_shadow_worker")
        self.file(schema.ROOT / "migrations/085_storage_v2_positioned_lexical_segments.sql")
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segments_at(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[]) TO storage_v2_shadow_worker")
        positioned=batch.replace("storage_v2_put_lexical_segments(","storage_v2_put_lexical_segments_at(")[:-1]+",ARRAY[1,7]::BIGINT[])"
        self.assertEqual(self.sql(self.admin(positioned)),"2")
        self.assertEqual(self.sql(f"SELECT row_to_json(segment)::TEXT FROM storage_v2_lexical_segment segment WHERE occurrence_id={generated_occurrence} ORDER BY segment_order"),old_rows)
        self.assert_sql_fails(self.admin(positioned.replace("ARRAY[1,7]","ARRAY[1,6]")),"valid source-backed lexical segment group required")
        self.assert_sql_fails(self.admin(positioned.replace("ARRAY['','']","ARRAY['prefix','']")),"lexical segment identity collision")
        self.assert_sql_fails(self.actor(schema.OTHER_ID,positioned),"authorized source-backed lexical segment required")
        self.assert_sql_fails(self.admin(positioned.replace("ARRAY[1,7]","ARRAY[1,-1]")),"bounded source character window required")

        # Presence is a boolean support check. It must retain the authorized
        # source boundary, duplicate-input semantics, and search results while
        # avoiding a full segment join and DISTINCT over every segment row.
        before_presence = self.exact_search(
            {"type": "and", "children":[
                {"type": "term", "value": "foo"},
                {"type": "term", "value": "alpha"}]}, source_id=19)
        self.file(schema.ROOT / "migrations/078_storage_v2_short_circuit_segment_presence.sql")
        self.assertEqual(self.exact_search(
            {"type": "and", "children":[
                {"type": "term", "value": "foo"},
                {"type": "term", "value": "alpha"}]}, source_id=19),
            before_presence)
        self.assertEqual(self.sql(self.admin(
            f"SELECT occurrence_id FROM storage_v2_source_segment_presence("
            f"ARRAY[{occurrence},{occurrence},999999]::BIGINT[])")), str(occurrence))
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,
            f"SELECT count(*) FROM storage_v2_source_segment_presence("
            f"ARRAY[{occurrence}]::BIGINT[])")), "0")
        presence_definition = self.sql(
            "SELECT pg_get_functiondef("
            "'storage_v2_source_segment_presence(bigint[])'::regprocedure)")
        self.assertIn("WHERE EXISTS", presence_definition)
        self.assertNotIn("SELECT DISTINCT segment.occurrence_id", presence_definition)

        # Segment support remains a matching gate, while the established
        # posting score keeps the retrieval order stable for existing hits.
        before_rank_parity = self.exact_search(
            {"type": "term", "value": "foo"}, source_id=19)
        self.file(schema.ROOT / "migrations/079_storage_v2_lexical_rank_parity.sql")
        after_rank_parity = self.exact_search(
            {"type": "term", "value": "foo"}, source_id=19)
        self.assertEqual(
            [row["occurrence_id"] for row in after_rank_parity["results"]],
            [row["occurrence_id"] for row in before_rank_parity["results"]],
        )
        self.assertEqual(
            [row["content"] for row in after_rank_parity["results"]],
            [row["content"] for row in before_rank_parity["results"]],
        )
        for signature in (
            "storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)",
            "storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)",
        ):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
            self.assertIn("lexical_score + graph_score + semantic_score + rerank_score AS final_score",
                          definition)
            self.assertNotIn("1000000.0 + staged.segment_score", definition)

        # Fragmented source items can carry an extracted legacy chunk view
        # that is not a contiguous substring of the sealed fragment.  Keep a
        # source-bound immutable rank projection and use it only when the
        # candidate body itself also matches the query.
        self.file(schema.ROOT / "migrations/080_storage_v2_legacy_rank_projection.sql")
        after_legacy_rank_projection = self.exact_search(
            {"type": "term", "value": "foo"}, source_id=19)
        self.assertEqual(
            [row["occurrence_id"] for row in after_legacy_rank_projection["results"]],
            [row["occurrence_id"] for row in after_rank_parity["results"]],
        )
        rank_definition = self.sql(
            "SELECT pg_get_functiondef("
            "'storage_v2_source_segment_ranks(bigint[],text)'::regprocedure)")
        self.assertIn("storage_v2_legacy_lexical_segment", rank_definition)
        self.assertIn("document.fts_simple @@ query.value", rank_definition)
        for signature in (
            "storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)",
            "storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)",
        ):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
            self.assertIn("CASE WHEN staged.segment_score >= 1000000.0", definition)
            self.assertNotIn("1000000.0 + staged.segment_score", definition)

        known_occurrence = fixture_ids["/synthetic/known.txt"]
        known_artifact = int(self.sql("SELECT artifact_version_id FROM occurrence "
                                      f"WHERE id={known_occurrence}"))
        known_file = int(self.sql("SELECT id FROM files WHERE source_id=17 "
                                  "AND path='/synthetic/known.txt'"))
        self.sql("INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,"
                 "content_text,start_line,end_line) VALUES "
                 f"({known_file},'text',digest('alpha','sha256'),'','alpha',1,1)")
        copy_batch = (f"SELECT storage_v2_copy_legacy_lexical_segments("
                      f"{known_occurrence},{known_artifact})")
        self.assertEqual(self.sql(self.admin(copy_batch)), "2")
        self.assertEqual(self.sql(self.admin(copy_batch)), "2")
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_lexical_segment "
                                  f"WHERE occurrence_id={known_occurrence}"), "2")
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_legacy_lexical_segment "
                                  f"WHERE occurrence_id={known_occurrence}"), "2")
        self.file(schema.ROOT / "migrations/081_storage_v2_set_based_lexical_verification.sql")
        self.file(schema.ROOT / "migrations/082_storage_v2_windowed_lexical_verification.sql")
        verification_definition = self.sql(
            "SELECT pg_get_functiondef("
            "'storage_v2_verify_lexical_segments(bigint)'::regprocedure)")
        self.assertIn("segment_values AS MATERIALIZED", verification_definition)
        self.assertIn("segment_base AS MATERIALIZED", verification_definition)
        self.assertIn("document_chunks AS MATERIALIZED", verification_definition)
        self.assertIn("segment_checks AS", verification_definition)
        self.assertNotIn("LEFT JOIN LATERAL", verification_definition)

        # PostgreSQL substring offsets count characters, not UTF-8 bytes.
        # Check a multibyte slice spanning a window boundary and a slice in
        # the next window against the previous complete-document verifier.
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(21,'window-fixture','fixture','window-fixture')")
        window_body = " " * 61438 + "Über🙂 Grenze甲 " + " " * 5000 + "tail Ω"
        window_run = self.begin(21, "d1" * 32, "d2" * 32, commit_sha=COMMIT)
        projection = self.sql(self.admin("""
WITH content AS (
    SELECT repeat(' ',61438) AS first,
           'Über🙂 Grenze甲 ' || repeat(' ',5000) || 'tail Ω' AS second
), body AS (
    SELECT first_body.id AS first_id, second_body.id AS second_id,
           digest(convert_to(content.first||content.second,'UTF8'),'sha256') AS digest,
           octet_length(convert_to(content.first||content.second,'UTF8')) AS size
      FROM content CROSS JOIN LATERAL
           storage_v2_put_inline_body(convert_to(content.first,'UTF8')) first_body
      CROSS JOIN LATERAL
           storage_v2_put_inline_body(convert_to(content.second,'UTF8')) second_body
), leaves AS (
    SELECT first_node.id AS first_id, second_node.id AS second_id, body.digest, body.size
      FROM body CROSS JOIN LATERAL
           storage_v2_put_leaf_node('shadow-fixture','text',body.first_id) first_node
      CROSS JOIN LATERAL
           storage_v2_put_leaf_node('shadow-fixture','text',body.second_id) second_node
), node AS (
    SELECT root.id, leaves.digest, leaves.size
      FROM leaves CROSS JOIN LATERAL storage_v2_put_internal_node(
           'shadow-fixture','artifact-root',leaves.size,
           ARRAY['first','second'],ARRAY[leaves.first_id,leaves.second_id]) root
), view_row AS (
    SELECT view_value.id, node.id AS node_id, node.digest
      FROM node CROSS JOIN LATERAL storage_v2_put_retrieval_view(
           'chunk','fixture-view-v1','text','fixture-tokenizer-v1',0,
           ARRAY['content'],ARRAY['node'],ARRAY[node.id],ARRAY[0::BIGINT],
           ARRAY[node.size::BIGINT]) view_value
)
SELECT node_id || ':' || id || ':' || encode(digest,'hex') FROM view_row;
"""))
        window_node, window_view, window_digest = projection.split(":")
        self.stage(window_run, "window.txt", window_body, int(window_node),
                   int(window_view), window_digest)
        self.complete_analysis(window_digest)
        window_document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{window_node},"
            "repeat(' ',61438)||'Über🙂 Grenze甲 '||repeat(' ',5000)||'tail Ω',"
            "ARRAY[]::TEXT[])"))
        self.sql(self.admin(
            f"SELECT storage_v2_bind_search_document({window_view},0,{window_document},1.0)"))
        self.commit(window_run, 1)
        window_generation = int(self.sql(
            f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={window_run}"))
        self.sql(self.admin(
            f"SELECT storage_v2_verify_generation({window_generation},'{'d3' * 32}')"))
        window_occurrence, window_artifact = map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT "
            "FROM occurrence WHERE source_id=21").split(":"))
        for order, text in enumerate(("Über🙂 Grenze甲", "Grenze甲", "tail Ω")):
            self.sql(self.admin(
                f"SELECT storage_v2_put_lexical_segment({window_occurrence},"
                f"{window_artifact},{order},'{text}','context Ω','text')"))
        self.assertEqual(self.sql(
            "SELECT text_start FROM storage_v2_lexical_segment "
            f"WHERE occurrence_id={window_occurrence} AND segment_order=0"), "61439")
        self.file(schema.ROOT / "migrations/081_storage_v2_set_based_lexical_verification.sql")
        expected = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_verify_lexical_segments({window_generation})")))
        self.file(schema.ROOT / "migrations/082_storage_v2_windowed_lexical_verification.sql")
        verify_window = f"SELECT storage_v2_verify_lexical_segments({window_generation})"
        self.assertEqual(json.loads(self.sql(self.admin(verify_window))), expected)
        self.assertEqual(expected["segment_count"], 3)
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segments_at({window_occurrence},{window_artifact},ARRAY[0,1,2]::BIGINT[],"
            "ARRAY['Über🙂 Grenze甲','Grenze甲','tail Ω']::TEXT[],"
            "ARRAY['context Ω','context Ω','context Ω']::TEXT[],ARRAY['text','text','text']::TEXT[],"
            f"ARRAY(SELECT text_start FROM storage_v2_lexical_segment WHERE occurrence_id={window_occurrence} ORDER BY segment_order))")),"3")

        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segments({window_occurrence},{window_artifact},ARRAY[0,1,2]::BIGINT[],"
            "ARRAY['Über🙂 Grenze甲','Grenze甲','tail Ω']::TEXT[],"
            "ARRAY['context Ω','context Ω','context Ω']::TEXT[],ARRAY['text','text','text']::TEXT[])")), "3")
        self.assertEqual(json.loads(self.sql(self.admin(verify_window))),expected)

        # A segment larger than the normal window must enlarge its overlap;
        # no truncation or UTF-8 byte/character conversion is acceptable.
        self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segment({window_occurrence},"
            f"{window_artifact},3,repeat(' ',61438)||'Über🙂 Grenze甲 '"
            "||repeat(' ',5000)||'tail Ω','context Ω','text')"))
        self.assertEqual(json.loads(self.sql(self.admin(verify_window)))["segment_count"], 4)
        self.assert_sql_fails(self.actor(schema.OTHER_ID, verify_window),
                              "verified authorized generation required")

        rank_request=(f"SELECT COALESCE(jsonb_agg(ranked ORDER BY occurrence_id,score,segment_order),'[]'::JSONB) "
                      f"FROM storage_v2_source_segment_ranks(ARRAY[{window_occurrence},{context_occurrence},{known_occurrence},{generated_occurrence}]::BIGINT[],'alpha') ranked")
        previous_ranks=self.sql(self.admin(rank_request))
        self.file(schema.ROOT / "migrations/084_storage_v2_requested_segment_rank_kind.sql")
        self.assertEqual(self.sql(self.admin(rank_request)),previous_ranks)
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,rank_request)),"[]")
        self.file(schema.ROOT / "migrations/086_storage_v2_bounded_rank_projection.sql")
        self.assertEqual(self.sql(self.admin(rank_request)),previous_ranks)
        # Keep complete envelopes, all query classes and boundary ties exact.
        # The extra fixture has more equal-score/equal-sort-key views than k.
        from eval.storage_v2.schema.test_search_materialization import SearchMaterializationTests
        SearchMaterializationTests.make_search_fixture(self)
        term={"type":"term","value":"alpha"}
        phrase={"type":"phrase","value":"alpha beta"}
        cases=[term,phrase,{"type":"exact","value":"exact_key"},
               {"type":"term","value":"missing"},
               {"type":"and","children":[term,{"type":"term","value":"beta"}]},
               {"type":"and","children":[term,{"type":"not","children":[phrase]}]},
               {"type":"or","children":[term,phrase]}]
        requests=[]
        for source in (15,19):
            for ast in cases:
                for limit in (1,10,1000):
                    encoded=json.dumps(ast).replace("'","''")
                    requests.append(self.admin(
                        f"SELECT storage_v2_search_exact({source},'1','{encoded}'::JSONB,'{{}}'::JSONB,{limit})"))
        before=[self.sql(request) for request in requests]
        metadata="SELECT jsonb_agg(to_jsonb(p)-'prosrc' ORDER BY proname) FROM pg_proc p WHERE oid IN (" \
                 "'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure," \
                 "'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure," \
                 "'storage_v2_source_segment_ranks(bigint[],text)'::regprocedure)"
        authority=self.sql(metadata)
        self.file(schema.ROOT / "migrations/087_storage_v2_scoped_search_scaling.sql")
        self.assertEqual([self.sql(request) for request in requests],before)
        self.assertEqual(self.sql(self.admin(rank_request)),previous_ranks)
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,rank_request)),"[]")
        self.assertEqual(self.sql(metadata),authority)
        self.file(schema.ROOT / "migrations/087_storage_v2_scoped_search_scaling.sql")
        self.assertEqual(self.sql(metadata),authority)
        self.assertEqual(self.exact_search(term,source_id=15)["fully_scored_views"],24)
        self.assertEqual(self.exact_search(term,source_id=15)["total"],24)
        self.assert_sql_fails(self.actor(schema.OTHER_ID,requests[0].rsplit('; ',1)[1]),
                              "authorized generation selector required")
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,"SELECT count(*) FROM storage_v2_lexical_segment")),"0")
        self.assertEqual(self.sql("SELECT relforcerowsecurity FROM pg_class WHERE oid='storage_v2_lexical_segment'::REGCLASS"),"t")
        self.assert_sql_fails(self.actor(schema.OTHER_ID,batch),"authorized source-backed lexical segment required")

        # A fragment has a different hash from the legacy whole file. Rank
        # materialization must precede the contiguous-copy early return.
        fragment_prefix=("BEGIN; ALTER TABLE storage_v2_legacy_lexical_segment DISABLE TRIGGER "
            "storage_v2_legacy_lexical_segment_immutable; "
            f"DELETE FROM storage_v2_legacy_lexical_segment WHERE occurrence_id={known_occurrence}; "
            f"UPDATE files SET hash=digest('alpha visible whole-file suffix','sha256'), "
            f"content_text='alpha visible whole-file suffix' WHERE id={known_file}; ")
        fragment_probe=(fragment_prefix+self.admin(
            f"SELECT storage_v2_copy_legacy_lexical_segments({known_occurrence},{known_artifact}); ")
            +f" RESET ROLE; SELECT count(*) FROM storage_v2_legacy_lexical_segment WHERE occurrence_id={known_occurrence}; ROLLBACK;")
        self.assertEqual(self.sql(fragment_probe).splitlines()[-1],"0")
        old_rank_definition=self.sql("SELECT pg_get_functiondef('storage_v2_source_segment_ranks(bigint[],text)'::REGPROCEDURE)")
        stable_source_requests=[request for request in requests if "(15," in request]
        stable_envelopes=[self.sql(request) for request in stable_source_requests]
        self.file(schema.ROOT / "migrations/089_storage_v2_fragment_rank_precision.sql")
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks_precise(BIGINT[],TEXT) TO storage_v2_shadow_worker")
        self.assertEqual(self.sql(fragment_probe).splitlines()[-1],"2")
        self.assertEqual([self.sql(request) for request in stable_source_requests],stable_envelopes)
        self.assertEqual(self.sql("SELECT pg_get_functiondef('storage_v2_source_segment_ranks(bigint[],text)'::REGPROCEDURE)"),old_rank_definition)
        self.assertEqual(self.sql(metadata),authority)
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,rank_request.replace(
            "storage_v2_source_segment_ranks(","storage_v2_source_segment_ranks_precise("))),"[]")

        # Two close proximity ranks collapse inside a REAL million-point tier.
        # The precise surface must select the stronger rank before the tie key.
        precision_prefix=("BEGIN; ALTER TABLE storage_v2_legacy_lexical_segment DISABLE TRIGGER "
            "storage_v2_legacy_lexical_segment_immutable; "
            f"DELETE FROM storage_v2_legacy_lexical_segment WHERE occurrence_id={occurrence}; "
            "INSERT INTO storage_v2_legacy_lexical_segment(occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,fts_vector) VALUES "
            f"({occurrence},19,{artifact},9001,digest('foo_3d alpha','sha256'),to_tsvector('simple','foo '||repeat('x ',32)||'alpha')),"
            f"({occurrence},19,{artifact},9002,digest('foo_3d alpha','sha256'),to_tsvector('simple','foo '||repeat('x ',31)||'alpha')); ")
        order_probe=self.admin(
            f"SELECT segment_order FROM storage_v2_source_segment_ranks(ARRAY[{occurrence}]::BIGINT[],'foo alpha'); "
            f"SELECT segment_order FROM storage_v2_source_segment_ranks_precise(ARRAY[{occurrence}]::BIGINT[],'foo alpha');")
        self.assertEqual(self.sql(precision_prefix+order_probe+" ROLLBACK;").splitlines(),["9001","9002"])
        self.file(schema.ROOT / "migrations/089_storage_v2_fragment_rank_precision.sql")
        self.assertEqual(self.sql(metadata),authority)

        # Interactive corpus bindings are shared across scoring branches. Keep
        # every complete envelope, boundary tie and permission exact; only the
        # two function-local JIT settings intentionally change configuration.
        current_envelopes=[self.sql(request) for request in requests]
        previous_metadata=json.loads(self.sql(metadata))
        self.file(schema.ROOT / "migrations/091_storage_v2_materialized_corpus_bindings.sql")
        self.assertEqual([self.sql(request) for request in requests],current_envelopes)
        expected_metadata=[]
        for row in previous_metadata:
            if row["proname"] in {"storage_v2_search_exact","storage_v2_search_active_unchecked"}:
                row["proconfig"]=[value for value in row["proconfig"] or []
                                  if not value.startswith("jit=")]+["jit=off"]
                definition=self.sql(f"SELECT pg_get_functiondef({row['oid']})")
                self.assertEqual(definition.count("    scoped_binding AS MATERIALIZED ("),1)
                self.assertNotIn("    scoped_binding AS (",definition)
            expected_metadata.append(row)
        self.assertEqual(json.loads(self.sql(metadata)),expected_metadata)
        self.file(schema.ROOT / "migrations/091_storage_v2_materialized_corpus_bindings.sql")
        self.assertEqual(json.loads(self.sql(metadata)),expected_metadata)
        self.assert_sql_fails(self.actor(schema.OTHER_ID,requests[0].rsplit('; ',1)[1]),
                              "authorized generation selector required")

        # Probe terms once and test segment presence without enumerating its
        # rows. Duplicate/NULL requests, generated/copied provenance and denied
        # source visibility must keep the complete result and function metadata.
        presence_request=("SELECT COALESCE(jsonb_agg(occurrence_id ORDER BY occurrence_id),'[]'::JSONB) "
                          "FROM storage_v2_source_segment_presence("
                          f"ARRAY[{window_occurrence},{context_occurrence},{known_occurrence},"
                          f"{generated_occurrence},{known_occurrence},NULL,999999999]::BIGINT[])")
        presence_before=[self.sql(self.actor(actor,presence_request))
                         for actor in (schema.ADMIN_ID,schema.OTHER_ID)]
        changed_metadata=metadata.replace(
            "'storage_v2_source_segment_ranks(bigint[],text)'::regprocedure)",
            "'storage_v2_source_segment_ranks(bigint[],text)'::regprocedure,"
            "'storage_v2_source_segment_ranks_precise(bigint[],text)'::regprocedure,"
            "'storage_v2_source_segment_presence(bigint[])'::regprocedure)")
        authority=self.sql(changed_metadata)
        current_envelopes=[self.sql(request) for request in requests]
        for _ in range(2):
            self.file(schema.ROOT / "migrations/092_storage_v2_query_posting_reuse.sql")
            self.assertEqual([self.sql(request) for request in requests],current_envelopes)
            self.assertEqual([self.sql(self.actor(actor,presence_request))
                              for actor in (schema.ADMIN_ID,schema.OTHER_ID)],presence_before)
            self.assertEqual(self.sql(changed_metadata),authority)
            self.assertEqual(self.sql(self.admin(rank_request)),previous_ranks)
        self.assertEqual(presence_before[1],"[]")
        self.assert_sql_fails(self.actor(schema.OTHER_ID,requests[0].rsplit('; ',1)[1]),
                              "authorized generation selector required")

        # Unbound documents can make a globally common term enormous without
        # changing this source's corpus or any of its scores. Exceed the probe
        # cap, then require full result identity through both complete paths.
        self.sql(self.admin(
            "SELECT count(*) FROM (SELECT storage_v2_put_search_document("
            "'bounded-probe-fixture-'||n::TEXT,'node',"
            f"(SELECT content_root_node_id FROM artifact_version WHERE id={known_artifact}),"
            "(SELECT document.search_text FROM storage_v2_search_document document "
            "JOIN storage_v2_search_view_document binding ON binding.document_id=document.id "
            "JOIN occurrence occurrence_row ON occurrence_row.view_id=binding.view_id "
            f"WHERE occurrence_row.id={known_occurrence} AND binding.ordinal=0),"
            "ARRAY[]::TEXT[]) FROM generate_series(1,4097) n) docs"))
        self.assertEqual([self.sql(request) for request in requests],current_envelopes)
        for _ in range(2):
            self.file(schema.ROOT / "migrations/093_storage_v2_bounded_term_probes.sql")
            self.assertEqual([self.sql(request) for request in requests],current_envelopes)
            self.assertEqual(self.sql(changed_metadata),authority)
            self.assertEqual([self.sql(self.actor(actor,presence_request))
                              for actor in (schema.ADMIN_ID,schema.OTHER_ID)],presence_before)
        self.assert_sql_fails(self.actor(schema.OTHER_ID,requests[0].rsplit('; ',1)[1]),
                              "authorized generation selector required")

        # Legacy indexing can cover only a prefix of a complete immutable
        # file. Its old rank must stay exact, while a new conjunction in the
        # unindexed suffix remains searchable and supported by query evidence.
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(22,'partial-projection-fixture','fixture','partial-projection')")
        self.sql("GRANT EXECUTE ON FUNCTION "
                 "storage_v2_materialize_legacy_chunk_ranks(BIGINT,BIGINT) "
                 "TO storage_v2_shadow_worker")
        partial_body = "legacy anchor novel conjunction"
        partial_run = self.begin(22, "a1" * 32, "a2" * 32, commit_sha=COMMIT)
        partial_node, partial_view, partial_digest = self.make_projection(partial_body)
        self.stage(partial_run, "partial.txt", partial_body, partial_node,
                   partial_view, partial_digest)
        self.complete_analysis(partial_digest)
        partial_document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{partial_node},"
            f"'{partial_body}',ARRAY[]::TEXT[])"))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document("
                            f"{partial_view},0,{partial_document},1.0)"))
        self.commit(partial_run, 1)
        partial_generation = int(self.sql(
            f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={partial_run}"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation("
                            f"{partial_generation},'{'a3' * 32}')"))
        partial_occurrence, partial_artifact = map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT "
            "FROM occurrence WHERE source_id=22").split(":"))
        self.sql("WITH file AS (INSERT INTO files(source_id,path,hash,content,"
                 "content_text,size_original,size_compressed,last_modified) VALUES "
                 f"(22,'/synthetic/partial.txt',digest('{partial_body}','sha256'),'',"
                 f"'{partial_body}',{len(partial_body)},0,now()) RETURNING id) "
                 "INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,"
                 "content_text,start_line,end_line) SELECT id,'text',"
                 "digest('legacy anchor','sha256'),'','legacy anchor',1,1 FROM file")
        self.sql(self.admin(f"SELECT storage_v2_put_lexical_segment("
                            f"{partial_occurrence},{partial_artifact},0,"
                            f"'{partial_body}','','text'); "
                            f"SELECT storage_v2_materialize_legacy_chunk_ranks("
                            f"{partial_occurrence},{partial_artifact})"))
        new_conjunction = {"type": "and", "children": [
            {"type": "term", "value": "novel"},
            {"type": "term", "value": "conjunction"}]}
        self.assertEqual(self.exact_search(new_conjunction, source_id=22)["results"], [])
        legacy_ast = {"type": "and", "children": [
            {"type": "term", "value": "legacy"},
            {"type": "term", "value": "anchor"}]}
        partial_before = self.exact_search(legacy_ast, source_id=22)
        partial_rows = self.sql("SELECT jsonb_agg(to_jsonb(projection)) "
                               "FROM storage_v2_legacy_lexical_segment projection "
                               f"WHERE occurrence_id={partial_occurrence}")
        for _ in range(2):
            self.file(schema.ROOT / "migrations/094_storage_v2_query_specific_projection_fallback.sql")
            self.assertEqual([self.sql(request) for request in requests], current_envelopes)
            self.assertEqual(self.sql(changed_metadata), authority)
            self.assertEqual(self.exact_search(legacy_ast, source_id=22), partial_before)
            added = self.exact_search(new_conjunction, source_id=22)
            self.assertEqual([row["occurrence_id"] for row in added["results"]],
                             [partial_occurrence])
            self.assertLess(added["results"][0]["score"], 1000000.0)
            for rank in ("storage_v2_source_segment_ranks",
                         "storage_v2_source_segment_ranks_precise"):
                request = (f"SELECT occurrence_id FROM {rank}("
                           f"ARRAY[{partial_occurrence},{partial_occurrence},NULL]::BIGINT[],"
                           "'novel conjunction')")
                self.assertEqual(self.sql(self.admin(request)), str(partial_occurrence))
                self.assertEqual(self.sql(self.actor(schema.OTHER_ID, request)), "")
            proof = json.loads(self.sql(self.admin(
                f"SELECT storage_v2_candidate_query_evidence(22,{partial_generation},"
                f"'{COMMIT}','novel conjunction',ARRAY[{partial_occurrence}]::BIGINT[],"
                "ARRAY[]::BIGINT[])")))
            self.assertTrue(proof["candidate"][0]["fts_body_matches"])
            self.assertTrue(proof["candidate"][0]["segment_matches"])
            self.assertFalse(proof["candidate"][0]["legacy_segment_matches"])
        self.assertEqual(self.sql("SELECT jsonb_agg(to_jsonb(projection)) "
                                  "FROM storage_v2_legacy_lexical_segment projection "
                                  f"WHERE occurrence_id={partial_occurrence}"), partial_rows)

        # Chunk boundaries can split a larger source token. The copied slice
        # is still exact source data, even though whole-document FTS misses it.
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(23,'copied-token-boundary','fixture','copied-token-boundary')")
        boundary_body = "prefixalpha omegaSuffix"
        boundary_run = self.begin(23, "b1" * 32, "b2" * 32, commit_sha=COMMIT)
        boundary_node, boundary_view, boundary_digest = self.make_projection(boundary_body)
        self.stage(boundary_run, "boundary.txt", boundary_body, boundary_node,
                   boundary_view, boundary_digest)
        self.complete_analysis(boundary_digest)
        boundary_document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{boundary_node},"
            f"'{boundary_body}',ARRAY[]::TEXT[])"))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document("
                            f"{boundary_view},0,{boundary_document},1.0)"))
        self.commit(boundary_run, 1)
        boundary_generation = int(self.sql(
            f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={boundary_run}"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation("
                            f"{boundary_generation},'{'b3' * 32}')"))
        boundary_occurrence, boundary_artifact = map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT "
            "FROM occurrence WHERE source_id=23").split(":"))
        boundary_chunk = int(self.sql(
            "WITH file AS (INSERT INTO files(source_id,path,hash,content,content_text,"
            "size_original,size_compressed,last_modified) VALUES "
            f"(23,'/synthetic/boundary.txt',digest('{boundary_body}','sha256'),'',"
            f"'{boundary_body}',{len(boundary_body)},0,now()) RETURNING id) "
            "INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,"
            "content_text,start_line,end_line) SELECT id,'text',digest('alpha omega','sha256'),"
            "'','alpha omega',1,1 FROM file RETURNING id"))
        self.assertEqual(self.sql(self.admin(f"SELECT storage_v2_copy_legacy_lexical_segments("
                                           f"{boundary_occurrence},{boundary_artifact})")), "1")
        boundary_ast = {"type": "and", "children": [
            {"type": "term", "value": "alpha"}, {"type": "term", "value": "omega"}]}
        self.assertEqual(self.exact_search(boundary_ast, source_id=23)["results"], [])
        self.file(schema.ROOT / "migrations/088_storage_v2_candidate_requalification.sql")
        dual_id = "00000000-0000-4000-8000-000000000095"
        old_artifact = '{"status":"PASS","unexplained_count":0,"comparisons":[]}'
        dual_sql = ("SELECT to_jsonb(storage_v2_record_dual_read_evidence("
                    f"'{dual_id}',23,{boundary_generation},'{COMMIT}',"
                    f"'{'b2' * 32}','{'b4' * 32}','{old_artifact}'::JSONB))")
        original_dual = json.loads(self.sql(self.admin(dual_sql)))
        revised_artifact = ('{"status":"PASS","unexplained_count":0,'
                            '"comparisons":[{"classification":"segmentation"}]}')
        revised_sql = dual_sql.replace(old_artifact, revised_artifact)
        self.assert_sql_fails(self.admin(revised_sql), "dual-read evidence identity collision")
        for _ in range(2):
            self.file(schema.ROOT / "migrations/095_storage_v2_qualification_continuation.sql")
            self.assertEqual([self.sql(request) for request in requests], current_envelopes)
            self.assertEqual(self.sql(changed_metadata), authority)
            boundary_result = self.exact_search(boundary_ast, source_id=23)
            self.assertEqual([row["occurrence_id"] for row in boundary_result["results"]],
                             [boundary_occurrence])
            self.assertGreater(boundary_result["results"][0]["score"], 1000000.0)
            proof_sql = (f"SELECT storage_v2_candidate_query_evidence(23,{boundary_generation},"
                         f"'{COMMIT}','alpha omega',ARRAY[{boundary_occurrence}]::BIGINT[],"
                         f"ARRAY[{boundary_chunk}]::BIGINT[])")
            proof = json.loads(self.sql(self.admin(proof_sql)))
            self.assertTrue(proof["candidate"][0]["fts_body_matches"])
            self.assertTrue(proof["candidate"][0]["segment_matches"])
            self.assertTrue(proof["candidate"][0]["legacy_segment_matches"])
            self.assertEqual(self.sql(self.actor(schema.OTHER_ID,
                "SET ROLE mainrag; SELECT storage_v2_source_legacy_segment_matches("
                f"{boundary_occurrence},'alpha omega')")), "f")
            self.assertEqual(self.sql(self.admin(
                "SET ROLE mainrag; SELECT storage_v2_source_legacy_segment_matches("
                f"{partial_occurrence},'novel conjunction')")), "f")
            revised_dual = json.loads(self.sql(self.admin(revised_sql)))
            self.assertNotEqual(revised_dual["id"], dual_id)
            self.assertEqual(json.loads(self.sql(self.admin(dual_sql))), original_dual)
            self.assertEqual(json.loads(self.sql(self.admin(revised_sql))), revised_dual)
            self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_dual_read_evidence "
                                     f"WHERE source_id=23 AND generation_id={boundary_generation}"), "2")
        self.assert_sql_fails(self.admin(revised_sql.replace("'" + COMMIT + "'", "'" + "c" * 40 + "'")),
                              "dual-read evidence identity collision")
        self.assert_sql_fails(self.actor(schema.OTHER_ID, revised_sql), "source access denied")
        self.assert_sql_fails(f"UPDATE storage_v2_dual_read_evidence SET artifact=artifact WHERE id='{dual_id}'",
                              "dual-read artifacts are immutable")
        self.assert_sql_fails(f"DELETE FROM storage_v2_dual_read_evidence WHERE id='{dual_id}'",
                              "dual-read artifacts are immutable")
        self.sql(f"DELETE FROM chunks WHERE id={boundary_chunk}")
        self.assertEqual(self.exact_search(boundary_ast, source_id=23), boundary_result)
        portable_proof = json.loads(self.sql(self.admin(proof_sql.replace(
            f"ARRAY[{boundary_chunk}]::BIGINT[]", "ARRAY[]::BIGINT[]"))))
        self.assertTrue(portable_proof["candidate"][0]["fts_body_matches"])
        self.assertTrue(portable_proof["candidate"][0]["legacy_segment_matches"])
        self.assertEqual(self.sql("SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL"), "0")

        self.assertEqual(json.loads(self.sql(self.admin(verify_window)))["invalid_count"],0)

        # Corruption is injected only in a disposable fixture transaction.
        # An error rolls back both the mutation and the disabled trigger.
        mutation_prefix = (
            "BEGIN; ALTER TABLE storage_v2_lexical_segment DISABLE TRIGGER "
            "storage_v2_lexical_segment_immutable; ")
        for value in ("text_sha256=digest('corrupt','sha256')",
                      "fts_vector=to_tsvector('simple','corrupt')"):
            self.assert_sql_fails(
                mutation_prefix + "UPDATE storage_v2_lexical_segment SET " + value
                + f" WHERE occurrence_id={window_occurrence} AND segment_order=0; "
                + self.admin(verify_window),
                "lexical segment projection is incomplete or differs from immutable source")
        self.assert_sql_fails(
            mutation_prefix + "DELETE FROM storage_v2_lexical_segment "
            + f"WHERE occurrence_id={window_occurrence}; " + self.admin(verify_window),
            "lexical segment projection is incomplete or differs from immutable source")
        self.assertEqual(json.loads(self.sql(self.admin(verify_window)))["segment_count"], 4)
        self.sql(f"DELETE FROM chunks WHERE id={context_chunk}")
        self.assertEqual(self.sql(self.admin(rank_query)).splitlines(),
                         [str(context_occurrence), str(generated_occurrence)])
        portable_context = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_candidate_query_evidence(20,{context_generation},"
            f"'{COMMIT}','prefixonly alpha',ARRAY[{context_occurrence}]::BIGINT[],"
            "ARRAY[]::BIGINT[])")))
        self.assertFalse(portable_context["candidate"][0]["fts_body_matches"])
        self.assertTrue(portable_context["candidate"][0]["segment_matches"])
        self.assertTrue(portable_context["candidate"][0]["legacy_segment_matches"])

        # Query-scoped input gathering must preserve complete envelopes and
        # copied token-boundary support after mutable legacy chunks are gone.
        # Check both rank surfaces, duplicate/NULL requests, generated fallback,
        # empty matches, authorization and unchanged function ownership/config.
        scoped_before = [self.sql(request) for request in requests]
        scoped_authority = self.sql(changed_metadata)
        scoped_rank_requests = [
            self.admin(
                "SELECT COALESCE(jsonb_agg(to_jsonb(rank) ORDER BY occurrence_id),'[]'::JSONB) "
                f"FROM {surface}(ARRAY[{boundary_occurrence},{partial_occurrence},"
                f"{generated_occurrence},{boundary_occurrence},NULL,999999999]::BIGINT[],"
                f"'{query}') rank"
            )
            for surface in ("storage_v2_source_segment_ranks",
                            "storage_v2_source_segment_ranks_precise")
            for query in ("alpha omega", "novel conjunction", "prefixonly alpha",
                          "synthetic_no_lexical_matches")
        ]
        scoped_ranks = [self.sql(request) for request in scoped_rank_requests]
        for _ in range(2):
            self.file(schema.ROOT / "migrations/096_storage_v2_query_scoped_lexical_inputs.sql")
            self.assertEqual([self.sql(request) for request in requests], scoped_before)
            self.assertEqual(self.sql(changed_metadata), scoped_authority)
            self.assertEqual([self.sql(request) for request in scoped_rank_requests], scoped_ranks)
            self.assertEqual(self.exact_search(boundary_ast, source_id=23), boundary_result)
            self.assertEqual(self.exact_search(new_conjunction, source_id=22), added)
            self.assertEqual(self.sql(self.admin(rank_query)).splitlines(),
                             [str(context_occurrence), str(generated_occurrence)])
            denied = ("SET ROLE mainrag; SELECT occurrence_id FROM "
                      "storage_v2_source_segment_ranks_precise("
                      f"ARRAY[{boundary_occurrence},{partial_occurrence}]::BIGINT[],"
                      "'alpha omega')")
            self.assertEqual(self.sql(self.actor(schema.OTHER_ID, denied)), "")
            self.assertEqual(json.loads(self.sql(self.admin(proof_sql.replace(
                f"ARRAY[{boundary_chunk}]::BIGINT[]", "ARRAY[]::BIGINT[]")))), portable_proof)

        self.assertEqual(self.sql(
            "SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL"), "0")
        self.sql("DROP INDEX idx_storage_v2_lexical_segment_source; "
                 "CREATE INDEX idx_storage_v2_lexical_segment_source "
                 "ON storage_v2_lexical_segment(source_id,artifact_version_id)")
        mismatch = self.command(
            "--file", str(schema.ROOT / "migrations/096_storage_v2_query_scoped_lexical_inputs.sql"),
            check=False)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("source lexical input index definition differs", mismatch.stderr)
        self.sql("DROP INDEX idx_storage_v2_lexical_segment_source")
        self.file(schema.ROOT / "migrations/096_storage_v2_query_scoped_lexical_inputs.sql")
        self.assertEqual([self.sql(request) for request in requests], scoped_before)

        # Materialized query matches and the positive-evidence guard retain
        # complete envelopes, scalar rank precision and every source boundary.
        boolean_queries = [
            {"type": "term", "value": "alpha"},
            {"type": "term", "value": "synthetic_no_lexical_matches"},
            {"type": "phrase", "value": "alpha beta"},
            {"type": "exact", "value": "exact_key"},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"},
                {"type": "not", "children": [
                    {"type": "term", "value": "forbidden"}]}]},
            {"type": "or", "children": [
                {"type": "term", "value": "entry0"},
                {"type": "term", "value": "entry1"}]},
            {"type": "group", "children": [{"type": "or", "children": [
                {"type": "phrase", "value": "alpha beta"},
                {"type": "exact", "value": "exact_key"}]}]},
        ]
        boolean_filters = [{}, {"path_prefix": "/synthetic/late-000"},
                           {"occurred_from": "2100-01-01T00:00:00Z"}]
        boolean_before = [self.exact_search(query, filters, source_id=15)
                          for query in boolean_queries for filters in boolean_filters]
        for _ in range(2):
            self.file(schema.ROOT / "migrations/098_storage_v2_materialized_lexical_matches.sql")
            self.assertEqual([self.sql(request) for request in requests], scoped_before)
            self.assertEqual(self.sql(changed_metadata), scoped_authority)
            self.assertEqual([self.sql(request) for request in scoped_rank_requests], scoped_ranks)
            self.assertEqual([self.exact_search(query, filters, source_id=15)
                              for query in boolean_queries for filters in boolean_filters],
                             boolean_before)
            self.assertEqual(self.exact_search(boundary_ast, source_id=23), boundary_result)
            self.assertEqual(self.exact_search(new_conjunction, source_id=22), added)
            self.assertEqual(self.sql(self.actor(schema.OTHER_ID, denied)), "")
            self.assertEqual(self.sql(precision_prefix+order_probe+" ROLLBACK;").splitlines(),
                             ["9001", "9002"])

        helper = "storage_v2_authorized_lexical_matches(bigint[],bigint[],text)"
        self.assertEqual(self.sql(
            "SELECT relrowsecurity::TEXT||':'||relforcerowsecurity::TEXT "
            "FROM pg_class WHERE oid='storage_v2_lexical_segment'::REGCLASS"), "true:true")
        self.assertEqual(self.sql(
            "SELECT rolcanlogin::TEXT||':'||rolbypassrls::TEXT||':'||rolsuper::TEXT "
            "FROM pg_roles WHERE rolname='mainrag_v2_lexical_rank_owner'"), "false:false:false")
        self.assertEqual(self.sql(
            f"SELECT has_function_privilege('mainrag','{helper}','EXECUTE')::TEXT "
            f"||':'||has_function_privilege('mainrag_v2_frontier_owner','{helper}','EXECUTE')::TEXT"
        ), "false:true")
        self.assert_sql_fails(
            "SET SESSION AUTHORIZATION mainrag; SET ROLE mainrag_v2_lexical_rank_owner;",
            "permission denied")
        self.assert_sql_fails(self.actor(schema.OTHER_ID,
            "SET ROLE mainrag; SELECT * FROM storage_v2_authorized_lexical_matches("
            f"ARRAY[{boundary_occurrence}]::BIGINT[],ARRAY[23]::BIGINT[],'alpha');"),
            "permission denied")
        # Even the trusted definer rejects forged source hints under a denied
        # actor; callers cannot turn the role-specific policy into source access.
        self.assertEqual(self.sql(self.actor(schema.OTHER_ID,
            "SET ROLE mainrag_v2_frontier_owner; SELECT * FROM "
            "storage_v2_authorized_lexical_matches("
            f"ARRAY[{boundary_occurrence}]::BIGINT[],ARRAY[23]::BIGINT[],'alpha');")), "")
        spoofed_authority = (
            "BEGIN; CREATE TEMP TABLE sources(id BIGINT); "
            "CREATE TEMP TABLE users(id UUID,is_admin BOOLEAN); "
            f"INSERT INTO users VALUES('{schema.OTHER_ID}',TRUE); "
            "GRANT SELECT ON users,sources TO mainrag_v2_lexical_rank_owner; "
            + self.actor(schema.OTHER_ID,
                "SET ROLE mainrag_v2_frontier_owner; SELECT count(*) FROM "
                "storage_v2_authorized_lexical_matches("
                f"ARRAY[{boundary_occurrence}]::BIGINT[],ARRAY[23]::BIGINT[],'alpha');")
            + " RESET ROLE; ROLLBACK;"
        )
        self.assertEqual(self.sql(spoofed_authority), "0")
        scoped_documents = (
            "SELECT DISTINCT binding.document_id FROM occurrence occurrence_row "
            "JOIN storage_v2_search_view_document binding "
            "ON binding.view_id=occurrence_row.view_id WHERE occurrence_row.source_id=15"
        )
        for term_value in ("alpha", "synthetic_no_lexical_matches", "exact_key"):
            reference = self.sql(
                "SELECT COALESCE(jsonb_agg(jsonb_build_array(document_id,term,term_frequency) "
                "ORDER BY document_id),'[]'::JSONB) FROM storage_v2_search_posting "
                f"WHERE document_id IN ({scoped_documents}) AND term='{term_value}'"
            )
            for documents in (
                f"ARRAY({scoped_documents})",
                "ARRAY(SELECT document_id FROM (" + scoped_documents + ") scope "
                "CROSS JOIN generate_series(1,5001))",
            ):
                self.assertEqual(self.sql(
                    "SELECT COALESCE(jsonb_agg(jsonb_build_array(document_id,term,term_frequency) "
                    "ORDER BY document_id),'[]'::JSONB) FROM storage_v2_scoped_term_posting("
                    f"{documents},'{term_value}')"), reference)
        before_guard = self.sql(changed_metadata)
        try:
            self.sql("ALTER ROLE mainrag_v2_lexical_rank_owner LOGIN")
            rejected = self.command("--file", str(schema.ROOT /
                "migrations/098_storage_v2_materialized_lexical_matches.sql"), check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("lexical rank owner authority differs", rejected.stderr)
            self.assertEqual(self.sql(changed_metadata), before_guard)
        finally:
            self.sql("ALTER ROLE mainrag_v2_lexical_rank_owner NOLOGIN")
