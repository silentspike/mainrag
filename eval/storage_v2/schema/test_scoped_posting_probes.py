"""Prevent global common-term scans in the complete scoped retrieval query."""
from __future__ import annotations

import json

from eval.storage_v2.schema import test_search_materialization as previous
from eval.storage_v2.schema import test_shadow_ingest_schema as schema

MIGRATION = schema.ROOT / "migrations/045_storage_v2_scoped_posting_probes.sql"


class ScopedPostingTests(schema.ShadowIngestSchemaTests):
    make_search_fixture = previous.SearchMaterializationTests.make_search_fixture

    def test_complete_results_match_before_and_after_scoped_posting_reuse(self) -> None:
        self.make_search_fixture()
        queries = [
            {"type": "term", "value": "alpha"},
            {"type": "term", "value": "missing"},
            {"type": "term", "value": "token_" + "x" * 4096},
            {"type": "phrase", "value": "alpha beta"},
            {"type": "exact", "value": "exact_key"},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"}, {"type": "term", "value": "beta"}]},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"}, {"type": "term", "value": "alpha"}]},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"},
                {"type": "not", "children": [{"type": "term", "value": "forbidden"}]}]},
            {"type": "or", "children": [
                {"type": "term", "value": "entry0"}, {"type": "term", "value": "entry1"}]},
        ]
        filters = [{}, {"path_prefix": "/synthetic/late-000"}, {"role": "heading"},
                   {"graph_profile": "not-present", "semantic_profile": "not-present"}]
        try:
            self.sql(previous.baseline_definition())
            self.file(previous.MIGRATION)
            before = [self.exact_search(q, f, source_id=15) for q in queries for f in filters]
            self.file(MIGRATION)
            self.file(MIGRATION)
            after = [self.exact_search(q, f, source_id=15) for q in queries for f in filters]
            self.assertEqual(before, after)
            self.assertEqual(after[0]["fully_scored_views"], 24)
            self.assertEqual(after[0]["total"], 24)
        finally:
            self.sql(previous.baseline_definition())
            self.file(previous.MIGRATION)
            self.file(MIGRATION)

    def test_actual_common_term_plan_never_uses_global_posting_index(self) -> None:
        self.make_search_fixture()
        before = self.exact_search({"type": "term", "value": "alpha"}, source_id=15)
        # A globally common term must not force retrieval outside the 24-view
        # scope. These documents have no occurrence in that scope.
        self.sql(self.admin("""
WITH bodies AS MATERIALIZED (
 SELECT body.id, 'alpha unrelated' || value AS content
 FROM generate_series(1,5000) value
 CROSS JOIN LATERAL storage_v2_put_inline_body(
 convert_to('alpha unrelated' || value,'UTF8')) body
)
SELECT count(*) FROM bodies CROSS JOIN LATERAL storage_v2_put_search_document(
 'global-common-term-fixture','body',bodies.id,bodies.content,ARRAY[]::TEXT[]) document;
"""))
        self.sql("ANALYZE storage_v2_search_document; ANALYZE storage_v2_search_posting;")
        self.assertEqual(before, self.exact_search(
            {"type": "term", "value": "alpha"}, source_id=15),
            "out-of-scope documents must not change corpus normalization or results")
        definition = self.sql(f"SELECT pg_get_functiondef('{previous.SIGNATURE}'::REGPROCEDURE)")
        statement = "WITH RECURSIVE" + definition.split("    WITH RECURSIVE", 1)[1] \
            .split(" INTO v_result;", 1)[0]
        for old, new in (("v_generation.generation_seq", "1"), ("p_source_id", "$1"),
                         ("p_ast", "$2"), ("p_filters", "$3"), ("p_limit", "$4")):
            statement = statement.replace(old, new)
        plan = json.loads(self.sql(
            "SET plan_cache_mode=force_generic_plan; SET jit=off; "
            f"PREPARE common_probe(BIGINT,JSONB,JSONB,BIGINT) AS {statement}; "
            "EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON) EXECUTE common_probe("
            "15,'{\"type\":\"term\",\"value\":\"alpha\"}','{}',3);"
        ))[0]["Plan"]
        nodes = list(previous.nodes(plan))
        self.assertNotIn("idx_storage_v2_search_posting_term", [n.get("Index Name") for n in nodes])
        posting = [n for n in nodes if n.get("Relation Name") == "storage_v2_search_posting"]
        self.assertEqual(len(posting), 1, "the three consumers must share one physical lookup")
        probe = posting[0]
        self.assertEqual(probe["Index Name"], "storage_v2_search_posting_pkey")
        self.assertIn("document_id", probe["Index Cond"])
        self.assertIn("term_sha256", probe["Index Cond"])
        self.assertEqual(probe["Actual Rows"], 1)
        self.assertEqual(probe["Actual Loops"], 24)
        materialization = [n for n in nodes if n.get("Subplan Name") == "CTE scoped_posting"]
        self.assertEqual(len(materialization), 1)
        self.assertEqual(materialization[0]["Actual Rows"], 24)
        aggregates = [n for n in nodes if n["Node Type"] == "Aggregate"
                      and any("string_agg" in value for value in n.get("Output", []))]
        self.assertEqual(len(aggregates), 1)
        self.assertEqual(aggregates[0]["Actual Loops"], 3)
        # Even when given an existing digest, the outer text guard rejects a
        # different authoritative term. It cannot be pushed into the hash index.
        document_id = self.sql("SELECT min(id) FROM storage_v2_search_document "
                               "WHERE profile_id='global-common-term-fixture'")
        self.assertEqual(self.sql(f"""
SELECT term FROM (
 SELECT term FROM storage_v2_search_posting
 WHERE document_id={document_id} AND term_sha256=digest('alpha','sha256') OFFSET 0
) posting WHERE term='different-authoritative-term';
"""), "")

    def test_migration_replay_preserves_authority_and_rejects_drift(self) -> None:
        signature = previous.SIGNATURE
        metadata = f"SELECT proowner::TEXT || ':' || COALESCE(proacl::TEXT,'') " \
                   f"FROM pg_proc WHERE oid='{signature}'::REGPROCEDURE"
        before = self.sql(metadata)
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(self.sql(metadata), before)
        try:
            self.sql(previous.baseline_definition())
            self.file(previous.MIGRATION)
            original = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            self.sql(original.replace("posting.term = ANY(query.score_terms)", "FALSE"))
            drift = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            result = self.command("--file", str(MIGRATION), check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("posting consumers differ", result.stderr)
            self.assertEqual(drift, self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)"))
        finally:
            self.sql(previous.baseline_definition())
            self.file(previous.MIGRATION)
            self.file(MIGRATION)
