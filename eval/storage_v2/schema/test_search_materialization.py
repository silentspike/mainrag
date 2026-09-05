"""Differential and real-plan checks for complete, late-materialized retrieval."""

from __future__ import annotations

import json
import re

from eval.storage_v2.schema import test_shadow_ingest_schema as schema


MIGRATION = schema.ROOT / "migrations/044_storage_v2_late_search_materialization.sql"
SIGNATURE = "storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)"


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def baseline_definition() -> str:
    original = (schema.ROOT / "migrations/034_storage_v2_retrieval.sql").read_text()
    definition = re.search(
        r"CREATE OR REPLACE FUNCTION storage_v2_search_exact\(.*?\n\$\$;",
        original, re.DOTALL,
    ).group(0)
    return definition.replace(
        "binding.fts_simple @@ phraseto_tsquery('simple', phrase.value)",
        "storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)",
    )


class SearchMaterializationTests(schema.ShadowIngestSchemaTests):
    """Also rerun the inherited authorization, phrase and oversized-input gate."""

    def make_search_fixture(self, count: int = 24) -> None:
        if self.sql("SELECT count(*) FROM sources WHERE id=15") != "0":
            return
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(15,'late-materialization-fixture','fixture','synthetic-late')")
        run = self.begin(15, "d1" * 32, "d2" * 32)
        for index in range(count):
            content = (f"entry{index} alpha beta exact_key " + "padding " * 2048
                       + (" forbidden" if index % 2 else "") + " token_" + "x" * 4096)
            node, view, digest = self.make_projection(content)
            self.stage(run, f"late-{index:04}.txt", content, node, view, digest)
            self.complete_analysis(digest)
            document = self.sql(self.admin(
                "SELECT id FROM storage_v2_put_search_document("
                f"'native-gin-v1','node',{node},'{content}',ARRAY['exact_key']);"
            ))
            self.sql(self.admin(
                f"SELECT storage_v2_bind_search_document({view},0,{document},1.0);"
            ))
        self.commit(run, count)
        self.sql("ANALYZE;")

    def test_late_materialization_preserves_complete_results_scores_and_acl(self) -> None:
        self.make_search_fixture()
        queries = [
            {"type": "term", "value": "alpha"},
            {"type": "phrase", "value": "alpha beta"},
            {"type": "phrase", "value": "missing phrase"},
            {"type": "exact", "value": "exact_key"},
            {"type": "term", "value": "token_" + "x" * 4096},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"},
                {"type": "not", "children": [{"type": "term", "value": "forbidden"}]},
            ]},
            {"type": "or", "children": [
                {"type": "term", "value": "entry0"},
                {"type": "term", "value": "entry1"},
            ]},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"},
                {"type": "term", "value": "alpha"},
            ]},
        ]
        filters = [{}, {"role": "artifact", "graph_profile": "not-present"},
                   {"path_prefix": "/synthetic/late-000"},
                   {"occurred_from": "2100-01-01T00:00:00Z"}]
        metadata = self.sql(f"SELECT proowner::TEXT || ':' || COALESCE(proacl::TEXT,'') "
                            f"FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE")
        try:
            self.sql(baseline_definition())
            before = [self.exact_search(q, f, source_id=15) for q in queries for f in filters]
            self.file(MIGRATION)
            self.file(MIGRATION)
            after = [self.exact_search(q, f, source_id=15) for q in queries for f in filters]
            self.assertEqual(before, after)
            self.assertEqual(after[0]["total"], 24)
            self.assertEqual(len(after[0]["results"]), 10)
            self.assertGreater(len(after[0]["results"][0]["content"]), 16000)
            self.assertEqual(metadata, self.sql(
                f"SELECT proowner::TEXT || ':' || COALESCE(proacl::TEXT,'') "
                f"FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE"))
        finally:
            self.sql(baseline_definition())
            self.file(MIGRATION)

    def test_actual_query_plan_materializes_only_returned_views(self) -> None:
        # Reuse the differential fixture if present, without depending on test order.
        if self.sql("SELECT count(*) FROM sources WHERE id=15") == "0":
            self.make_search_fixture()
        definition = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        statement = definition.split("    WITH RECURSIVE", 1)[1].split(" INTO v_result;", 1)[0]
        statement = "WITH RECURSIVE" + statement
        for old, new in (("v_generation.generation_seq", "1"), ("p_source_id", "$1"),
                         ("p_ast", "$2"), ("p_filters", "$3"), ("p_limit", "$4")):
            statement = statement.replace(old, new)
        plan = json.loads(self.sql(
            "SET plan_cache_mode=force_generic_plan; SET jit=off; "
            f"PREPARE fixture_search(BIGINT,JSONB,JSONB,BIGINT) AS {statement}; "
            "EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON) EXECUTE fixture_search("
            "15,'{\"type\":\"term\",\"value\":\"alpha\"}','{}',3);"
        ))[0]["Plan"]
        aggregates = [n for n in nodes(plan) if n["Node Type"] == "Aggregate"
                      and any("string_agg" in x for x in n.get("Output", []))]
        self.assertEqual(len(aggregates), 1)
        self.assertEqual(aggregates[0]["Actual Loops"], 3)
        self.assertEqual(aggregates[0]["Actual Rows"], 1)
        scope = [n for n in nodes(plan) if n.get("Subplan Name") == "CTE scoped_binding"]
        self.assertEqual(len(scope), 1)
        self.assertEqual(scope[0]["Actual Rows"], 24)
        self.assertFalse(any("search_text" in x or "fts_simple" in x
                             or "exact_identifiers" in x for x in scope[0]["Output"]))

    def test_migration_rejects_definition_drift_atomically(self) -> None:
        before = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        try:
            # Break an exact reviewed anchor, rather than accepting a partial rewrite.
            self.sql(baseline_definition().replace("'content', content,", "'content', 'drift',"))
            drifted = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
            result = self.command("--file", str(MIGRATION), check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("definition differs", result.stderr)
            self.assertEqual(drifted, self.sql(
                f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)"))
        finally:
            self.sql(before)

    def test_generic_posting_probes_use_both_primary_key_columns_and_term_guard(self) -> None:
        self.make_search_fixture()
        definition = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        predicates = re.findall(
            r"WHERE posting.term_sha256 = ANY\(query\.(?:score_)?term_hashes\)\s+"
            r"AND posting.term = ANY\(query\.(?:score_)?terms\)", definition,
        )
        self.assertEqual(len(predicates), 3)
        # Many terms in one fixture document make prefix-only probes visibly
        # expensive. Keep this disposable extension isolated from differential tests.
        setup = """
WITH body AS (
 SELECT value.id FROM (SELECT string_agg('probe' || i, ' ') AS text
 FROM generate_series(1,5000) i) text
 CROSS JOIN LATERAL storage_v2_put_inline_body(convert_to(text.text,'UTF8')) value
), document AS (
 SELECT value.id FROM body
 CROSS JOIN LATERAL storage_v2_put_search_document('probe-fixture','body',body.id,
 (SELECT string_agg('probe' || i, ' ') FROM generate_series(1,5000) i),ARRAY[]::TEXT[]) value
)
SELECT id FROM document;
"""
        document_id = int(self.sql(self.admin(setup)))
        self.sql("ANALYZE storage_v2_search_posting")
        for predicate in predicates:
            predicate = re.sub(r"query\.(?:score_)?term_hashes", "$2", predicate)
            predicate = re.sub(r"query\.(?:score_)?terms", "$3", predicate)
            statement = "SELECT term FROM storage_v2_search_posting posting " + predicate \
                        + " AND posting.document_id=$1"
            plan = json.loads(self.sql(
                "SET plan_cache_mode=force_generic_plan; "
                f"PREPARE fixture_probe(BIGINT,BYTEA[],TEXT[]) AS {statement}; "
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) EXECUTE fixture_probe("
                f"{document_id},ARRAY[digest('probe1','sha256')],ARRAY['probe1']);"
            ))[0]["Plan"]
            self.assertEqual(plan["Index Name"], "storage_v2_search_posting_pkey")
            self.assertIn("document_id = $1", plan["Index Cond"])
            self.assertIn("term_sha256 = ANY ($2)", plan["Index Cond"])
            self.assertEqual(plan["Actual Rows"], 1)
            self.assertEqual(plan.get("Rows Removed by Filter", 0), 0)
            self.assertEqual(self.sql(
                f"PREPARE fixture_probe(BIGINT,BYTEA[],TEXT[]) AS {statement}; "
                f"EXECUTE fixture_probe({document_id},ARRAY[digest('probe1','sha256')],ARRAY['probe2']);"
            ), "", "hash-only equality must never accept a different authoritative term")
