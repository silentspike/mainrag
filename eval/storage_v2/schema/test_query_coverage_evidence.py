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
