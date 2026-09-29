"""Complete mixed posting reads, exact collisions and immutable constructors."""

from __future__ import annotations

import hashlib
import json
import unittest

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema.test_candidate_projection_restore_schema import (
    CandidateProjectionSchemaTests,
)
from eval.storage_v2.schema.test_search_document_reuse import SearchDocumentReuseTests


MIGRATION = schema.ROOT / "migrations/099_storage_v2_compact_exact_postings.sql"
COMMIT = "a" * 40


class CompactExactPostingTests(unittest.TestCase):
    schema = schema
    command = classmethod(CandidateProjectionSchemaTests.command.__func__)
    sql = classmethod(schema.ShadowIngestSchemaTests.sql.__func__)
    file = classmethod(schema.ShadowIngestSchemaTests.file.__func__)
    admin = classmethod(schema.ShadowIngestSchemaTests.admin.__func__)
    actor = staticmethod(schema.ShadowIngestSchemaTests.actor)
    make_projection = schema.ShadowIngestSchemaTests.make_projection
    begin = schema.ShadowIngestSchemaTests.begin
    stage = schema.ShadowIngestSchemaTests.stage
    complete_analysis = schema.ShadowIngestSchemaTests.complete_analysis
    commit = schema.ShadowIngestSchemaTests.commit
    exact_search = schema.ShadowIngestSchemaTests.exact_search
    assert_sql_fails = schema.ShadowIngestSchemaTests.assert_sql_fails
    start_client = SearchDocumentReuseTests.start_client
    wait_for_client = SearchDocumentReuseTests.wait_for_client

    @classmethod
    def setUpClass(cls):
        schema.ShadowIngestSchemaTests.setUpClass.__func__(cls)
        for number in range(66, 99):
            paths = list((schema.ROOT / "migrations").glob(f"{number:03}_*.sql"))
            if len(paths) != 1:
                raise AssertionError(f"one migration required for {number}")
            cls.file(paths[0])
        cls.sql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO mainrag; "
                "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO mainrag;")
        # Match production relation ownership without changing the dedicated
        # controlled frontier/lexical owners introduced by later migrations.
        cls.sql("""
DO $$ DECLARE relation REGCLASS; routine REGPROCEDURE; BEGIN
 FOR relation IN SELECT oid::REGCLASS FROM pg_class
  WHERE relnamespace='public'::REGNAMESPACE AND relkind IN ('r','p')
    AND relowner=current_user::REGROLE LOOP
  EXECUTE format('ALTER TABLE %s OWNER TO mainrag',relation);
 END LOOP;
 FOR routine IN SELECT oid::REGPROCEDURE FROM pg_proc
  WHERE pronamespace='public'::REGNAMESPACE AND proowner=current_user::REGROLE
    AND (proname LIKE 'storage_v2_%' OR proname='user_can_access_source') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO mainrag',routine);
 END LOOP;
END $$;
""")
        for signature in ("storage_v2_put_search_document(text,text,bigint,text,text[])",
                          "storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)",
                          "storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)",
                          "storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])"):
            cls.sql(f"ALTER FUNCTION {signature} OWNER TO mainrag;")

    @classmethod
    def tearDownClass(cls):
        schema.ShadowIngestSchemaTests.tearDownClass.__func__(cls)

    @staticmethod
    def quote(value):
        return "'" + value.replace("'", "''") + "'"

    def put(self, node, text, profile="mainrag.lexical-simple.v1"):
        return int(self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'{profile}','node',{node},{self.quote(text)},ARRAY['key_identifier'])")))

    def constructor_postings(self, identity):
        return self.sql("SELECT string_agg(term||':'||term_frequency,',' ORDER BY term) FROM ("
            f"SELECT term,term_frequency FROM storage_v2_search_posting WHERE document_id={identity} "
            "UNION ALL SELECT item.term,item.frequency FROM storage_v2_compact_posting_block block "
            "CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency) "
            f"WHERE block.document_id={identity}) posting")

    def test_complete_mixed_search_and_new_constructor_contract(self):
        # Find a deterministic real 16-bit collision. The two complete strings
        # must stay distinct in every helper and in the ordinary search path.
        seen = {}
        for number in range(20000):
            term = f"collision_fixture_{number}"
            fingerprint = hashlib.sha256(term.encode()).digest()[:2]
            if fingerprint in seen:
                collision = seen[fingerprint], term
                break
            seen[fingerprint] = term
        else:
            self.fail("deterministic collision fixture not found")
        long_term = "q" * 3000
        texts = ["alpha beta alpha common beta.gamma key_identifier",
                 "beta gamma common", "common " + long_term,
                 "common " + " ".join(f"token_{i}" for i in range(700)),
                 "common " + collision[0], "common " + collision[1], ""]
        run = self.begin(6, "e1" * 32, "e2" * 32, commit_sha=COMMIT)
        documents = []
        for index, text in enumerate(texts):
            node, view, digest = self.make_projection(text)
            self.stage(run, f"compact-{index}.txt", text, node, view, digest)
            self.complete_analysis(digest)
            document = self.put(node, text)
            documents.append(document)
            self.sql(self.admin(
                f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        self.commit(run, len(texts))
        generation = int(self.sql(
            f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
        self.sql(self.admin(
            f"SELECT storage_v2_verify_generation({generation},'{'e3' * 32}')"))
        occurrence = int(self.sql(
            "SELECT id FROM occurrence WHERE source_id=6 "
            "AND source_path='/synthetic/compact-0.txt'"))
        evidence_call = ("SELECT storage_v2_candidate_query_evidence("
                         f"6,{generation},'{COMMIT}','alpha',"
                         f"ARRAY[{occurrence}]::BIGINT[],ARRAY[]::BIGINT[])")
        evidence_before = json.loads(self.sql(self.admin(evidence_call)))
        asts = [{"type": "term", "value": "alpha"},
                {"type": "term", "value": "common"},
                {"type": "term", "value": collision[0]},
                {"type": "term", "value": long_term},
                {"type": "and", "children": [{"type": "term", "value": "alpha"},
                                                {"type": "term", "value": "beta"}]},
                {"type": "or", "children": [{"type": "term", "value": "alpha"},
                                               {"type": "term", "value": "gamma"}]},
                {"type": "and", "children": [{"type": "term", "value": "common"},
                                                {"type": "not", "children": [
                                                    {"type": "term", "value": "gamma"}]}]},
                {"type": "phrase", "value": "alpha beta"},
                {"type": "exact", "value": "key_identifier"}]
        filters = ({}, {"path_prefix": "/synthetic/compact-0"}, {"role": "artifact"})
        before = [self.exact_search(ast, filter_value, user_id=user)
                  for user in (schema.ADMIN_ID, schema.WRITER_ID)
                  for ast in asts for filter_value in filters]
        identity_sql = ("SELECT md5(jsonb_agg(to_jsonb(d) ORDER BY id)::TEXT) "
                        "FROM storage_v2_search_document d; "
                        "SELECT md5(jsonb_agg(to_jsonb(o) ORDER BY id)::TEXT) "
                        "FROM occurrence o; "
                        "SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL;")
        identity_before = self.sql(identity_sql)
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(identity_before, self.sql(identity_sql))
        self.assertEqual(evidence_before, json.loads(self.sql(self.admin(evidence_call))))

        # Convert only disposable fixture rows to exercise the two layouts in
        # one unchanged generation. Production migration never rewrites rows.
        compact_ids = documents[::2]
        compact_array = "ARRAY" + str(compact_ids) + "::BIGINT[]"
        self.sql("""
WITH numbered AS (
 SELECT document_id,term,term_frequency,
        (row_number() OVER (PARTITION BY document_id ORDER BY term COLLATE "C")-1)/256 block_order
 FROM storage_v2_search_posting WHERE document_id=ANY(""" + compact_array + """))
INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies)
 SELECT document_id,block_order,array_agg(term ORDER BY term COLLATE "C"),
        array_agg(term_frequency ORDER BY term COLLATE "C")
 FROM numbered GROUP BY document_id,block_order;
ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_immutable;
DELETE FROM storage_v2_search_posting WHERE document_id=ANY(""" + compact_array + """);
ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_immutable;
""")
        after = [self.exact_search(ast, filter_value, user_id=user)
                 for user in (schema.ADMIN_ID, schema.WRITER_ID)
                 for ast in asts for filter_value in filters]
        self.assertEqual(before, after, "complete mixed-layout envelopes differ")
        self.assertEqual(identity_before, self.sql(identity_sql))
        self.assertEqual(evidence_before, json.loads(self.sql(self.admin(evidence_call))))
        self.assert_sql_fails(self.actor(schema.OTHER_ID,
            "SELECT storage_v2_search_exact(6,'1','{\"type\":\"term\",\"value\":\"alpha\"}',"
            "'{}',10)"), "authorized generation selector")

        # The actual compact constructor keeps the full tokenizer and exact
        # identity/reuse contract, including punctuation and oversized terms.
        content = "alpha alpha beta.gamma naïve " + long_term + " " + " ".join(
            f"new_token_{i}" for i in range(700))
        node, _, _ = self.make_projection(content)
        new_document = self.put(node, content, "compact-constructor-fixture")
        self.assertEqual(new_document, self.put(node, content, "compact-constructor-fixture"))
        self.assertEqual(self.sql(
            f"SELECT count(*) FROM storage_v2_search_posting WHERE document_id={new_document}"), "0")
        self.assertGreater(int(self.sql(
            f"SELECT count(*) FROM storage_v2_compact_posting_block WHERE document_id={new_document}")), 1)
        reference = """WITH tokens AS (
 SELECT token FROM regexp_split_to_table(lower(""" + self.quote(content) + """),'[^[:alnum:]_]+') token
 WHERE token<>'' UNION ALL
 SELECT token FROM regexp_split_to_table(lower(""" + self.quote(content) + """),'[[:space:]]+') token
 WHERE token<>'' AND token !~ '^[[:alnum:]_]+$' AND token ~ '[[:alnum:]_]'
), expected AS (SELECT token term,count(*) frequency FROM tokens GROUP BY token),
actual AS (SELECT item.term,item.frequency FROM storage_v2_compact_posting_block block
 CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
 WHERE block.document_id=""" + str(new_document) + """), difference AS (
 (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual) UNION ALL
 (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)) SELECT count(*) FROM difference;"""
        self.assertEqual(self.sql(reference), "0")
        self.assert_sql_fails(self.admin(
            "SELECT storage_v2_put_search_document('compact-constructor-fixture','node',"
            f"{node},'changed',ARRAY['key_identifier'])"), "profile collision")
        self.assert_sql_fails(self.actor(schema.WRITER_ID,
            "SELECT storage_v2_put_search_document('unauthorized','node',"
            f"{node},'alpha',ARRAY[]::TEXT[])"), "administrator authority")
        self.assert_sql_fails(f"UPDATE storage_v2_compact_posting_block SET terms=terms "
                              f"WHERE document_id={new_document}", "immutable")
        self.assert_sql_fails(f"DELETE FROM storage_v2_compact_posting_block "
                              f"WHERE document_id={new_document}", "immutable")

        # Both plan choices are exact, even for duplicate/NULL/unknown scope
        # entries and a colliding fingerprint. The large array selects the
        # indexed branch without adding any document to the logical scope.
        all_ids = documents + [new_document, new_document, 999999]
        for term in ("common", "alpha", "beta.gamma", long_term, *collision, "missing_fixture"):
            for scope in ("ARRAY" + str(all_ids) + "::BIGINT[]",
                          "ARRAY(SELECT unnest(ARRAY" + str(all_ids) +
                          "::BIGINT[]) FROM generate_series(1,1200))",
                          "ARRAY[NULL]::BIGINT[]", "ARRAY[]::BIGINT[]"):
                actual = self.sql("SET ROLE mainrag; SELECT COALESCE(jsonb_agg(to_jsonb(p) "
                    "ORDER BY document_id,term),'[]'::JSONB) FROM storage_v2_scoped_term_posting("
                    f"{scope},{self.quote(term)}) p")
                expected = self.sql("SET ROLE mainrag; SELECT COALESCE(jsonb_agg(to_jsonb(p) "
                    "ORDER BY document_id,term),'[]'::JSONB) FROM ("
                    "SELECT requested.id document_id,posting.term,posting.term_frequency "
                    f"FROM (SELECT DISTINCT id FROM unnest({scope}) input(id)) requested "
                    f"CROSS JOIN LATERAL storage_v2_document_posting(requested.id,{self.quote(term)}) posting) p")
                self.assertEqual(json.loads(actual), json.loads(expected))
        collision_result = json.loads(self.sql("SET ROLE mainrag; SELECT jsonb_agg(term) "
            f"FROM storage_v2_posting_probe({self.quote(collision[0])},4097)"))
        self.assertEqual(collision_result, [collision[0]])
        self.sql("GRANT SELECT ON storage_v2_compact_posting_block TO storage_v2_shadow_worker")
        self.assertEqual(self.sql(self.actor(schema.WRITER_ID,
            "SELECT count(*) FROM storage_v2_compact_posting_block")), "0")
        self.assert_sql_fails(self.actor(schema.WRITER_ID,
            "SELECT * FROM storage_v2_document_posting(1,'alpha')"), "permission denied")
        self.file(MIGRATION)
        self.assertEqual(after, [self.exact_search(ast, filter_value, user_id=user)
            for user in (schema.ADMIN_ID, schema.WRITER_ID)
            for ast in asts for filter_value in filters])
        self.assertEqual(self.sql("SELECT bool_and(attcompression='l') FROM pg_attribute "
            "WHERE attrelid='storage_v2_compact_posting_block'::REGCLASS "
            "AND attname IN ('terms','term_frequencies')"), "t")
        # Reuse the race barrier from the ordinary constructor gate. This
        # executes the actual conflict readback for both component kinds after
        # compact blocks are written, including rejected text/identifier races.
        SearchDocumentReuseTests.test_concurrent_insert_reuses_identity_and_rejects_conflicting_materializations(self)
        drift = self.command("--command", "BEGIN; DROP INDEX idx_storage_v2_compact_posting_fingerprint; "
            "CREATE INDEX idx_storage_v2_compact_posting_fingerprint "
            "ON storage_v2_compact_posting_block USING GIN(fingerprints) WHERE FALSE; "
            + MIGRATION.read_text().replace("\nBEGIN;", "\n", 1).rsplit("COMMIT;", 1)[0]
            + "ROLLBACK;", check=False)
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("compact posting index identity differs", drift.stderr)
        self.assertEqual(self.sql("SELECT indpred IS NULL FROM pg_index WHERE "
            "indexrelid='idx_storage_v2_compact_posting_fingerprint'::REGCLASS"), "t")
        self.assertEqual(self.sql("SET ROLE mainrag; SELECT count(*) FROM "
            "storage_v2_document_posting(NULL,'alpha'); SELECT count(*) FROM "
            "storage_v2_document_posting(1,NULL); SELECT count(*) FROM "
            "storage_v2_posting_probe(NULL,4097); SELECT count(*) FROM "
            "storage_v2_posting_probe('alpha',NULL)"), "0\n0\n0\n0")
        constraint = self.sql("SELECT conname FROM pg_constraint WHERE "
            "conrelid='storage_v2_compact_posting_block'::REGCLASS AND contype='c' "
            "AND pg_get_expr(conbin,conrelid) LIKE '%term_frequencies%'")
        self.assertTrue(constraint.replace("_", "").isalnum())
        drift = self.command("--command", "BEGIN; ALTER TABLE storage_v2_compact_posting_block "
            f"DROP CONSTRAINT {constraint}; ALTER TABLE storage_v2_compact_posting_block "
            "ADD CONSTRAINT fixture_weakened_frequency CHECK(TRUE); "
            + MIGRATION.read_text().replace("\nBEGIN;", "\n", 1).rsplit("COMMIT;", 1)[0]
            + "ROLLBACK;", check=False)
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("compact posting constraint identity differs", drift.stderr)
        self.file(MIGRATION)
