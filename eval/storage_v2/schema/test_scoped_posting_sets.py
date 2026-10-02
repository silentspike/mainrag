"""Full reader equivalence, large posting scopes and source-aware GIN probes."""
import json
import unittest
from unittest.mock import patch

from eval.storage_v2.schema import test_posting_hash_correlation as previous

parent = previous.parent
POSTINGS = parent.schema.ROOT / "migrations/120_storage_v2_scoped_posting_sets.sql"
GIN = parent.schema.ROOT / "migrations/121_storage_v2_source_coupled_legacy_gin.sql"


class ScopedPostingSetsTests(unittest.TestCase):
    schema = parent.schema
    command = classmethod(parent.BoundNativeRankTests.command.__func__)
    sql = classmethod(parent.BoundNativeRankTests.sql.__func__)
    file = classmethod(parent.BoundNativeRankTests.file.__func__)
    admin = classmethod(parent.BoundNativeRankTests.admin.__func__)
    actor = staticmethod(parent.BoundNativeRankTests.actor)
    quote = staticmethod(parent.BoundNativeRankTests.quote)
    make_projection = parent.BoundNativeRankTests.make_projection
    begin = parent.BoundNativeRankTests.begin
    stage = parent.BoundNativeRankTests.stage
    complete_analysis = parent.BoundNativeRankTests.complete_analysis
    commit = parent.BoundNativeRankTests.commit
    put = parent.BoundNativeRankTests.put
    assert_sql_fails = parent.BoundNativeRankTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            parent.BoundNativeRankTests.setUpClass.__func__(cls)
            for number in range(106, 108):
                cls.file(next((parent.schema.ROOT / "migrations").glob(f"{number:03}_*.sql")))
            cls.file(previous.MIGRATION)
            cls.file(parent.schema.ROOT / "migrations/117_storage_v2_authorized_copied_rank_scope.sql")
        except BaseException:
            if hasattr(cls, "stack"):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        parent.BoundNativeRankTests.tearDownClass.__func__(cls)

    def test_full_envelopes_authorization_phrase_and_index_replay(self):
        for source in (6, 9):
            run = self.begin(source, f"{source:02x}" * 32, f"{source+20:02x}" * 32)
            texts = ["alpha beta common key_identifier", "alpha common beta",
                     "common beta", "alpha beta", "absent", ""]
            for number, text in enumerate(texts):
                node, view, digest = self.make_projection(text)
                self.stage(run, f"scoped-{source}-{number}.txt", text, node, view, digest)
                self.complete_analysis(digest)
                document = self.put(node, text)
                self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
            self.commit(run, len(texts))
        # Both sources have phrase terms, but some vectors lack adjacency.
        self.sql("INSERT INTO storage_v2_legacy_lexical_segment "
                 "(occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,fts_vector) "
                 "SELECT o.id,o.source_id,o.artifact_version_id,10000+o.id,digest(d.search_text,'sha256'),"
                 "to_tsvector('simple',d.search_text) FROM occurrence o "
                 "JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0 "
                 "JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.source_id IN (6,9)")

        def term(value):
            return {"type": "term", "value": value}

        asts = [term("alpha"), term("common"), term("missing"),
                {"type": "and", "children": [term("alpha"), term("beta")]},
                {"type": "or", "children": [term("alpha"), term("beta")]},
                {"type": "phrase", "value": "alpha beta"},
                {"type": "and", "children": [term("common"), {"type": "not", "children": [term("beta")]}]},
                {"type": "exact", "value": "key_identifier"}]

        def searches():
            statements = []
            for source in (6, 9):
                for ast in asts:
                    for limit in (1, 3, 10):
                        statements.append(f"SELECT storage_v2_search_exact({source},'1',"
                                          f"{self.quote(json.dumps(ast))}::jsonb,'{{}}'::jsonb,{limit});")
            return [json.loads(line) for line in self.sql(self.actor(
                parent.schema.ADMIN_ID, "\n".join(statements))).splitlines()]

        before = searches()
        rows = self.sql("SELECT md5(jsonb_agg(to_jsonb(s) ORDER BY occurrence_id,legacy_chunk_id)::text) "
                        "FROM storage_v2_legacy_lexical_segment s")
        for migration in (POSTINGS, GIN):
            self.file(migration)
            self.file(migration)
        self.assertEqual(before, searches())
        self.assertEqual(rows, self.sql("SELECT md5(jsonb_agg(to_jsonb(s) ORDER BY occurrence_id,legacy_chunk_id)::text) "
                                       "FROM storage_v2_legacy_lexical_segment s"))
        index = "idx_storage_v2_legacy_lexical_segment_fts"
        self.assertEqual(self.sql(f"SELECT relowner::regrole FROM pg_class WHERE oid='{index}'::regclass"),
                         "mainrag_v2_frontier_owner")
        # Prove the GIN can combine both predicates. The tiny fixture may
        # normally prefer its cheaper source B-tree; remove only disposable
        # alternative indexes inside a rolled-back fixture transaction.
        plan = json.loads(self.sql("""BEGIN;
DO $$ DECLARE alternative REGCLASS; BEGIN
 FOR alternative IN SELECT indexrelid::REGCLASS FROM pg_index
  WHERE indrelid='storage_v2_legacy_lexical_segment'::REGCLASS
    AND indexrelid<>'idx_storage_v2_legacy_lexical_segment_fts'::REGCLASS
    AND NOT indisprimary AND NOT indisunique LOOP
  EXECUTE format('DROP INDEX %s',alternative);
 END LOOP;
END $$;
SET enable_seqscan=off;
EXPLAIN (FORMAT JSON) SELECT occurrence_id """
            "FROM storage_v2_legacy_lexical_segment WHERE source_id=6::BIGINT "
            "AND fts_vector @@ websearch_to_tsquery('simple','\"alpha beta\"'); ROLLBACK;"))
        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)
        probes = [node for node in nodes(plan[0]["Plan"]) if node.get("Index Name") == index]
        self.assertEqual(len(probes), 1)
        self.assertIn("source_id =", probes[0]["Index Cond"])
        self.assertIn("fts_vector", probes[0]["Index Cond"])
        denied = self.sql(f"SET ROLE mainrag; SET app.user_id='{parent.schema.OTHER_ID}'; " +
            "SELECT count(*) FROM storage_v2_source_segment_rank_candidates("
            "ARRAY(SELECT id FROM occurrence WHERE source_id IN (6,9)),'alpha',ARRAY[6,9]::bigint[])")
        self.assertEqual(denied, "0")
        print("complete envelopes compared:", len(before), flush=True)

    def test_large_sparse_dense_duplicate_null_and_collision_scopes(self):
        # The independent flat/compact reference includes 50,000 documents,
        # hash collisions and both sides of the point/set threshold.
        with patch.object(previous, "MIGRATION", POSTINGS):
            previous.PostingHashCorrelationTests.test_sparse_dense_duplicate_null_and_collision_scopes(self)

    def test_posting_identity_and_authority_drift_fail_closed(self):
        self.file(POSTINGS)
        signature = "storage_v2_scoped_term_posting(bigint[],text)"
        candidate = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
        body = POSTINGS.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        for change, expected in (
            (candidate.replace("RETURN QUERY", "/* drift */ RETURN QUERY", 1) + ";", "definition"),
            (f"ALTER FUNCTION {signature} OWNER TO storage_v2_shadow_worker;", "authority"),
            (f"GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;", "authority"),
            (f"ALTER FUNCTION {signature} SECURITY DEFINER;", "definition"),
        ):
            result = self.command("--command", "BEGIN; " + change + body + "ROLLBACK;", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"scoped posting {expected} differs", result.stderr)

    def test_index_identity_drift_fails_before_replacement(self):
        self.file(GIN)
        body = GIN.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        change = ("DROP INDEX idx_storage_v2_legacy_lexical_segment_fts; "
                  "CREATE INDEX idx_storage_v2_legacy_lexical_segment_fts "
                  "ON storage_v2_legacy_lexical_segment USING gin(fts_vector); "
                  "ALTER TABLE storage_v2_legacy_lexical_segment OWNER TO mainrag;")
        result = self.command("--command", "BEGIN; " + change + body + "ROLLBACK;", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owned copied lexical index authority differs", result.stderr)
