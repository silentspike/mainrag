"""Complete reader equivalence, scoped metadata, and covering query postings."""
import json
import re
import unittest
from unittest.mock import patch

from eval.storage_v2.schema import test_scoped_posting_sets as previous

ROOT = previous.parent.schema.ROOT
METADATA = ROOT / "migrations/122_storage_v2_keyed_lexical_metadata.sql"
QUERY = ROOT / "migrations/123_storage_v2_shared_query_postings.sql"


class SharedQueryPostingTests(unittest.TestCase):
    schema = previous.parent.schema
    command = classmethod(previous.ScopedPostingSetsTests.command.__func__)
    sql = classmethod(previous.ScopedPostingSetsTests.sql.__func__)
    file = classmethod(previous.ScopedPostingSetsTests.file.__func__)
    admin = classmethod(previous.ScopedPostingSetsTests.admin.__func__)
    quote = staticmethod(previous.ScopedPostingSetsTests.quote)
    make_projection = previous.ScopedPostingSetsTests.make_projection
    begin = previous.ScopedPostingSetsTests.begin
    stage = previous.ScopedPostingSetsTests.stage
    complete_analysis = previous.ScopedPostingSetsTests.complete_analysis
    commit = previous.ScopedPostingSetsTests.commit
    put = previous.ScopedPostingSetsTests.put
    assert_sql_fails = previous.ScopedPostingSetsTests.assert_sql_fails

    @staticmethod
    def actor(user_id, statement):
        # Production calls these controlled readers as the application role;
        # the unchecked active evaluator is private to that role's definer.
        return f"SET ROLE mainrag; SET app.user_id='{user_id}'; {statement}"

    @classmethod
    def setUpClass(cls):
        try:
            previous.ScopedPostingSetsTests.setUpClass.__func__(cls)
            cls.file(previous.POSTINGS)
            cls.file(previous.GIN)
            # The old generic fixture granted every schema function to its
            # shadow worker. Match these two production reader ACLs exactly.
            for signature in ("storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)",
                              "storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)"):
                cls.sql(f"REVOKE ALL ON FUNCTION {signature} FROM storage_v2_shadow_worker")
        except BaseException:
            if hasattr(cls, "stack"):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        previous.ScopedPostingSetsTests.tearDownClass.__func__(cls)

    def test_a_complete_active_and_exact_envelopes(self):
        self.file(METADATA)
        with patch.object(previous.parent, "MIGRATION", QUERY):
            previous.parent.BoundNativeRankTests.test_complete_results_authorization_ties_and_replay(self)
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            "SELECT storage_v2_search_exact(6,'1','{\"type\":\"term\",\"value\":\"alpha\"}','{}',10)"),
            "authorized generation selector required")

    def test_sparse_dense_multi_term_duplicate_null_and_collision_scopes(self):
        self.file(METADATA)
        self.file(QUERY)
        definition = self.sql("SELECT pg_get_functiondef('storage_v2_scoped_query_posting(bigint[],text[])'::regprocedure)")
        point = self.sql("SELECT pg_get_functiondef('storage_v2_document_posting(bigint,text)'::regprocedure)")
        for original, replacement in (
            ("public.storage_v2_scoped_query_posting", "query_fixture.scoped"),
            ("public.storage_v2_document_posting", "query_fixture.point"),
            ("public.storage_v2_search_posting", "query_fixture.flat"),
            ("public.storage_v2_compact_posting_block", "query_fixture.compact"),
        ):
            definition = definition.replace(original, replacement)
            point = point.replace(original, replacement)
        # Physical-scale namespace; real reader ACL/provenance is tested above.
        self.sql("""
CREATE SCHEMA query_fixture;
CREATE TABLE query_fixture.flat(document_id bigint,term text,term_frequency bigint,
 term_sha256 bytea,PRIMARY KEY(document_id,term_sha256));
CREATE INDEX query_fixture_scope ON query_fixture.flat(term_sha256,document_id) INCLUDE(term_frequency);
CREATE TABLE query_fixture.compact(document_id bigint,block_order bigint,
 terms text[],term_frequencies bigint[],fingerprints integer[],PRIMARY KEY(document_id,block_order));
CREATE INDEX query_fixture_fingerprints ON query_fixture.compact USING gin(fingerprints);
INSERT INTO query_fixture.flat
 SELECT i,'common',1,digest('common','sha256') FROM generate_series(1,50000) i WHERE i%5<>0;
INSERT INTO query_fixture.flat VALUES (1,'sparse',7,digest('sparse','sha256')),
 (2,'false-digest-collision',9,digest('collision-target','sha256'));
INSERT INTO query_fixture.compact SELECT i,0,ARRAY['common','sparse','common'],
 ARRAY[2::bigint,3::bigint,4::bigint],storage_v2_posting_fingerprints(ARRAY['common','sparse','common'])
 FROM generate_series(1,50000) i WHERE i%5=0;
-- Preserve physical ordinal pairing even for different array lower bounds.
INSERT INTO query_fixture.compact VALUES (1,1,'[0:2]={common,common,sparse}'::text[],
 '[3:5]={11,12,13}'::bigint[],storage_v2_posting_fingerprints(ARRAY['common','common','sparse']));
ANALYZE query_fixture.flat; ANALYZE query_fixture.compact;
""" + point + ";" + definition + ";")
        scopes = ["NULL::bigint[]", "ARRAY[]::bigint[]", "ARRAY[NULL]::bigint[]",
                  "ARRAY[1,1,5,NULL]::bigint[]",
                  "ARRAY(SELECT i::bigint FROM generate_series(1,1024) i)",
                  "ARRAY(SELECT i::bigint FROM generate_series(1,1025) i)",
                  "ARRAY(SELECT i::bigint FROM generate_series(1,45000) i)||ARRAY[1,5,NULL]::bigint[]"]
        terms = ["NULL::text[]", "ARRAY[]::text[]", "ARRAY['common']::text[]",
                 "ARRAY['common','sparse','common',NULL]::text[]",
                 "ARRAY['absent','collision-target']::text[]"]
        for scope in scopes:
            for query in terms:
                value = json.loads(self.sql(f"""
WITH actual AS MATERIALIZED (SELECT * FROM query_fixture.scoped({scope},{query})),
requested_term AS (SELECT DISTINCT value FROM unnest({query}) input(value) WHERE value IS NOT NULL),
reference AS MATERIALIZED (
 SELECT p.document_id,p.term,p.term_frequency FROM query_fixture.flat p JOIN requested_term q ON q.value=p.term
 WHERE p.document_id=ANY({scope}) AND p.term_sha256=digest(q.value,'sha256')
 UNION ALL
 SELECT b.document_id,item.term,item.frequency FROM query_fixture.compact b
 CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) item(term,frequency)
 JOIN requested_term q ON q.value=item.term WHERE b.document_id=ANY({scope})
), a AS (SELECT coalesce(jsonb_agg(to_jsonb(actual) ORDER BY document_id,term,term_frequency),'[]') v FROM actual),
r AS (SELECT coalesce(jsonb_agg(to_jsonb(reference) ORDER BY document_id,term,term_frequency),'[]') v FROM reference)
SELECT jsonb_build_object('equal',a.v=r.v,'rows',jsonb_array_length(a.v)) FROM a,r
"""))
                self.assertTrue(value["equal"], (scope, query, value))
        plan = json.loads(self.sql("SET enable_seqscan=off; EXPLAIN (ANALYZE,FORMAT JSON) "
            "SELECT document_id,term_sha256,term_frequency FROM query_fixture.flat "
            "WHERE term_sha256=digest('sparse','sha256')"))
        self.assertEqual(plan[0]["Plan"]["Node Type"], "Index Only Scan")
        self.assertEqual(plan[0]["Plan"]["Actual Rows"], 1)
        print("independent complete posting comparisons: 35; physical corpus: 50,000", flush=True)

    def test_metadata_scope_does_not_expand_unrelated_vectors(self):
        self.file(METADATA)
        self.file(QUERY)
        # A real compact block has exactly the same presence and generated
        # marker semantics as its expanded logical rows. Keep the independent
        # reference view but never use it in the candidate implementation.
        self.sql("CREATE SCHEMA metadata_fixture; CREATE TABLE metadata_fixture.requested(id bigint); "
                 "CREATE TABLE metadata_fixture.ordinary(occurrence_id bigint,segment_order bigint,PRIMARY KEY(occurrence_id,segment_order)); "
                 "CREATE TABLE metadata_fixture.compact(occurrence_id bigint,segment_orders bigint[],fts_vectors tsvector[],PRIMARY KEY(occurrence_id)); "
                 "INSERT INTO metadata_fixture.requested VALUES (1),(2),(3),(4); "
                 "INSERT INTO metadata_fixture.ordinary VALUES (1,0),(2,9); "
                 "INSERT INTO metadata_fixture.compact SELECT i,ARRAY[CASE WHEN i%2=0 THEN 0 ELSE 7 END]::bigint[],"
                 "ARRAY[to_tsvector('simple','alpha beta')] FROM generate_series(3,50000) i; ANALYZE metadata_fixture.compact;")
        definition = self.sql("SELECT pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure)")
        marker = re.search(r"SELECT unprojected.id, (EXISTS \(.*?\) AS generated)\s+FROM unprojected", definition, re.S)
        self.assertIsNotNone(marker)
        expression = marker.group(1).replace("public.storage_v2_lexical_segment", "metadata_fixture.ordinary")
        expression = expression.replace("public.storage_v2_compact_lexical_block", "metadata_fixture.compact")
        query = "SELECT unprojected.id," + expression + " FROM metadata_fixture.requested unprojected"
        actual = self.sql(query + " ORDER BY 1")
        reference = self.sql("SELECT r.id, EXISTS(SELECT 1 FROM metadata_fixture.ordinary o WHERE o.occurrence_id=r.id AND o.segment_order=0 "
                             "UNION ALL SELECT 1 FROM metadata_fixture.compact b CROSS JOIN LATERAL unnest(b.segment_orders,b.fts_vectors) item(segment_order,vector) "
                             "WHERE b.occurrence_id=r.id AND item.segment_order=0) FROM metadata_fixture.requested r ORDER BY r.id")
        self.assertEqual(actual, reference)
        plan = json.loads(self.sql("EXPLAIN (ANALYZE,FORMAT JSON) " + query))[0]["Plan"]
        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)
        compact = [n for n in nodes(plan) if n.get("Relation Name") == "compact"]
        self.assertTrue(compact)
        self.assertTrue(all(n["Node Type"] == "Index Scan" and n["Actual Loops"] <= 4 for n in compact))
        self.assertNotIn("Function Scan", [n["Node Type"] for n in nodes(plan)])
        print("four keyed metadata probes against 50,000 rows; no vector expansion", flush=True)

    def test_function_and_index_drift_abort(self):
        self.file(METADATA)
        self.file(QUERY)
        body = QUERY.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        signature = "storage_v2_scoped_query_posting(bigint[],text[])"
        function = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
        for change, error in (
            (function.replace("RETURN QUERY", "/* drift */ RETURN QUERY", 1) + ";", "definition"),
            (f"GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;", "authority"),
            (f"ALTER FUNCTION {signature} SECURITY DEFINER;", "definition"),
            ("DROP INDEX idx_storage_v2_posting_query_scope; CREATE INDEX idx_storage_v2_posting_query_scope ON storage_v2_search_posting(term_sha256);", "index identity"),
        ):
            result = self.command("--command", "BEGIN;" + change + body + "ROLLBACK;", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error + " differs", result.stderr)
